"""
episodic_store.py — Episodic Memory Store.

Storage : Supabase table `episodes`
Purpose : Full snapshots of resolved incidents. Every closed ticket
          gets saved here. Used by L2 for structured past-incident lookup
          and by graders as an audit trail.
Lifetime: Permanent.

Called by:
  - orchestrator.py write_back_node → save_episode() after L3 resolves
  - memory_ops.Recaller             → get_similar_episodes() before L2

Supabase table schema:
  id                      SERIAL PRIMARY KEY
  session_id              TEXT UNIQUE NOT NULL
  ticket_id               TEXT NOT NULL
  ticket_type             INTEGER
  resolution              TEXT
  resolved                BOOLEAN DEFAULT FALSE
  resolution_time_minutes INTEGER
  turns                   JSONB       -- full conversation as JSON array
  created_at              TIMESTAMPTZ DEFAULT NOW()
"""

import os
import json
from datetime import datetime, timezone

from supabase import create_client, Client
from loguru import logger
from dotenv import load_dotenv

from memory.schemas import Episode

load_dotenv()

TABLE = "episodes"


def _get_supabase() -> Client:
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


class EpisodicStore:
    """
    Episodic Memory — full incident session snapshots.

    Usage:
        ep = EpisodicStore()
        await ep.save_episode(episode)
        past = await ep.get_similar_episodes(ticket_type=4, severity="critical")
    """

    def __init__(self):
        self._sb = _get_supabase()

    async def save_episode(self, episode: Episode) -> None:
        """
        Save a full incident episode after resolution.
        Called by orchestrator write_back_node.

        Args:
            episode: Episode dict with all incident fields.
        """
        try:
            self._sb.table(TABLE).upsert({
                "session_id":              episode["session_id"],
                "ticket_id":               episode["ticket_id"],
                "ticket_type":             episode.get("ticket_type"),
                "resolution":              episode.get("resolution", ""),
                "resolved":                episode.get("resolved", False),
                "resolution_time_minutes": episode.get("resolution_time_minutes"),
                "turns":                   json.dumps(episode.get("turns", [])),
                "created_at":              datetime.now(timezone.utc).isoformat(),
            }, on_conflict="session_id").execute()

            logger.info(
                "EpisodicStore saved episode for ticket {} (resolved={})",
                episode["ticket_id"], episode.get("resolved"),
            )

        except Exception as exc:
            logger.error("EpisodicStore.save_episode failed for {}: {}", episode.get("ticket_id"), exc)

    async def get_similar_episodes(
        self,
        ticket_type: int,
        limit: int = 3,
        resolved_only: bool = True,
    ) -> list[Episode]:
        """
        Retrieve past episodes matching the given ticket type.
        L2 uses this for structured historical lookup.

        Args:
            ticket_type:   Filter by ticket type 1-4.
            limit:         Max episodes to return (default 3).
            resolved_only: Only return resolved incidents (default True).

        Returns:
            List of Episode dicts, most recent first.
        """
        try:
            query = (
                self._sb.table(TABLE)
                .select("session_id, ticket_id, ticket_type, resolution, resolved, resolution_time_minutes, created_at")
                .eq("ticket_type", ticket_type)
                .order("created_at", desc=True)
                .limit(limit)
            )
            if resolved_only:
                query = query.eq("resolved", True)

            result = query.execute()

            return [
                Episode(
                    session_id              = r["session_id"],
                    ticket_id               = r["ticket_id"],
                    ticket_type             = r.get("ticket_type"),
                    turns                   = [],       # omit turns for summary lookup
                    resolution              = r.get("resolution"),
                    resolved                = r.get("resolved", False),
                    resolution_time_minutes = r.get("resolution_time_minutes"),
                    created_at              = r.get("created_at", ""),
                )
                for r in (result.data or [])
            ]

        except Exception as exc:
            logger.error("EpisodicStore.get_similar_episodes failed: {}", exc)
            return []

    async def get_by_ticket_id(self, ticket_id: str) -> Episode | None:
        """
        Fetch a specific episode by ticket ID.

        Args:
            ticket_id: The ticket ID to look up.

        Returns:
            Episode dict or None if not found.
        """
        try:
            result = (
                self._sb.table(TABLE)
                .select("*")
                .eq("ticket_id", ticket_id)
                .single()
                .execute()
            )
            if not result.data:
                return None
            r = result.data
            return Episode(
                session_id              = r["session_id"],
                ticket_id               = r["ticket_id"],
                ticket_type             = r.get("ticket_type"),
                turns                   = json.loads(r.get("turns", "[]")),
                resolution              = r.get("resolution"),
                resolved                = r.get("resolved", False),
                resolution_time_minutes = r.get("resolution_time_minutes"),
                created_at              = r.get("created_at", ""),
            )
        except Exception as exc:
            logger.error("EpisodicStore.get_by_ticket_id failed for {}: {}", ticket_id, exc)
            return None