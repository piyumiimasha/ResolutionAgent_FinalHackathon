#!/usr/bin/env python3
"""
distill_stage1.py — Groq extraction only.

Reads transcripts from Azure Blob, sends to Groq, writes results
to disk immediately after each record — safe to interrupt at any time.

Re-running this script automatically skips already-extracted transcripts.

Usage:
    python scripts/distill_stage1.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import asyncio
from loguru import logger
from services.ingest_service.distiller import run_extraction_only


async def main():
    logger.info("=" * 60)
    logger.info("DISTILLATION STAGE 1 — Groq extraction")
    logger.info("Results saved to disk after EVERY record.")
    logger.info("Safe to interrupt — re-run to resume from where it stopped.")
    logger.info("=" * 60)

    summary = await run_extraction_only()

    logger.info("=" * 60)
    logger.info("STAGE 1 COMPLETE")
    logger.info("  Total transcripts   : {}", summary["total"])
    logger.info("  Already done        : {}", summary["already_done"])
    logger.info("  Newly extracted     : {}", summary["newly_extracted"])
    logger.info("  Failed              : {}", summary["failed"])
    logger.info("  Total extracted     : {}", summary["total_extracted"])
    logger.info("  Output file         : {}", summary["output"])
    logger.info("=" * 60)

    if summary["failed"] > 0:
        logger.warning(
            "{} transcripts failed. Re-run this script to retry them.",
            summary["failed"],
        )
    if summary["total_extracted"] == summary["total"]:
        logger.info("All transcripts extracted! Run distill_stage2.py next.")


if __name__ == "__main__":
    asyncio.run(main())