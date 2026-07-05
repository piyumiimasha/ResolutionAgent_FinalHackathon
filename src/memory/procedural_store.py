"""
procedural_store.py — Procedural Memory Store.

Storage : Qdrant collection `procedural_memory`
Purpose : SOPs distilled from the 1050 historical transcripts.
          Written once during ingest pipeline. Read by L2 during live investigations.
Lifetime: Permanent. Rebuilt by running distill_transcripts.py.
"""

import os
from loguru import logger
from dotenv import load_dotenv

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
from infastructure.llm.embeddings import get_embedding

load_dotenv()

COLLECTION_NAME = "procedural_memory"
QDRANT_URL      = os.getenv("QDRANT_URL")
QDRANT_API_KEY  = os.getenv("QDRANT_API_KEY")


def _get_qdrant() -> AsyncQdrantClient:
    return AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)


def _extract_points(raw) -> list:
    """
    Safely extract a list of ScoredPoint from whatever query_points returns.
    Handles:
      - QueryResponse object with .points  (qdrant-client >= 1.9)
      - tuple (points_list, next_offset)   (some intermediate versions)
      - plain list                         (older versions)
    """
    if isinstance(raw, tuple):
        return list(raw[0])
    if hasattr(raw, "points"):
        return list(raw.points)
    return list(raw)


class ProceduralStore:
    """
    Procedural Memory — historical SOPs from 1050 distilled transcripts.
    Read-only during live app. Written only by ingest pipeline.
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
        """
        try:
            embedding = await get_embedding(problem)

            search_filter = None
            if filter_by_type:
                search_filter = Filter(
                    must=[FieldCondition(
                        key   = "ticket_type_int",
                        match = MatchValue(value=ticket_type),
                    )]
                )

            raw    = await self._qdrant.query_points(
                collection_name = COLLECTION_NAME,
                query           = embedding,
                query_filter    = search_filter,
                limit           = top_k,
                with_payload    = True,
            )
            points = _extract_points(raw)

            if not points:
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
                for r in points
            ]

        except Exception as exc:
            logger.error("ProceduralStore.search_similar failed: {}", exc)
            return []

    async def count(self) -> int:
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
        try:
            embedding = await get_embedding(resolution_keyword)
            raw       = await self._qdrant.query_points(
                collection_name = COLLECTION_NAME,
                query           = embedding,
                limit           = top_k,
                with_payload    = True,
            )
            points = _extract_points(raw)
            return [r.payload for r in points]
        except Exception as exc:
            logger.error("ProceduralStore.search_by_resolution_pattern failed: {}", exc)
            return []