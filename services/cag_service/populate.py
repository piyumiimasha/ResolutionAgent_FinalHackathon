"""
cag_service/populate.py — Bridge between ingest pipeline and live CAG cache.

Thin wrapper that calls cag_populator.run_cag_population() and reports results.
Called by scripts/rebuild_cag_cache.py.

Also exposes clear_cache() for resetting the cache during development.
"""

import os
from loguru import logger

from qdrant_client import AsyncQdrantClient
from services.ingest_service.cag_populator import run_cag_population


async def populate_from_transcripts() -> dict:
    """
    Run full CAG population pipeline from distilled procedural memory.
    Delegates to ingest_service/cag_populator.py.

    Returns:
        Summary dict from cag_populator.run_cag_population().
    """
    logger.info("Starting CAG cache population from procedural memory...")
    summary = await run_cag_population()
    logger.info("CAG cache population complete: {}", summary)
    return summary


async def clear_cache() -> bool:
    """
    Delete all points in the CAG cache collection.
    Useful during development when re-seeding after threshold changes.

    Returns:
        True on success, False on failure.
    """
    try:
        qdrant = AsyncQdrantClient(
            url     = os.getenv("QDRANT_URL"),
            api_key = os.getenv("QDRANT_API_KEY") or None,
        )
        await qdrant.delete_collection("cag_cache")
        logger.info("CAG cache collection deleted.")
        return True
    except Exception as exc:
        logger.error("Failed to clear CAG cache: {}", exc)
        return False


async def get_cache_stats() -> dict:
    """
    Return stats about the current CAG cache.

    Returns:
        Dict with point count and collection info.
    """
    try:
        qdrant = AsyncQdrantClient(
            url     = os.getenv("QDRANT_URL"),
            api_key = os.getenv("QDRANT_API_KEY") or None,
        )
        info = await qdrant.get_collection("cag_cache")
        return {
            "points_count":  info.points_count,
            "vector_size":   info.config.params.vectors.size,
            "distance":      str(info.config.params.vectors.distance),
        }
    except Exception as exc:
        logger.error("Failed to get CAG cache stats: {}", exc)
        return {}