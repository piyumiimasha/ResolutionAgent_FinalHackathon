"""
rebuild_cag_cache.py — Runner script to populate/refresh the CAG cache.

Scans procedural memory for high-frequency Type 1 patterns
and seeds them into the CAG cache collection in Qdrant.

Usage:
    python scripts/rebuild_cag_cache.py

Run AFTER distill_transcripts.py.
Can be re-run anytime to refresh the cache (e.g. after new transcripts added).
"""

import asyncio
import sys
from loguru import logger
from src.services.ingest_service.cag_populator import run_cag_population


async def main():
    logger.info("=" * 60)
    logger.info("CAG CACHE POPULATION STARTING")
    logger.info("=" * 60)

    try:
        summary = await run_cag_population()

        logger.info("=" * 60)
        logger.info("CAG POPULATION COMPLETE")
        logger.info("  Type 1 records scanned : {}", summary["total_type1_records"])
        logger.info("  Clusters found         : {}", summary["clusters_found"])
        logger.info("  Clusters seeded to CAG : {}", summary["clusters_seeded"])
        logger.info("  Clusters skipped       : {}", summary["clusters_skipped"])
        logger.info("  Min frequency threshold: {}", summary["min_frequency"])
        logger.info("=" * 60)

    except Exception as exc:
        logger.error("CAG population crashed: {}", exc)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
