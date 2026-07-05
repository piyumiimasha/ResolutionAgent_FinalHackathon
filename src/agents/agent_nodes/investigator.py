"""
l2_investigator.py — L2 Investigator Agent node.

Responsibilities:
  1. Query Observability MCP for server metrics and service status.
  2. Search Procedural Memory (Qdrant) for similar past resolutions.
  3. Search Long-Term Memory for known facts about this service/server.
  4. Compile all findings into state["findings"] for L3 to act on.

Does NOT propose or execute fixes. Only gathers evidence.
"""

import logging
from langchain_core.messages import HumanMessage, SystemMessage

from agents.state import AgentState
from agents.prompts.agent_prompts import L2_INVESTIGATOR_PROMPT
from memory.memory_ops import Recaller
from memory.procedural_store import ProceduralStore
from infrastructure.llm.factory import get_large_llm
from infrastructure.observability import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer()


async def _query_observability_mcp(ticket: str, ticket_type: int) -> dict:
    """
    Calls the Observability MCP server to fetch metrics and service status.
    Returns raw metrics dict. Handles MCP errors gracefully.
    """
    try:
        # Import here to avoid circular deps at module level
        from mcp_servers.observability_server import ObservabilityMCPClient
        client = ObservabilityMCPClient()
        metrics = await client.get_metrics(ticket=ticket, ticket_type=ticket_type)
        return metrics or {}
    except Exception as exc:
        logger.error("Observability MCP query failed: %s", exc)
        return {"error": str(exc), "metrics": None}


async def _query_procedural_memory(ticket: str, ticket_type: int) -> list:
    """
    Searches Procedural Memory (Qdrant) for similar past incidents.
    Returns list of past resolution dicts.
    """
    try:
        store = ProceduralStore()
        results = await store.search_similar(
            problem=ticket,
            ticket_type=ticket_type,
            top_k=3,
        )
        return results or []
    except Exception as exc:
        logger.error("Procedural memory search failed: %s", exc)
        return []


async def l2_investigator_node(state: AgentState) -> AgentState:
    """
    LangGraph node — investigates the classified ticket.

    Args:
        state: AgentState after L1 has set ticket_type and severity.

    Returns:
        Updated state with findings, metrics_queried, similar_past_incidents.
    """
    with tracer.start_span("l2_investigator") as span:
        span.set_attribute("ticket_id", state.get("ticket_id", ""))
        span.set_attribute("ticket_type", state.get("ticket_type", -1))
        span.set_attribute("severity", state.get("severity", ""))

        try:
            # ── 1. Query Observability MCP ────────────────────────────
            metrics = await _query_observability_mcp(
                ticket=state["ticket"],
                ticket_type=state.get("ticket_type", 1),
            )

            # ── 2. Query Procedural Memory ────────────────────────────
            past_incidents = await _query_procedural_memory(
                ticket=state["ticket"],
                ticket_type=state.get("ticket_type", 1),
            )

            # ── 3. Pull memory context (LT + ST) ─────────────────────
            recaller = Recaller()
            memory_context = await recaller.recall(
                query=state["ticket"],
                ticket_id=state["ticket_id"],
            )

            # ── 4. LLM synthesises all evidence into findings ─────────
            llm = get_large_llm()   # L2 needs reasoning power

            past_incidents_text = "\n".join([
                f"- [{i+1}] Problem: {p.get('problem')} | Resolution: {p.get('resolution')}"
                for i, p in enumerate(past_incidents)
            ]) or "No similar past incidents found."

            metrics_text = str(metrics) if metrics else "No metrics available."

            system_prompt = L2_INVESTIGATOR_PROMPT.format(
                memory_context=memory_context,
                metrics=metrics_text,
                past_incidents=past_incidents_text,
            )

            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=(
                    f"Ticket: {state['ticket']}\n"
                    f"Type: {state.get('ticket_type')} | Severity: {state.get('severity')}\n"
                    f"Classification reason: {state.get('classification_reason', '')}"
                )),
            ]

            response = await llm.ainvoke(messages)
            findings = response.content.strip()

            # ── 5. Update conversation history ────────────────────────
            history = state.get("conversation_history", [])
            history.append({"role": "l2_investigator", "content": findings})

            span.set_attribute("past_incidents_found", len(past_incidents))
            logger.info("L2 completed investigation for ticket %s", state.get("ticket_id"))

            return {
                **state,
                "findings": findings,
                "metrics_queried": metrics,
                "similar_past_incidents": past_incidents,
                "conversation_history": history,
            }

        except Exception as exc:
            logger.error("L2 investigator error for ticket %s: %s", state.get("ticket_id"), exc)
            return {
                **state,
                "findings": f"Investigation failed: {exc}. Proceeding with available context only.",
                "metrics_queried": {},
                "similar_past_incidents": [],
                "retry_count": state.get("retry_count", 0) + 1,
                "error": str(exc),
            }