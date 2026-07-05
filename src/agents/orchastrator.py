"""
orchestrator.py — Inner LangGraph StateGraph (the swarm).

Only runs when decision_graph routes here (CAG miss).

Flow:
    l1_triage → l2_investigator → l3_resolver → write-back → END

After L3 resolves:
  - Saves episode to episodic memory
  - Distills facts to long-term memory
  - Writes resolution to CAG cache (so next similar ticket is a fast-path hit)
"""

import logging
from datetime import datetime, timezone

from langgraph.graph import StateGraph, END

from agents.state import AgentState
from agents.agent_nodes.triage import l1_triage_node
from agents.agent_nodes.investigator import l2_investigator_node
from agents.agent_nodes.resolver import l3_resolver_node
from memory.episodic_store import EpisodicStore
from memory.memory_ops import Distiller
from services.cag_service.cache import CAGCache
from infrastructure.observability import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer()


# ── Write-back node ───────────────────────────────────────────────────────────

async def write_back_node(state: AgentState) -> AgentState:
    """
    Runs after L3 resolver. Persists the resolved incident to memory and
    updates the CAG cache so future similar tickets short-circuit.

    Args:
        state: Final AgentState after L3 has set resolution.

    Returns:
        State unchanged (write-back is a side effect only).
    """
    with tracer.start_span("write_back") as span:
        span.set_attribute("ticket_id", state.get("ticket_id", ""))

        try:
            # 1. Save full episode to episodic memory
            episodic = EpisodicStore()
            await episodic.save_episode({
                "session_id": state["ticket_id"],
                "ticket_id": state["ticket_id"],
                "ticket_type": state.get("ticket_type"),
                "turns": state.get("conversation_history", []),
                "resolution": state.get("resolution"),
                "resolved": bool(state.get("resolution")),
            })

            # 2. Distill durable facts to long-term memory
            distiller = Distiller()
            await distiller.distill(state.get("conversation_history", []))

            # 3. Update CAG cache with this resolution
            # Only cache if resolution exists and no error occurred
            if state.get("resolution") and not state.get("error"):
                cache = CAGCache()
                await cache.set(state["ticket"], state["resolution"])
                span.set_attribute("cag_updated", True)

            span.set_attribute("write_back_success", True)

        except Exception as exc:
            # Write-back failures must never affect the user response
            logger.error("Write-back failed for ticket %s: %s", state.get("ticket_id"), exc)
            span.set_attribute("write_back_error", str(exc))

        return {
            **state,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }


# ── Conditional edge: escalate if retry_count exceeded ───────────────────────

def _check_retry_limit(state: AgentState) -> str:
    """Graceful fallback — escalate to human if too many retries."""
    if state.get("retry_count", 0) >= 3:
        logger.warning("Max retries reached for ticket %s — escalating", state.get("ticket_id"))
        return "escalate"
    return "l2"


async def _escalate_node(state: AgentState) -> AgentState:
    """Graceful fallback node — sets resolution to human escalation message."""
    return {
        **state,
        "resolution": (
            "This incident requires human intervention. "
            "Escalating to on-call engineer. Ticket ID: " + state.get("ticket_id", "")
        ),
        "error": "max_retries_exceeded",
    }


# ── Graph construction ────────────────────────────────────────────────────────

def build_swarm_graph() -> StateGraph:
    """
    Builds and compiles the inner swarm graph (L1 → L2 → L3).

    Returns:
        Compiled LangGraph StateGraph.
    """
    graph = StateGraph(AgentState)

    # Nodes
    graph.add_node("l1", l1_triage_node)
    graph.add_node("l2", l2_investigator_node)
    graph.add_node("l3", l3_resolver_node)
    graph.add_node("write_back", write_back_node)
    graph.add_node("escalate", _escalate_node)

    # Entry
    graph.set_entry_point("l1")

    # L1 → L2 (with retry guard)
    graph.add_conditional_edges(
        "l1",
        _check_retry_limit,
        {"l2": "l2", "escalate": "escalate"},
    )

    # L2 → L3
    graph.add_edge("l2", "l3")

    # L3 → write-back → END
    graph.add_edge("l3", "write_back")
    graph.add_edge("write_back", END)

    # Escalate → END (no write-back needed)
    graph.add_edge("escalate", END)

    return graph.compile()


# Singleton
_swarm_graph = None


def _get_swarm_graph():
    global _swarm_graph
    if _swarm_graph is None:
        _swarm_graph = build_swarm_graph()
    return _swarm_graph


async def run_swarm(state: AgentState) -> AgentState:
    """
    Entry point called by decision_graph as a single node.
    Runs the full L1 → L2 → L3 swarm and returns final state.
    """
    with tracer.start_span("swarm") as span:
        span.set_attribute("ticket_id", state.get("ticket_id", ""))
        graph = _get_swarm_graph()
        return await graph.ainvoke(state)