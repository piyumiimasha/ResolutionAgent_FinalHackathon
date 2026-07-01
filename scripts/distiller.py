"""
distiller.py — LLM distillation pipeline.

Reads every transcript from Azure Blob, sends to Groq LLM,
extracts Problem / Root Cause / Resolution, saves to Qdrant
as Procedural Memory.

Run once before the app starts:
    python scripts/distill_transcripts.py
"""

import json
import asyncio
import logging
from dataclasses import dataclass

from groq import AsyncGroq
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance
from dotenv import load_dotenv
from loguru import logger

from services.ingest_service.transcript_reader import TranscriptReader, ParsedTranscript
from infrastructure.llm.embeddings import get_embedding

load_dotenv()

QDRANT_URL              = __import__("os").getenv("QDRANT_URL")
QDRANT_API_KEY          = __import__("os").getenv("QDRANT_API_KEY")
GROQ_API_KEY            = __import__("os").getenv("GROQ_API_KEY")
COLLECTION_NAME         = "procedural_memory"
DISTILL_MODEL           = "llama-3.1-8b-instant"   # fast + cheap for bulk distillation
VECTOR_SIZE             = 1536                       # match your OpenRouter embedding model
BATCH_SIZE              = 20                         # Qdrant upsert batch size
CONCURRENT_LLM_CALLS   = 5                          # parallel Groq calls


DISTILLATION_PROMPT = """
You are an IT support knowledge extractor.

Given the following IT support conversation, extract ONLY:
1. PROBLEM: What was broken or requested (one sentence, factual)
2. ROOT_CAUSE: Why it happened (one sentence, or "Unknown" if not determined)
3. RESOLUTION: Exactly what fixed it (one sentence, specific steps if available)

Rules:
- Be factual and specific. No fluff.
- Ignore tone, pleasantries, false starts, and escalation chatter.
- Focus only on the technical content.
- If the issue was NOT resolved, set RESOLUTION to "Unresolved".

Respond with ONLY valid JSON, no markdown:
{
  "problem": "...",
  "root_cause": "...",
  "resolution": "..."
}

CONVERSATION:
"""


@dataclass
class DistilledRecord:
    """Extracted knowledge from one transcript."""
    transcript_id: str
    ticket_type_int: int
    ticket_type_str: str
    problem: str
    root_cause: str
    resolution: str
    channel: str
    was_escalated: bool
    resolution_time_minutes: int
    satisfaction_score: int
    timestamp: str


