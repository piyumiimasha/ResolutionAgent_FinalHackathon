"""
lt_store.py — Long-Term Memory Store.

Storage : Supabase pgvector table `lt_facts`
Purpose : Durable facts that persist across sessions — server patterns,
          team knowledge, config changes worth remembering forever.
Lifetime: Permanent. Never auto-deleted.

Called by:
  - memory_ops.Distiller → store_fact() after each resolved ticket
  - memory_ops.Recaller  → search() before L2 investigates

Supabase table schema (requires pgvector extension):
  id          SERIAL PRIMARY KEY
  fact        TEXT NOT NULL
  embedding   VECTOR(1536)
  confidence  FLOAT
  category    TEXT          -- server | service | employee | pattern | config
  source      TEXT          -- ticket_id this came from
  created_at  TIMESTAMPTZ DEFAULT NOW()
"""

import os
import json
from datetime import datetime, timezone

from supabase import create_client, Client
from loguru import logger
from dotenv import load_dotenv

from memory.schemas import MemoryFact
from infrastructure.llm.embeddings import get_embedding

load_dotenv()

TABLE              = "lt_facts"
SIMILARITY_THRESHOLD = float(os.getenv("LT_SIMILARITY_THRESHOLD", "0.75"))


def _get_supabase() -> Client:
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


class LTStore:
    """
    Long-Term Memory — semantic fact store backed by pgvector.

    Usage:
        lt = LTStore()
        await lt.store_fact(fact)
        facts = await lt.search("prod-cache-01 memory issues", top_k=5)
    """

    def __init__(self):
        self._sb = _get_supabase()

    async def store_fact(self, fact: MemoryFact) -> None:
        """
        Embed and store a durable fact in pgvector.

        Args:
            fact: MemoryFact dict with fact text, confidence, category, source.
        """
        try:
            embedding = await get_embedding(fact["fact"])
            self._sb.table(TABLE).insert({
                "fact":       fact["fact"],
                "embedding":  json.dumps(embedding),   # pgvector accepts JSON array
                "confidence": fact["confidence"],
                "category":   fact.get("category", "pattern"),
                "source":     fact.get("source", ""),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
            logger.debug("LTStore stored fact: {:.80s}...", fact["fact"])

        except Exception as exc:
            logger.error("LTStore.store_fact failed: {}", exc)

    async def search(self, query: str, top_k: int = 5) -> list[MemoryFact]:
        """
        Semantic search over long-term facts using pgvector cosine similarity.

        Args:
            query:  Natural language query e.g. "prod-cache-01 memory"
            top_k:  Number of top results to return.

        Returns:
            List of MemoryFact dicts ordered by relevance.
        """
        try:
            embedding = await get_embedding(query)
            embedding_str = json.dumps(embedding)

            # pgvector cosine similarity via Supabase RPC
            result = self._sb.rpc(
                "match_lt_facts",
                {
                    "query_embedding": embedding_str,
                    "match_threshold": SIMILARITY_THRESHOLD,
                    "match_count":     top_k,
                },
            ).execute()

            return [
                MemoryFact(
                    fact=r["fact"],
                    confidence=r.get("confidence", 0.5),
                    source=r.get("source", ""),
                    category=r.get("category", "pattern"),
                    created_at=r.get("created_at", ""),
                )
                for r in (result.data or [])
            ]

        except Exception as exc:
            logger.error("LTStore.search failed for query '{}': {}", query[:50], exc)
            return []

    async def store_many(self, facts: list[MemoryFact]) -> None:
        """Batch store multiple facts. Called by Distiller after a session."""
        for fact in facts:
            await self.store_fact(fact)