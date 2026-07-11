"""
rag_service/retriever.py — CRAG runbook retriever.
"""

import os
from loguru import logger
from dotenv import load_dotenv

from qdrant_client import AsyncQdrantClient
from infrastructure.config import get_config
from infrastructure.llm.embeddings import get_embedding

load_dotenv()

COLLECTION = "runbooks"


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


class CRAGRetriever:
    """Corrective RAG retriever for runbook chunks."""

    def __init__(self):
        self._qdrant = _get_qdrant()
        cfg          = get_config()
        rag_cfg      = cfg.get("rag", {})
        self._top_k                = rag_cfg.get("top_k", 3)
        self._sim_threshold        = rag_cfg.get("similarity_threshold", 0.70)
        self._validation_threshold = rag_cfg.get("validation_threshold", 0.60)

    async def retrieve(
        self,
        query:       str,
        ticket_type: int = 1,
        top_k:       int | None = None,
    ) -> list[dict]:
        k          = top_k or self._top_k
        raw_chunks = await self._vector_search(query, k * 2)

        if not raw_chunks:
            logger.warning("CRAG: No chunks found for: {:.60s}", query)
            return []

        validated = self._validate(raw_chunks)
        if not validated:
            logger.warning("CRAG: No chunks passed validation threshold {:.2f}", self._validation_threshold)
            return raw_chunks[:1]   # fallback: return top result anyway

        return validated[:k]

    async def _vector_search(self, query: str, limit: int) -> list[dict]:
        try:
            embedding = await get_embedding(query)
            raw       = await self._qdrant.query_points(
                collection_name = COLLECTION,
                query           = embedding,
                limit           = limit,
                with_payload    = True,
            )
            points = _extract_points(raw)
            return [
                {
                    "source":      r.payload.get("source", "unknown"),
                    "text":        r.payload.get("text", ""),
                    "heading":     r.payload.get("heading", ""),
                    "chunk_index": r.payload.get("chunk_index", 0),
                    "score":       round(r.score, 3),
                }
                for r in points
            ]
        except Exception as exc:
            logger.error("CRAG vector search failed: {}", exc)
            return []

    def _validate(self, chunks: list[dict]) -> list[dict]:
        return [c for c in chunks if c.get("score", 0) >= self._validation_threshold]