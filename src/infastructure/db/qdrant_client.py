"""
db/qdrant_client.py — Qdrant async client singleton.

Single place where QdrantClient is created and configured.
Used by: procedural_store, lt_store, cag_service, rag_service.

Also exposes:
  - ensure_collections() — creates all required collections on startup
  - check_connection()   — health check for API /health endpoint
"""

import os
from functools import lru_cache

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import VectorParams, Distance, CollectionInfo
from loguru import logger
from dotenv import load_dotenv

from infastructure.config import get_config, get_embedding_dimensions

load_dotenv()


@lru_cache(maxsize=1)
def get_qdrant() -> AsyncQdrantClient:
    """
    Returns the singleton AsyncQdrantClient.

    Raises:
        EnvironmentError: If QDRANT_URL is not set.
    """
    url = os.getenv("QDRANT_URL", "")
    key = os.getenv("QDRANT_API_KEY", "")

    if not url:
        raise EnvironmentError("QDRANT_URL not set in .env")

    client = AsyncQdrantClient(
        url     = url,
        api_key = key or None,    # None = no auth (local Qdrant)
    )
    logger.debug("Qdrant client created (url={}...)", url[:30])
    return client


async def ensure_collections() -> None:
    """
    Create all required Qdrant collections if they don't exist.
    Called once at app startup (api/main.py lifespan).

    Collections:
      - procedural_memory  (SOPs from 1050 transcripts)
      - cag_cache          (high-frequency FAQ answers)
      - runbooks           (chunked runbook markdown)
    """
    cfg        = get_config()
    qdrant     = get_qdrant()
    dimensions = get_embedding_dimensions()
    collections_cfg = cfg.get("qdrant", {}).get("collections", {})

    existing = {c.name for c in (await qdrant.get_collections()).collections}

    for col_key, col_cfg in collections_cfg.items():
        name = col_cfg["name"]
        if name not in existing:
            distance_str = col_cfg.get("distance", "Cosine").upper()
            distance     = getattr(Distance, distance_str, Distance.COSINE)
            vector_size  = col_cfg.get("vector_size", dimensions)

            await qdrant.create_collection(
                collection_name = name,
                vectors_config  = VectorParams(size=vector_size, distance=distance),
            )
            logger.info(
                "Created Qdrant collection '{}' (size={} distance={})",
                name, vector_size, distance_str,
            )
        else:
            logger.debug("Qdrant collection '{}' already exists — skipping", name)


async def check_connection() -> bool:
    """
    Health check — verifies Qdrant is reachable.

    Returns:
        True if connected, False otherwise.
    """
    try:
        qdrant = get_qdrant()
        await qdrant.get_collections()
        logger.debug("Qdrant connection OK")
        return True
    except Exception as exc:
        logger.error("Qdrant connection failed: {}", exc)
        return False


async def get_collection_info(name: str) -> CollectionInfo | None:
    """
    Get info (point count, vector config) for a collection.

    Args:
        name: Collection name.

    Returns:
        CollectionInfo or None if not found.
    """
    try:
        qdrant = get_qdrant()
        return await qdrant.get_collection(name)
    except Exception:
        return None