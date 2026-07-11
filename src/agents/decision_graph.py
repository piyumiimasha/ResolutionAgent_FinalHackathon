"""
decision_graph.py — Outer LangGraph StateGraph.

This is the entry point the FastAPI /chat router calls.

Flow:
    guardrail → router → (cag_hit?) → END (fast-path)
                                  ↘ → swarm (L1 → L2 → L3)

The inner swarm (orchestrator) is wired in as a single node here.
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


# ── Conditional edge function ─────────────────────────────────────────────────

def _route_after_guardrail(state: AgentState) -> str:
    """After guardrail: blocked → END, safe → router."""
    if not state.get("is_safe", False):
        return "end"
    return "router"


def _route_after_router(state: AgentState) -> str:
    """After router: cache hit → END, miss → swarm."""
    if state.get("cag_hit", False):
        return "end"
    return "swarm"


# ── Graph construction ────────────────────────────────────────────────────────

def build_decision_graph() -> StateGraph:
    """
    Builds and compiles the outer decision graph.

    Returns:
        Compiled LangGraph StateGraph ready to invoke.
    """
    graph = StateGraph(AgentState)

    # Nodes
    graph.add_node("guardrail", guardrail_node)
    graph.add_node("router", router_node)
    graph.add_node("swarm", run_swarm)          # inner graph from orchestrator.py

    # Entry point
    graph.set_entry_point("guardrail")

    # Edges
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


# Singleton compiled graph — built once at startup
_graph = None


def get_decision_graph():
    global _graph
    if _graph is None:
        _graph = build_decision_graph()
    return _graph


# ── Public interface used by the API ─────────────────────────────────────────

async def process_ticket(ticket: str, user_id: str = None) -> AgentState:
    """
    Main entry point. Called by FastAPI chat router.

    Args:
        ticket:  Raw user message / incident description.
        user_id: Optional user identifier.

    Returns:
        Final AgentState with resolution (or guardrail_reason if blocked).
    """
    initial_state: AgentState = {
        "ticket_id": str(uuid.uuid4()),
        "ticket": ticket,
        "user_id": user_id,
        "is_safe": False,
        "guardrail_reason": None,
        "cag_hit": False,
        "cag_answer": None,
        "ticket_type": None,
        "severity": None,
        "classification_reason": None,
        "findings": None,
        "similar_past_incidents": None,
        "metrics_queried": None,
        "resolution": None,
        "runbook_used": None,
        "action_taken": None,
        "conversation_history": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "resolved_at": None,
        "error": None,
        "retry_count": 0,
    }

    with tracer.start_span("decision_graph") as span:
        span.set_attribute("ticket_id", initial_state["ticket_id"])
        try:
            graph = get_decision_graph()
            final_state = await graph.ainvoke(initial_state)
            final_state["resolved_at"] = datetime.now(timezone.utc).isoformat()
            span.set_attribute("resolved", True)
            return final_state
        except Exception as exc:
            logger.error("Decision graph failed for ticket %s: %s", initial_state["ticket_id"], exc)
            return {**initial_state, "error": str(exc)}
        finally:
            # Flush immediately so traces appear in Langfuse dashboard
            from infastructure.observability import get_langfuse
            lf = get_langfuse()
            if lf:
                lf.flush()