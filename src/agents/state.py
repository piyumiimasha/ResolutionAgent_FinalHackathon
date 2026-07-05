"""
AgentState — shared state TypedDict passed between every node in the LangGraph.
This is the single source of truth for a ticket's lifecycle.
"""

from typing import TypedDict, Optional


class AgentState(TypedDict):
    # ── Incoming ticket ──────────────────────────────────────────────
    ticket_id: str                        # unique ID for this incident
    ticket: str                           # raw user message / incident description
    user_id: Optional[str]                # who raised the ticket

    # ── Guardrail ────────────────────────────────────────────────────
    is_safe: bool                         # did guardrail pass?
    guardrail_reason: Optional[str]       # reason if blocked

    # ── CAG fast-path ────────────────────────────────────────────────
    cag_hit: bool                         # did the CAG cache return a match?
    cag_answer: Optional[str]             # cached answer if hit

    # ── L1 Triage ────────────────────────────────────────────────────
    ticket_type: Optional[int]            # 1=Access, 2=Asset, 3=Degradation, 4=Outage
    severity: Optional[str]              # "low" | "medium" | "critical"
    classification_reason: Optional[str]  # L1's reasoning

    # ── L2 Investigation ─────────────────────────────────────────────
    findings: Optional[str]               # metrics, logs, past SOPs found
    similar_past_incidents: Optional[list]  # from procedural/episodic memory
    metrics_queried: Optional[dict]       # raw data from Observability MCP

    # ── L3 Resolution ────────────────────────────────────────────────
    resolution: Optional[str]             # proposed + executed fix
    runbook_used: Optional[str]           # which runbook was cited
    action_taken: Optional[str]           # what Action MCP executed

    # ── Conversation history (short-term memory) ─────────────────────
    conversation_history: list            # list of {role, content} dicts

    # ── Meta ─────────────────────────────────────────────────────────
    started_at: Optional[str]             # ISO timestamp
    resolved_at: Optional[str]            # ISO timestamp
    error: Optional[str]                  # set if any node fails gracefully
    retry_count: int                      # tracks tool retries (max 3 before escalate)