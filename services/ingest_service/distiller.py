"""
distiller.py — LLM distillation pipeline.

Reads every transcript from Azure Blob via TranscriptReader,
sends each conversation to Groq LLM, extracts:
  - Problem
  - Root Cause
  - Resolution

Saves all 1050 structured records to Qdrant as Procedural Memory.

Run once before the app starts:
    python scripts/distill_transcripts.py
"""

import json
import asyncio
from dataclasses import dataclass

from groq import AsyncGroq
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance
from loguru import logger
from dotenv import load_dotenv

import os
from services.ingest_service.transcript_reader import TranscriptReader, ParsedTranscript
from infastructure.llm.embeddings import get_embedding, get_embeddings_batch
from infastructure.config import get_config, get_embedding_dimensions

load_dotenv()

GROQ_API_KEY        = os.getenv("GROQ_API_KEY")
QDRANT_URL          = os.getenv("QDRANT_URL")
QDRANT_API_KEY      = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME     = "procedural_memory"
DISTILL_MODEL       = "llama-3.1-8b-instant"   # fast + cheap for bulk distillation
CONCURRENT_CALLS    = 5                          # parallel Groq calls
BATCH_SIZE          = 20                         # Qdrant upsert batch size


DISTILLATION_PROMPT = """
You are an IT support knowledge extractor.

Given the following IT support conversation, extract ONLY:
1. PROBLEM: What was broken or requested (one sentence, factual)
2. ROOT_CAUSE: Why it happened (one sentence, or "Unknown" if not determined)
3. RESOLUTION: Exactly what fixed it (specific steps if available, or "Unresolved")

Rules:
- Be factual and specific. No fluff.
- Ignore tone, pleasantries, false starts, and escalation chatter.
- Focus only on the technical content.
- If issue was NOT resolved, set RESOLUTION to "Unresolved".

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
    transcript_id:           str
    ticket_type_int:         int
    ticket_type_str:         str
    problem:                 str
    root_cause:              str
    resolution:              str
    channel:                 str
    was_escalated:           bool
    resolution_time_minutes: int
    satisfaction_score:      int
    timestamp:               str


async def _call_groq(
    client: AsyncGroq,
    transcript: ParsedTranscript,
    semaphore: asyncio.Semaphore,
) -> DistilledRecord | None:
    """
    Send one conversation to Groq and extract structured knowledge.
    Semaphore limits concurrent calls to respect rate limits.

    Args:
        client:     AsyncGroq client.
        transcript: ParsedTranscript from reader.
        semaphore:  Controls concurrency.

    Returns:
        DistilledRecord or None if extraction fails.
    """
    async with semaphore:
        try:
            response = await client.chat.completions.create(
                model           = DISTILL_MODEL,
                messages        = [{
                    "role":    "user",
                    "content": DISTILLATION_PROMPT + transcript.conversation_text,
                }],
                temperature     = 0.1,
                max_tokens      = 300,
                response_format = {"type": "json_object"},
            )
            raw      = response.choices[0].message.content.strip()
            extracted = json.loads(raw)

            return DistilledRecord(
                transcript_id           = transcript.transcript_id,
                ticket_type_int         = transcript.ticket_type_int,
                ticket_type_str         = transcript.ticket_type_str,
                problem                 = extracted.get("problem", ""),
                root_cause              = extracted.get("root_cause", "Unknown"),
                resolution              = extracted.get("resolution", "Unresolved"),
                channel                 = transcript.channel,
                was_escalated           = transcript.was_escalated,
                resolution_time_minutes = transcript.resolution_time_minutes,
                satisfaction_score      = transcript.satisfaction_score,
                timestamp               = transcript.timestamp,
            )

        except json.JSONDecodeError as exc:
            logger.error(
                "JSON parse error for {}: {}",
                transcript.transcript_id, exc,
            )
            return None
        except Exception as exc:
            logger.error(
                "Groq call failed for {}: {}",
                transcript.transcript_id, exc,
            )
            return None


async def _ensure_collection(qdrant: AsyncQdrantClient) -> None:
    """Create procedural_memory collection if it doesn't exist."""
    dimensions  = get_embedding_dimensions()
    collections = {c.name for c in (await qdrant.get_collections()).collections}

    if COLLECTION_NAME not in collections:
        await qdrant.create_collection(
            collection_name = COLLECTION_NAME,
            vectors_config  = VectorParams(size=dimensions, distance=Distance.COSINE),
        )
        logger.info("Created Qdrant collection '{}'.", COLLECTION_NAME)
    else:
        logger.info("Collection '{}' already exists.", COLLECTION_NAME)


