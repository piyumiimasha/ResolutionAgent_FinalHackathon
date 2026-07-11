"""
decision_graph.py — Outer LangGraph StateGraph.

Entry point the FastAPI /chat router calls.

Flow:
    guardrail → router → (cag_hit?) → END (fast-path)
                                  ↘ → swarm (L1 → L2 → L3)
"""

import logging
import uuid
from datetime import datetime, timezone

from langgraph.graph import StateGraph, END

from agents.state import AgentState
from agents.guardrail import guardrail_node
from agents.router import router_node
from agents.orchastrator import run_swarm
from infastructure.observability import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer()


# ── Conditional edges ─────────────────────────────────────────────────────────

def _route_after_guardrail(state: AgentState) -> str:
    if not state.get("is_safe", False):
        return "end"
    return "router"


def _route_after_router(state: AgentState) -> str:
    if state.get("cag_hit", False):
        return "end"
    return "swarm"


# ── Graph construction ────────────────────────────────────────────────────────

def build_decision_graph() -> StateGraph:
    graph = StateGraph(AgentState)

    graph.add_node("guardrail", guardrail_node)
    graph.add_node("router",    router_node)
    graph.add_node("swarm",     run_swarm)

    graph.set_entry_point("guardrail")

    graph.add_conditional_edges(
        "guardrail",
        _route_after_guardrail,
        {"end": END, "router": "router"},
    )
    graph.add_conditional_edges(
        "router",
        _route_after_router,
        {"end": END, "swarm": "swarm"},
    )
    graph.add_edge("swarm", END)

    return graph.compile()


_graph = None

def get_decision_graph():
    global _graph
    if _graph is None:
        _graph = build_decision_graph()
    return _graph


# ── Public interface ──────────────────────────────────────────────────────────

async def process_ticket(
    ticket:               str,
    user_id:              str  = None,
    existing_ticket_id:   str  = None,
    conversation_history: list = None,
) -> AgentState:
    """
    Main entry point called by FastAPI chat router.

    Args:
        ticket:               Raw user message.
        user_id:              Employee ID.
        existing_ticket_id:   If set, this is a follow-up on an existing ticket.
                              The same ticket_id is reused so ST memory is shared.
        conversation_history: Prior turns loaded from ST memory for follow-ups.

    Returns:
        Final AgentState with resolution.
    """
    # Reuse existing ticket_id for follow-ups so memory stays in the same thread
    ticket_id = existing_ticket_id or str(uuid.uuid4())

    # Build the full message for the LLM — include conversation context
    # for follow-up messages so agents understand what came before
    if conversation_history and existing_ticket_id:
        # Format prior turns as context prefix
        context_lines = []
        for turn in conversation_history[-6:]:   # last 6 turns
            role    = turn.get("role", "unknown").upper()
            content = turn.get("content", "")[:200]  # truncate long turns
            context_lines.append(f"{role}: {content}")

        context_prefix = "\n".join(context_lines)
        enriched_ticket = (
            f"[CONVERSATION SO FAR]\n{context_prefix}\n\n"
            f"[NEW MESSAGE FROM USER]\n{ticket}"
        )
    else:
        enriched_ticket = ticket

    initial_state: AgentState = {
        "ticket_id":             ticket_id,
        "ticket":                enriched_ticket,
        "user_id":               user_id,
        "is_safe":               False,
        "guardrail_reason":      None,
        "cag_hit":               False,
        "cag_answer":            None,
        "ticket_type":           None,
        "severity":              None,
        "classification_reason": None,
        "findings":              None,
        "similar_past_incidents":None,
        "metrics_queried":       None,
        "resolution":            None,
        "runbook_used":          None,
        "action_taken":          None,
        "conversation_history":  conversation_history or [],
        "started_at":            datetime.now(timezone.utc).isoformat(),
        "resolved_at":           None,
        "error":                 None,
        "retry_count":           0,
    }

    with tracer.start_span("decision_graph") as span:
        span.set_attribute("ticket_id", ticket_id)
        span.set_attribute("is_followup", bool(existing_ticket_id))
        try:
            graph       = get_decision_graph()
            final_state = await graph.ainvoke(initial_state)
            final_state["resolved_at"] = datetime.now(timezone.utc).isoformat()
            return final_state
        except Exception as exc:
            logger.error("Decision graph failed for ticket %s: %s", ticket_id, exc)
            return {**initial_state, "error": str(exc)}