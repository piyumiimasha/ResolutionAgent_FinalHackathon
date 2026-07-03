"""
cag_service/cache.py — CAG cache store backed by Qdrant.

Stores question-answer pairs as vectors.
Queried by router.py on every incoming ticket.
Written by orchestrator write_back_node after L3 resolves a new incident.
Also seeded by cag_populator.py during ingest pipeline.

Qdrant collection: cag_cache
"""

import os
from loguru import logger
from dotenv import load_dotenv

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct
from infrastructure.config import get_config
from infrastructure.llm.embeddings import get_embedding
from services.cag_service.matcher import CAGMatcher

load_dotenv()

COLLECTION = "cag_cache"


def _get_qdrant() -> AsyncQdrantClient:
    return AsyncQdrantClient(
        url     = os.getenv("QDRANT_URL"),
        api_key = os.getenv("QDRANT_API_KEY") or None,
    )


class CAGCache:
    """
    Cache-Augmented Generation store.

    get() — semantic lookup: is this ticket a known FAQ?
    set() — write a new resolution to cache after L3 resolves it

    Usage:
        cache = CAGCache()
        answer = await cache.get("How do I reset my VPN?")
        if answer:
            return answer  # fast-path, skip swarm
        else:
            # run swarm, then:
            await cache.set("How do I reset my VPN?", "Go to Settings > VPN > Reset...")
    """

    def __init__(self):
        self._qdrant  = _get_qdrant()
        self._matcher = CAGMatcher()
        cfg           = get_config()
        self._threshold = cfg.get("cag", {}).get("similarity_threshold", 0.82)
        self._top_k     = cfg.get("cag", {}).get("top_k", 1)

    async def get(self, query: str) -> str | None:
        """
        Semantic cache lookup for an incoming ticket.

        Args:
            query: Incoming ticket text.

        Returns:
            Cached answer string if hit, None if miss.
        """
        try:
            embedding = await get_embedding(query)
            results   = await self._qdrant.search(
                collection_name = COLLECTION,
                query_vector    = embedding,
                limit           = self._top_k,
                with_payload    = True,
            )

            if not results:
                return None

            top = results[0]
            score = top.score

            if score >= self._threshold:
                answer = top.payload.get("answer", "")
                logger.debug(
                    "CAG cache HIT (score={:.3f} >= {:.3f}): {:.60s}",
                    score, self._threshold, query,
                )
                return answer

            logger.debug(
                "CAG cache MISS (score={:.3f} < {:.3f}): {:.60s}",
                score, self._threshold, query,
            )
            return None

        except Exception as exc:
            logger.error("CAGCache.get failed: {}", exc)
            return None

    async def set(self, query: str, answer: str) -> bool:
        """
        Write a new resolution to the CAG cache.
        Called by write_back_node after every resolved ticket.

        Args:
            query:  The original ticket text.
            answer: The resolution text from L3.

        Returns:
            True on success, False on failure.
        """
        try:
            embedding = await get_embedding(query)

            # Use a hash-based ID to allow deduplication
            import hashlib
            point_id = int(hashlib.md5(query.encode()).hexdigest()[:8], 16)

            await self._qdrant.upsert(
                collection_name = COLLECTION,
                points = [PointStruct(
                    id      = point_id,
                    vector  = embedding,
                    payload = {
                        "query":   query,
                        "answer":  answer,
                        "source":  "write_back",
                    },
                )],
            )
            logger.debug("CAGCache.set — cached resolution for: {:.60s}", query)
            return True

        except Exception as exc:
            logger.error("CAGCache.set failed: {}", exc)
            return False