async def _call_groq(client: AsyncGroq, conversation_text: str) -> dict:
    """
    Send one conversation to Groq and extract structured knowledge.

    Args:
        client:            AsyncGroq client.
        conversation_text: Plain-text conversation from transcript_reader.

    Returns:
        Dict with problem, root_cause, resolution keys.
    """
    response = await client.chat.completions.create(
        model=DISTILL_MODEL,
        messages=[
            {
                "role": "user",
                "content": DISTILLATION_PROMPT + conversation_text,
            }
        ],
        temperature=0.1,       # low temp for factual extraction
        max_tokens=300,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content.strip()
    return json.loads(raw)


async def _distill_one(
    groq_client: AsyncGroq,
    transcript: ParsedTranscript,
    semaphore: asyncio.Semaphore,
) -> DistilledRecord | None:
    """
    Distill a single transcript with concurrency control via semaphore.

    Args:
        groq_client: AsyncGroq client.
        transcript:  ParsedTranscript from transcript_reader.
        semaphore:   Limits concurrent Groq API calls.

    Returns:
        DistilledRecord or None if extraction fails.
    """
    async with semaphore:
        try:
            extracted = await _call_groq(groq_client, transcript.conversation_text)
            return DistilledRecord(
                transcript_id           = transcript.transcript_id,
                ticket_type_int         = transcript.ticket_type_int,
                ticket_type_str         = transcript.ticket_type_str,
                problem                 = extracted.get("problem", ""),
                root_cause              = extracted.get("root_cause", ""),
                resolution              = extracted.get("resolution", ""),
                channel                 = transcript.channel,
                was_escalated           = transcript.was_escalated,
                resolution_time_minutes = transcript.resolution_time_minutes,
                satisfaction_score      = transcript.satisfaction_score,
                timestamp               = transcript.timestamp,
            )
        except Exception as exc:
            logger.error("Distillation failed for {}: {}", transcript.transcript_id, exc)
            return None


async def _ensure_collection(qdrant: AsyncQdrantClient) -> None:
    """Create Qdrant collection if it doesn't exist."""
    collections = await qdrant.get_collections()
    names = [c.name for c in collections.collections]
    if COLLECTION_NAME not in names:
        await qdrant.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        logger.info("Created Qdrant collection '{}'.", COLLECTION_NAME)
    else:
        logger.info("Qdrant collection '{}' already exists.", COLLECTION_NAME)


async def _upsert_batch(
    qdrant: AsyncQdrantClient,
    records: list[DistilledRecord],
    start_id: int,
) -> None:
    """
    Embed and upsert a batch of distilled records into Qdrant.

    Args:
        qdrant:   AsyncQdrantClient.
        records:  List of DistilledRecord to upsert.
        start_id: Starting integer ID for this batch.
    """
    points = []
    for i, record in enumerate(records):
        # Embed the combined problem + resolution text
        text_to_embed = f"{record.problem} {record.root_cause} {record.resolution}"
        vector = await get_embedding(text_to_embed)

        points.append(PointStruct(
            id      = start_id + i,
            vector  = vector,
            payload = {
                "transcript_id":            record.transcript_id,
                "ticket_type_int":          record.ticket_type_int,
                "ticket_type_str":          record.ticket_type_str,
                "problem":                  record.problem,
                "root_cause":               record.root_cause,
                "resolution":               record.resolution,
                "channel":                  record.channel,
                "was_escalated":            record.was_escalated,
                "resolution_time_minutes":  record.resolution_time_minutes,
                "satisfaction_score":       record.satisfaction_score,
                "timestamp":                record.timestamp,
            },
        ))

    await qdrant.upsert(collection_name=COLLECTION_NAME, points=points)


async def run_distillation() -> dict:
    """
    Main distillation pipeline. Reads all transcripts from Azure Blob,
    distills with Groq, saves to Qdrant.

    Returns:
        Summary dict with counts.
    """
    groq_client = AsyncGroq(api_key=GROQ_API_KEY)
    qdrant      = AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    reader      = TranscriptReader()
    semaphore   = asyncio.Semaphore(CONCURRENT_LLM_CALLS)

    await _ensure_collection(qdrant)

    # ── Load all transcripts (lazy iter converted to list for batching) ──
    logger.info("Reading transcripts from Azure Blob...")
    transcripts = list(reader.iter_all())
    logger.info("Loaded {} transcripts. Starting distillation...", len(transcripts))

    # ── Distill all concurrently (with semaphore) ─────────────────────
    tasks = [
        _distill_one(groq_client, t, semaphore)
        for t in transcripts
    ]
    results = await asyncio.gather(*tasks)

    # Filter out failures
    distilled = [r for r in results if r is not None]
    failed    = len(results) - len(distilled)
    logger.info("Distilled: {} | Failed: {}", len(distilled), failed)

    # ── Upsert to Qdrant in batches ───────────────────────────────────
    for i in range(0, len(distilled), BATCH_SIZE):
        batch = distilled[i : i + BATCH_SIZE]
        await _upsert_batch(qdrant, batch, start_id=i)
        logger.info("Upserted batch {}/{}", min(i + BATCH_SIZE, len(distilled)), len(distilled))

    summary = {
        "total_transcripts": len(transcripts),
        "distilled":         len(distilled),
        "failed":            failed,
        "collection":        COLLECTION_NAME,
    }
    logger.info("Distillation complete: {}", summary)
    return summary
