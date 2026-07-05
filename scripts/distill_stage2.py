#!/usr/bin/env python3
"""
distill_stage2.py — Embed + upsert to Qdrant.

Reads data/distilled_groq/groq_extractions.jsonl produced by Stage 1,
embeds each record via OpenRouter, upserts to Qdrant procedural_memory.

Run AFTER distill_stage1.py is complete (or partially complete).
Can be re-run safely — Qdrant upsert is idempotent.

Usage:
    python scripts/distill_stage2.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import asyncio
from loguru import logger
from services.ingest_service.distiller import run_embeddings_from_dir


async def main():
    logger.info("=" * 60)
    logger.info("DISTILLATION STAGE 2 — Embeddings + Qdrant upsert")
    logger.info("=" * 60)

    summary = await run_embeddings_from_dir()

    if summary.get("error"):
        logger.error("Stage 2 failed: {}", summary)
        return

    logger.info("=" * 60)
    logger.info("STAGE 2 COMPLETE")
    logger.info("  Records processed   : {}", summary["total"])
    logger.info("  Saved to Qdrant     : {}", summary["saved"])
    logger.info("  Collection          : {}", summary["collection"])
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())