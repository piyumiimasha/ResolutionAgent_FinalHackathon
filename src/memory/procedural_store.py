"""
procedural_store.py — Procedural Memory Store.

Storage : Qdrant collection `procedural_memory`
Purpose : SOPs distilled from the 1050 historical transcripts.
          Written once during ingest pipeline. Read by L2 during live investigations.
Lifetime: Permanent. Rebuilt by running distill_transcripts.py.

Called by:
  - ingest_service/distiller.py   → (writes) stores distilled SOPs after pipeline
  - memory_ops.Recaller           → search_similar() before L2 investigates
  - l2_investigator.py            → search_similar() directly

Qdrant collection: procedural_memory
  vector: 1536-dim embedding of "problem + root_cause + resolution"
  payload fields:
    transcript_id, ticket_type_int, ticket_type_str,
    problem, root_cause, resolution,
    channel, was_escalated, resolution_time_minutes,
    satisfaction_score, timestamp
"""

import os
from loguru import logger
from dotenv import load_dotenv

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
from infrastructure.llm.embeddings import get_embedding

load_dotenv()

COLLECTION_NAME = "procedural_memory"
QDRANT_URL      = os.getenv("QDRANT_URL")
QDRANT_API_KEY  = os.getenv("QDRANT_API_KEY")


def _get_qdrant() -> AsyncQdrantClient:
    return AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)


class ProceduralStore:
    """
    Procedural Memory — historical SOPs from 1050 distilled transcripts.

    Read-only during live app operation.
    Written only by ingest pipeline (distiller.py).

    Usage:
        store = ProceduralStore()
        sops = await store.search_similar("Redis OOM on prod-cache-01", ticket_type=4)
    """

    def __init__(self):
        self._qdrant = _get_qdrant()

    async def search_similar(
        self,
        problem: str,
        ticket_type: int,
        top_k: int = 3,
        filter_by_type: bool = True,
    ) -> list[dict]:
        """
        Semantic search for similar past incidents in procedural memory.

        Args:
            problem:         The current incident description.
            ticket_type:     Ticket type int (1-4) to narrow the search.
            top_k:           Number of results to return (default 3).
            filter_by_type:  If True, only search within the same ticket type.

        Returns:
            List of payload dicts with problem, root_cause, resolution fields.
        """
        try:
            embedding = await get_embedding(problem)

            # Optionally filter by ticket type for more relevant results
            search_filter = None
            if filter_by_type:
                search_filter = Filter(
                    must=[FieldCondition(
                        key   = "ticket_type_int",
                        match = MatchValue(value=ticket_type),
                    )]
                )

            results = await self._qdrant.search(
                collection_name = COLLECTION_NAME,
                query_vector    = embedding,
                query_filter    = search_filter,
                limit           = top_k,
                with_payload    = True,
            )

            if not results:
                # Fallback: search without type filter if no results found
                if filter_by_type:
                    logger.debug(
                        "No type-filtered results for ticket_type={}, retrying without filter",
                        ticket_type,
                    )
                    return await self.search_similar(
                        problem, ticket_type, top_k, filter_by_type=False
                    )
                return []

            return [
                {
                    "transcript_id":           r.payload.get("transcript_id"),
                    "ticket_type_str":         r.payload.get("ticket_type_str"),
                    "problem":                 r.payload.get("problem"),
                    "root_cause":              r.payload.get("root_cause"),
                    "resolution":              r.payload.get("resolution"),
                    "was_escalated":           r.payload.get("was_escalated"),
                    "resolution_time_minutes": r.payload.get("resolution_time_minutes"),
                    "satisfaction_score":      r.payload.get("satisfaction_score"),
                    "similarity_score":        round(r.score, 3),
                }
                for r in results
            ]

        except Exception as exc:
            logger.error("ProceduralStore.search_similar failed: {}", exc)
            return []

    async def count(self) -> int:
        """
        Return total number of SOPs in the collection.
        Useful for verifying the ingest pipeline ran successfully.
        """
        try:
            info = await self._qdrant.get_collection(COLLECTION_NAME)
            return info.points_count or 0
        except Exception as exc:
            logger.error("ProceduralStore.count failed: {}", exc)
            return 0

    async def search_by_resolution_pattern(
        self,
        resolution_keyword: str,
        top_k: int = 5,
    ) -> list[dict]:
        """
        Search procedural memory by a keyword in the resolution field.
        Useful for CAG population — find all records resolved by a specific action.

        Args:
            resolution_keyword: e.g. "maxmemory", "credential reset", "restart"
            top_k:              Max results.

        Returns:
            List of matching SOP dicts.
        """
        try:
            embedding = await get_embedding(resolution_keyword)
            results   = await self._qdrant.search(
                collection_name = COLLECTION_NAME,
                query_vector    = embedding,
                limit           = top_k,
                with_payload    = True,
            )
            return [r.payload for r in results]
        except Exception as exc:
            logger.error("ProceduralStore.search_by_resolution_pattern failed: {}", exc)
            return []