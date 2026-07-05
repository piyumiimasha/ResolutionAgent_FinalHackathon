"""
schemas.py — Data shapes used across all 4 memory tiers.
Every store imports from here. Nothing defines its own types.
"""

from typing import TypedDict, Optional
from dataclasses import dataclass, field
from datetime import datetime


class ConversationTurn(TypedDict):
    """One message turn in an active conversation."""
    role:       str        # "user" | "agent" | "l1_triage" | "l2_investigator" | "l3_resolver"
    content:    str        # message text
    timestamp:  str        # ISO string
    ticket_id:  str        # which ticket this turn belongs to


class MemoryFact(TypedDict):
    """A durable fact extracted from a conversation for long-term storage."""
    fact:        str       # e.g. "prod-cache-01 has recurring OOM every ~30 days"
    confidence:  float     # 0.0 - 1.0, set by Distiller LLM
    source:      str       # ticket_id this fact came from
    category:    str       # "server" | "service" | "employee" | "pattern" | "config"
    created_at:  str       # ISO string


class Episode(TypedDict):
    """Full snapshot of one resolved incident session."""
    session_id:               str
    ticket_id:                str
    ticket_type:              Optional[int]   # 1-4
    turns:                    list            # list of ConversationTurn
    resolution:               Optional[str]
    resolved:                 bool
    resolution_time_minutes:  Optional[int]
    created_at:               str             # ISO string


@dataclass
class RecalledContext:
    """
    Assembled memory context returned by Recaller.
    Injected into agent prompts before every LLM call.
    """
    recent_turns:      list[ConversationTurn] = field(default_factory=list)
    known_facts:       list[MemoryFact]       = field(default_factory=list)
    past_episodes:     list[Episode]          = field(default_factory=list)
    past_sops:         list[dict]             = field(default_factory=list)
    total_tokens_used: int                    = 0

    def to_prompt_string(self) -> str:
        """
        Formats all recalled memory into a single string
        ready to be injected into an agent's system prompt.
        """
        parts = []

        if self.recent_turns:
            parts.append("RECENT CONVERSATION:")
            for t in self.recent_turns[-6:]:   # last 6 turns max
                parts.append(f"  {t['role'].upper()}: {t['content']}")

        if self.known_facts:
            parts.append("\nKNOWN FACTS ABOUT THIS SYSTEM:")
            for f in self.known_facts[:5]:
                parts.append(f"  [{f['category']}] {f['fact']} (confidence: {f['confidence']:.0%})")

        if self.past_episodes:
            parts.append("\nSIMILAR PAST INCIDENTS:")
            for ep in self.past_episodes[:2]:
                parts.append(
                    f"  Ticket {ep['ticket_id']} (Type {ep.get('ticket_type')}) — "
                    f"Resolved: {ep['resolved']} — {ep.get('resolution', 'N/A')[:120]}"
                )

        if self.past_sops:
            parts.append("\nRELEVANT PAST RESOLUTIONS (from historical data):")
            for sop in self.past_sops[:3]:
                parts.append(
                    f"  Problem: {sop.get('problem', '')[:80]}\n"
                    f"  Resolution: {sop.get('resolution', '')[:120]}"
                )

        return "\n".join(parts) if parts else "No prior memory context available."