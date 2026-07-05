"""
distiller.py — LLM distillation pipeline.

Reads every transcript from Azure Blob via TranscriptReader,
sends each conversation to Groq LLM, extracts:
  - Problem
  - Root Cause
  - Resolution

Saves all 1050 structured records to Qdrant as Procedural Memory.

Two-stage pipeline to handle Groq rate limits:
  Stage 1: run_extraction_only()     → Groq extraction, saves to disk immediately
  Stage 2: run_embeddings_from_dir() → embed + upsert to Qdrant

Run via scripts:
    python scripts/distill_stage1.py   ← Groq extraction
    python scripts/distill_stage2.py   ← embeddings + Qdrant
"""

import json
import asyncio
from dataclasses import dataclass, asdict
from pathlib import Path

from groq import AsyncGroq
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance
from loguru import logger
from dotenv import load_dotenv

import os
from services.ingest_service.transcript_reader import TranscriptReader, ParsedTranscript
from infastructure.llm.embeddings import get_embeddings_batch
from infastructure.config import get_config, get_embedding_dimensions

load_dotenv()

GROQ_API_KEY        = os.getenv("GROQ_API_KEY")
QDRANT_URL          = os.getenv("QDRANT_URL")
QDRANT_API_KEY      = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME     = "procedural_memory"
DISTILL_MODEL       = "llama-3.1-8b-instant"
CONCURRENT_CALLS    = 1
BATCH_SIZE          = 10
GROQ_MAX_RETRIES    = 5
GROQ_BASE_DELAY     = 2.0
GROQ_CALL_DELAY     = 12.0


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

    @classmethod
    def from_dict(cls, d: dict) -> "DistilledRecord":
        return cls(
            transcript_id           = d.get("transcript_id", ""),
            ticket_type_int         = d.get("ticket_type_int", 0),
            ticket_type_str         = d.get("ticket_type_str", ""),
            problem                 = d.get("problem", ""),
            root_cause              = d.get("root_cause", "Unknown"),
            resolution              = d.get("resolution", "Unresolved"),
            channel                 = d.get("channel", ""),
            was_escalated           = d.get("was_escalated", False),
            resolution_time_minutes = d.get("resolution_time_minutes", 0),
            satisfaction_score      = d.get("satisfaction_score", 0),
            timestamp               = d.get("timestamp", ""),
        )


# ── Groq call ─────────────────────────────────────────────────────────────────

