"""
distill_transcripts.py — Runner script for the distillation pipeline.

Reads all transcripts from Azure Blob, distils with Groq,
saves structured SOPs to Qdrant procedural memory.

Usage:
    python scripts/distill_transcripts.py

Run AFTER upload_to_azure.py and BEFORE rebuild_cag_cache.py.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import sys
from loguru import logger
from services.ingest_service.distiller import run_distillation


async def main():
    logger.info("=" * 60)
    logger.info("DISTILLATION PIPELINE STARTING")
    logger.info("=" * 60)

    try:
        summary = await run_distillation()

        logger.info("=" * 60)
        logger.info("DISTILLATION COMPLETE")
        logger.info("  Total transcripts : {}", summary["total_transcripts"])
        logger.info("  Successfully distilled : {}", summary["distilled"])
        logger.info("  Failed             : {}", summary["failed"])
        logger.info("  Saved to collection: {}", summary["collection"])
        logger.info("=" * 60)

        if summary["failed"] > 0:
            logger.warning(
                "{} transcripts failed distillation. Check logs above.",
                summary["failed"],
            )

    except Exception as exc:
        logger.error("Distillation pipeline crashed: {}", exc)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