async def _upsert_batch(
    qdrant:   AsyncQdrantClient,
    records:  list[DistilledRecord],
    start_id: int,
) -> None:
    """
    Embed and upsert a batch of distilled records into Qdrant.

    Args:
        qdrant:   Qdrant async client.
        records:  Batch of DistilledRecord to upsert.
        start_id: Starting integer ID for this batch.
    """
    # Embed combined text for each record
    texts   = [f"{r.problem} {r.root_cause} {r.resolution}" for r in records]
    vectors = await get_embeddings_batch(texts)

    points = [
        PointStruct(
            id     = start_id + i,
            vector = vectors[i],
            payload = {
                "transcript_id":           records[i].transcript_id,
                "ticket_type_int":         records[i].ticket_type_int,
                "ticket_type_str":         records[i].ticket_type_str,
                "problem":                 records[i].problem,
                "root_cause":              records[i].root_cause,
                "resolution":             records[i].resolution,
                "channel":                 records[i].channel,
                "was_escalated":           records[i].was_escalated,
                "resolution_time_minutes": records[i].resolution_time_minutes,
                "satisfaction_score":      records[i].satisfaction_score,
                "timestamp":               records[i].timestamp,
            },
        )
        for i in range(len(records))
    ]

    await qdrant.upsert(collection_name=COLLECTION_NAME, points=points)


async def run_distillation() -> dict:
    """
    Main distillation pipeline.

    1. Read all transcripts from Azure Blob via TranscriptReader
    2. Distil each with Groq (concurrent, semaphore-limited)
    3. Embed + upsert to Qdrant in batches

    Returns:
        Summary dict with counts.
    """
    cfg         = get_config()
    ingest_cfg  = cfg.get("ingest", {}).get("distillation", {})
    concurrent  = ingest_cfg.get("concurrent_calls", CONCURRENT_CALLS)
    batch_size  = ingest_cfg.get("batch_size", BATCH_SIZE)

    groq_client = AsyncGroq(api_key=GROQ_API_KEY)
    qdrant      = AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None)
    reader      = TranscriptReader()
    semaphore   = asyncio.Semaphore(concurrent)

    await _ensure_collection(qdrant)

    # ── Load all transcripts from Azure Blob ──────────────────────────
    logger.info("Reading transcripts from Azure Blob...")
    transcripts = list(reader.iter_all())
    total       = len(transcripts)
    logger.info("Loaded {} transcripts. Starting distillation...", total)

    # ── Distil all concurrently ───────────────────────────────────────
    tasks   = [_call_groq(groq_client, t, semaphore) for t in transcripts]
    results = await asyncio.gather(*tasks)

    distilled = [r for r in results if r is not None]
    failed    = total - len(distilled)
    logger.info("Distilled {} | Failed {}", len(distilled), failed)

    # ── Upsert to Qdrant in batches ───────────────────────────────────
    for i in range(0, len(distilled), batch_size):
        batch = distilled[i : i + batch_size]
        await _upsert_batch(qdrant, batch, start_id=i)
        logger.info(
            "Upserted batch {}/{}",
            min(i + batch_size, len(distilled)), len(distilled),
        )

    summary = {
        "total_transcripts": total,
        "distilled":         len(distilled),
        "failed":            failed,
        "collection":        COLLECTION_NAME,
    }
    logger.info("Distillation complete: {}", summary)
    return summary