async def _call_groq(
    client:     AsyncGroq,
    transcript: ParsedTranscript,
    semaphore:  asyncio.Semaphore,
) -> DistilledRecord | None:
    """
    Send one conversation to Groq and extract structured knowledge.
    Retries on rate limit with exponential backoff.
    """
    async with semaphore:
        for attempt in range(1, GROQ_MAX_RETRIES + 1):
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
                raw       = response.choices[0].message.content.strip()
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
                logger.error("JSON parse error for {}: {}", transcript.transcript_id, exc)
                return None

            except Exception as exc:
                msg           = str(exc)
                is_rate_limit = "429" in msg or "rate limit" in msg.lower()

                if is_rate_limit and attempt < GROQ_MAX_RETRIES:
                    delay = GROQ_BASE_DELAY * (2 ** attempt)   # exponential backoff
                    logger.warning(
                        "Rate limit hit for {} — attempt {}/{}. Sleeping {:.0f}s...",
                        transcript.transcript_id, attempt, GROQ_MAX_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                logger.error(
                    "Groq failed for {} after {} attempt(s): {}",
                    transcript.transcript_id, attempt, exc,
                )
                return None

        return None


# ── Qdrant helpers ────────────────────────────────────────────────────────────

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
    """Embed and upsert a batch of distilled records into Qdrant."""
    texts   = [f"{r.problem} {r.root_cause} {r.resolution}" for r in records]
    vectors = await get_embeddings_batch(texts)

    points = [
        PointStruct(
            id      = start_id + i,
            vector  = vectors[i],
            payload = {
                "transcript_id":           records[i].transcript_id,
                "ticket_type_int":         records[i].ticket_type_int,
                "ticket_type_str":         records[i].ticket_type_str,
                "problem":                 records[i].problem,
                "root_cause":              records[i].root_cause,
                "resolution":              records[i].resolution,
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


# ── Stage 1: Groq extraction — writes to disk immediately ─────────────────────

async def run_extraction_only(out_dir: str = "data/distilled_groq") -> dict:
    """
    Stage 1 — Groq extraction only.

    KEY BEHAVIOUR: writes each record to disk immediately after Groq returns.
    If Groq rate-limits mid-run, already-extracted records are safe on disk.
    Re-running this function automatically skips already-extracted transcripts.

    Output files:
      data/distilled_groq/groq_extractions.jsonl   ← one JSON per line, appended live
      data/distilled_groq/groq_success_ids.json    ← list of completed transcript IDs
      data/distilled_groq/groq_failed_ids.json     ← list of failed transcript IDs

    Args:
        out_dir: Directory to write output files.

    Returns:
        Summary dict.
    """
    cfg        = get_config()
    ingest_cfg = cfg.get("ingest", {}).get("distillation", {})
    concurrent  = ingest_cfg.get("concurrent_calls", CONCURRENT_CALLS)
    call_delay  = ingest_cfg.get("groq_call_delay_seconds", GROQ_CALL_DELAY)

    groq_client = AsyncGroq(api_key=GROQ_API_KEY)
    reader      = TranscriptReader()
    semaphore   = asyncio.Semaphore(concurrent)

    out_path     = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    output_file  = out_path / "groq_extractions.jsonl"
    success_file = out_path / "groq_success_ids.json"
    failure_file = out_path / "groq_failed_ids.json"

    # ── Resume: load already-completed IDs ───────────────────────────
    already_done: set[str] = set()
    if success_file.exists():
        already_done = set(json.loads(success_file.read_text(encoding="utf-8")))
        logger.info(
            "Resume mode: {} transcripts already extracted — skipping them.",
            len(already_done),
        )

    # Load existing failed IDs (will be updated at the end)
    existing_failed: list[str] = []
    if failure_file.exists():
        existing_failed = json.loads(failure_file.read_text(encoding="utf-8"))

    # ── Load transcripts, skip already done ───────────────────────────
    all_transcripts = list(reader.iter_all())
    total           = len(all_transcripts)
    pending         = [t for t in all_transcripts if t.transcript_id not in already_done]

    logger.info(
        "Total: {} | Already done: {} | Remaining: {}",
        total, len(already_done), len(pending),
    )

    succeeded_ids: list[str] = []
    failed_ids:    list[str] = []
    count          = len(already_done)  # offset for progress display

    # ── Open JSONL file in APPEND mode ───────────────────────────────
    # This is the key change — we append one line per record immediately.
    with output_file.open("a", encoding="utf-8") as fh:
        for transcript in pending:
            count += 1
            result = await _call_groq(groq_client, transcript, semaphore)

            if result is not None:
                # ── Write immediately to disk ─────────────────────────
                fh.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
                fh.flush()      # force OS to write — don't buffer

                succeeded_ids.append(transcript.transcript_id)

                logger.info(
                    "[{}/{}] ✓ {}",
                    count, total, transcript.transcript_id,
                )
            else:
                failed_ids.append(transcript.transcript_id)
                logger.warning(
                    "[{}/{}] ✗ {} — failed, will retry on next run",
                    count, total, transcript.transcript_id,
                )

            # ── Update ID tracking files after every record ───────────
            # Merge with already_done so resume works correctly
            all_success = list(already_done) + succeeded_ids
            success_file.write_text(
                json.dumps(all_success, indent=2), encoding="utf-8"
            )

            # ── Paced delay between calls ─────────────────────────────
            if call_delay > 0 and count < total:
                await asyncio.sleep(call_delay)

    # ── Final failure file (merge with previous failures) ────────────
    all_failed = list(set(existing_failed + failed_ids))
    failure_file.write_text(json.dumps(all_failed, indent=2), encoding="utf-8")

    summary = {
        "total":          total,
        "already_done":   len(already_done),
        "newly_extracted":len(succeeded_ids),
        "failed":         len(failed_ids),
        "total_extracted":len(already_done) + len(succeeded_ids),
        "output":         str(output_file),
    }
    logger.info("Stage 1 complete: {}", summary)
    return summary


# ── Stage 2: Embed + upsert to Qdrant ────────────────────────────────────────

async def run_embeddings_from_dir(out_dir: str = "data/distilled_groq") -> dict:
    """
    Stage 2 — Read JSONL from disk, embed, upsert to Qdrant.

    Reads data/distilled_groq/groq_extractions.jsonl produced by Stage 1.
    Can be re-run safely — Qdrant upsert is idempotent.

    Args:
        out_dir: Directory containing groq_extractions.jsonl.

    Returns:
        Summary dict.
    """
    cfg        = get_config()
    batch_size = cfg.get("ingest", {}).get("distillation", {}).get("batch_size", BATCH_SIZE)

    qdrant     = AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None)
    await _ensure_collection(qdrant)

    input_file = Path(out_dir) / "groq_extractions.jsonl"
    if not input_file.exists():
        logger.error("Stage 2 input not found: {}", input_file)
        return {"error": "input_not_found", "path": str(input_file)}

    # ── Load all records from JSONL ───────────────────────────────────
    records: list[DistilledRecord] = []
    with input_file.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(DistilledRecord.from_dict(json.loads(line)))
            except Exception as exc:
                logger.warning("Skipping malformed JSONL line: {}", exc)

    total = len(records)
    logger.info("Stage 2: {} records loaded. Embedding + upserting to Qdrant...", total)

    saved = 0
    for i in range(0, total, batch_size):
        batch = records[i : i + batch_size]
        try:
            await _upsert_batch(qdrant, batch, start_id=i)
            saved += len(batch)
            logger.info(
                "Upserted {}/{} records",
                min(i + batch_size, total), total,
            )
        except Exception as exc:
            logger.error("Qdrant upsert failed at batch {}: {}", i, exc)
            return {
                "error":       "qdrant_upsert_failed",
                "batch_start": i,
                "saved_so_far":saved,
                "exception":   str(exc),
            }

    summary = {"total": total, "saved": saved, "collection": COLLECTION_NAME}
    logger.info("Stage 2 complete: {}", summary)
    return summary


# ── Full pipeline (single run, for when rate limits aren't an issue) ──────────

async def run_distillation() -> dict:
    """
    Combined pipeline — Stage 1 + Stage 2 in one call.
    Uses the same disk-backed Stage 1 so it's also resumable.
    """
    logger.info("Running full distillation pipeline (Stage 1 + Stage 2)...")
    s1 = await run_extraction_only()
    if s1.get("error"):
        return s1
    s2 = await run_embeddings_from_dir()
    return {**s1, **s2}