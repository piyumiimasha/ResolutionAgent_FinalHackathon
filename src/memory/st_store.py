"""
st_store.py — Short-Term Memory Store.

Storage : Supabase table `st_memory`
Purpose : Ring buffer of the last N turns of the current active conversation.
Lifetime: Cleared after ticket resolves or session expires.

Called by:
  - memory_ops.Recaller  → get_recent() before every agent LLM call
  - orchestrator.py      → add_turn() after every message
  - orchestrator.py      → clear() after ticket closes

Supabase table schema:
  id          SERIAL PRIMARY KEY
  ticket_id   TEXT NOT NULL
  role        TEXT NOT NULL
  content     TEXT NOT NULL
  timestamp   TIMESTAMPTZ NOT NULL
  created_at  TIMESTAMPTZ DEFAULT NOW()
"""

import os
from datetime import datetime, timezone

from supabase import create_client, Client
from loguru import logger
from dotenv import load_dotenv

from memory.schemas import ConversationTurn

load_dotenv()

TABLE    = "st_memory"
MAX_TURNS = int(os.getenv("ST_MAX_TURNS", "20"))   # configurable via param.yaml / env


def _get_supabase() -> Client:
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


class STStore:
    """
    Short-Term Memory — ring buffer per ticket_id.

    Usage:
        st = STStore()
        await st.add_turn(ticket_id, "user", "Redis is OOM on prod-cache-01")
        turns = await st.get_recent(ticket_id, n=10)
    """

    def __init__(self):
        self._sb = _get_supabase()

    async def add_turn(self, ticket_id: str, role: str, content: str) -> None:
        """
        Append one turn to short-term memory.
        Automatically trims oldest turns if count exceeds MAX_TURNS.

        Args:
            ticket_id: Active ticket ID.
            role:      "user" | "agent" | "l1_triage" | "l2_investigator" | "l3_resolver"
            content:   Message text.
        """
        try:
            now = datetime.now(timezone.utc).isoformat()
            self._sb.table(TABLE).insert({
                "ticket_id": ticket_id,
                "role":      role,
                "content":   content,
                "timestamp": now,
            }).execute()

            await self._trim(ticket_id)

        except Exception as exc:
            logger.error("STStore.add_turn failed for ticket {}: {}", ticket_id, exc)

    async def get_recent(self, ticket_id: str, n: int = 10) -> list[ConversationTurn]:
        """
        Fetch the last N turns for a ticket, ordered oldest → newest.

        Args:
            ticket_id: Ticket ID to fetch turns for.
            n:         Number of recent turns to return (default 10).

        Returns:
            List of ConversationTurn dicts.
        """
        try:
            result = (
                self._sb.table(TABLE)
                .select("role, content, timestamp, ticket_id")
                .eq("ticket_id", ticket_id)
                .order("timestamp", desc=True)
                .limit(n)
                .execute()
            )
            # Reverse so oldest is first (chronological order for prompt)
            turns = list(reversed(result.data or []))
            return [
                ConversationTurn(
                    role=t["role"],
                    content=t["content"],
                    timestamp=t["timestamp"],
                    ticket_id=t["ticket_id"],
                )
                for t in turns
            ]
        except Exception as exc:
            logger.error("STStore.get_recent failed for ticket {}: {}", ticket_id, exc)
            return []

    async def clear(self, ticket_id: str) -> None:
        """
        Delete all ST memory for a ticket after it resolves.

        Args:
            ticket_id: Ticket to clear.
        """
        try:
            self._sb.table(TABLE).delete().eq("ticket_id", ticket_id).execute()
            logger.debug("STStore cleared for ticket {}", ticket_id)
        except Exception as exc:
            logger.error("STStore.clear failed for ticket {}: {}", ticket_id, exc)

    async def _trim(self, ticket_id: str) -> None:
        """
        Keep only the most recent MAX_TURNS rows for this ticket.
        Deletes oldest rows beyond the limit.
        """
        try:
            # Count current rows
            count_result = (
                self._sb.table(TABLE)
                .select("id", count="exact")
                .eq("ticket_id", ticket_id)
                .execute()
            )
            count = count_result.count or 0

            if count > MAX_TURNS:
                # Fetch IDs of oldest rows to delete
                oldest = (
                    self._sb.table(TABLE)
                    .select("id")
                    .eq("ticket_id", ticket_id)
                    .order("timestamp", desc=False)
                    .limit(count - MAX_TURNS)
                    .execute()
                )
                ids_to_delete = [r["id"] for r in (oldest.data or [])]
                if ids_to_delete:
                    self._sb.table(TABLE).delete().in_("id", ids_to_delete).execute()

        except Exception as exc:
            logger.warning("STStore._trim failed for ticket {}: {}", ticket_id, exc)