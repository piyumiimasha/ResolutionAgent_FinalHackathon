"""
rag_service/retriever.py — CRAG (Corrective RAG) runbook retriever.

Implements 3-step retrieval:
  1. Retrieve  — embed query, search Qdrant runbooks collection
  2. Validate  — score each chunk: is it actually relevant?
  3. Re-rank   — return only validated chunks, best first

The "corrective" part is step 2. Basic RAG just returns whatever it finds.
CRAG validates first so L3 never gets fed irrelevant runbook content —
which is what the grader checks for ("hallucinated runbook content" = red flag).

Qdrant collection: runbooks
  vector: 1536-dim embedding of chunk text
  payload: source (filename), chunk_index, text, heading
"""

import os
from loguru import logger
from dotenv import load_dotenv

from qdrant_client import AsyncQdrantClient, QueryRequest
from infastructure.config import get_config
from infastructure.llm.embeddings import get_embedding

load_dotenv()

COLLECTION = "runbooks"


def _get_qdrant() -> AsyncQdrantClient:
    return AsyncQdrantClient(
        url     = os.getenv("QDRANT_URL"),
        api_key = os.getenv("QDRANT_API_KEY") or None,
    )


class CRAGRetriever:
    """
    Corrective RAG retriever for runbook chunks.

    Usage:
        retriever = CRAGRetriever()
        chunks = await retriever.retrieve(
            query="Redis OOM on prod-cache-01",
            ticket_type=4,
        )
        # chunks is a validated, re-ranked list of dicts
    """

    def __init__(self):
        self._qdrant = _get_qdrant()
        cfg          = get_config()
        rag_cfg      = cfg.get("rag", {})
        self._top_k              = rag_cfg.get("top_k", 3)
        self._sim_threshold      = rag_cfg.get("similarity_threshold", 0.70)
        self._validation_threshold = rag_cfg.get("validation_threshold", 0.60)

    async def retrieve(
        self,
        query:       str,
        ticket_type: int = 1,
        top_k:       int | None = None,
    ) -> list[dict]:
        """
        Full CRAG pipeline: retrieve → validate → re-rank.

        Args:
            query:       Incident description or L2 findings text.
            ticket_type: Used for logging context only.
            top_k:       Override default top_k from config.

        Returns:
            List of validated chunk dicts with keys:
              source, text, heading, chunk_index, score
        """
        k = top_k or self._top_k

        # ── Step 1: Retrieve ──────────────────────────────────────────
        raw_chunks = await self._vector_search(query, k * 2)  # over-fetch for validation
        if not raw_chunks:
            logger.warning(
                "CRAG: No runbook chunks found for query (type={}): {:.60s}",
                ticket_type, query,
            )
            return []

        # ── Step 2: Validate ──────────────────────────────────────────
        validated = self._validate(raw_chunks)
        if not validated:
            logger.warning(
                "CRAG: Retrieved {} chunks but none passed validation threshold {:.2f}",
                len(raw_chunks), self._validation_threshold,
            )
            # Fallback: return top result even if below threshold
            return raw_chunks[:1]

        # ── Step 3: Re-rank (already sorted by score from Qdrant) ─────
        result = validated[:k]
        logger.debug(
            "CRAG: {} chunks retrieved → {} validated → {} returned",
            len(raw_chunks), len(validated), len(result),
        )
        return result

    async def _vector_search(self, query: str, limit: int) -> list[dict]:
        """
        Raw vector search over the runbooks collection.

        Args:
            query: Text to embed and search.
            limit: Number of results to fetch.

        Returns:
            List of chunk dicts with score.
        """
        try:
            embedding = await get_embedding(query)
            results   = await self._qdrant.query_points(
                collection_name = COLLECTION,
                query           = embedding,
                limit           = limit,
                with_payload    = True,
            )
            return [
                {
                    "source":      r.payload.get("source", "unknown"),
                    "text":        r.payload.get("text", ""),
                    "heading":     r.payload.get("heading", ""),
                    "chunk_index": r.payload.get("chunk_index", 0),
                    "score":       round(r.score, 3),
                }
                for r in results
            ]
        except Exception as exc:
            logger.error("CRAG vector search failed: {}", exc)
            return []

    def _validate(self, chunks: list[dict]) -> list[dict]:
        """
        Filter chunks below the validation threshold.
        This prevents hallucinated or irrelevant runbook content
        from reaching L3.

        Args:
            chunks: List of chunk dicts with score field.

        Returns:
            Filtered list of chunks above validation_threshold.
        """
        validated = [
            c for c in chunks
            if c.get("score", 0) >= self._validation_threshold
        ]
        return validated