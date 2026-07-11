"""
cag_service/cache.py — CAG cache store backed by Qdrant.
"""

import os
from loguru import logger
from dotenv import load_dotenv

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct
from infrastructure.config import get_config
from infrastructure.llm.embeddings import get_embedding

load_dotenv()

COLLECTION = "cag_cache"


def _get_qdrant() -> AsyncQdrantClient:
    return AsyncQdrantClient(
        url     = os.getenv("QDRANT_URL"),
        api_key = os.getenv("QDRANT_API_KEY") or None,
    )


def _extract_points(raw) -> list:
    """Safely extract points regardless of qdrant-client version."""
    if isinstance(raw, tuple):
        return list(raw[0])
    if hasattr(raw, "points"):
        return list(raw.points)
    return list(raw)


class CAGCache:
    """Cache-Augmented Generation store."""

    def __init__(self):
        self._qdrant    = _get_qdrant()
        cfg             = get_config()
        self._threshold = cfg.get("cag", {}).get("similarity_threshold", 0.45)
        self._top_k     = cfg.get("cag", {}).get("top_k", 1)

    async def get(self, query: str) -> str | None:
        try:
            embedding = await get_embedding(query)
            raw       = await self._qdrant.query_points(
                collection_name = COLLECTION,
                query           = embedding,
                limit           = self._top_k,
                with_payload    = True,
            )
            points = _extract_points(raw)

            if not points:
                return None

            top   = points[0]
            score = top.score

            if score >= self._threshold:
                logger.debug(
                    "CAG HIT (score={:.3f} >= {:.3f}): {:.60s}",
                    score, self._threshold, query,
                )
                return top.payload.get("answer", "")

            logger.debug(
                "CAG MISS (score={:.3f} < {:.3f}): {:.60s}",
                score, self._threshold, query,
            )
            return None

        except Exception as exc:
            logger.error("CAGCache.get failed: {}", exc)
            return None

    async def set(self, query: str, answer: str) -> bool:
        try:
            embedding = await get_embedding(answer)   # embed answer for consistency
            import hashlib
            point_id = int(hashlib.md5(query.encode()).hexdigest()[:8], 16)

            await self._qdrant.upsert(
                collection_name = COLLECTION,
                points = [PointStruct(
                    id      = point_id,
                    vector  = embedding,
                    payload = {"query": query, "answer": answer, "source": "write_back"},
                )],
            )
            return True
        except Exception as exc:
            logger.error("CAGCache.set failed: {}", exc)
            return False