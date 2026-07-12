"""
l1_triage.py — L1 Triage Agent node.

Responsibilities:
  1. Classify the ticket into Type 1/2/3/4 and severity.
  2. CREATE the ticket record in Supabase via Ticketing MCP.
  3. Write ticket_type, severity, classification_reason to state.
"""

import json
import logging
from langchain_core.messages import HumanMessage, SystemMessage

from agents.state import AgentState
from agents.prompts.agent_prompts import L1_TRIAGE_PROMPT
from memory.memory_ops import Recaller
from infrastructure.llm.factory import get_small_llm
from infrastructure.observability import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer()


async def _create_ticket_in_db(state: AgentState, ticket_type: int, severity: str, reason: str) -> None:
    """Create ticket record in Supabase via Ticketing MCP."""
    try:
        from mcp_servers.ticketing_server import TicketingMCPClient
        client = TicketingMCPClient()

        # Build a short title from the ticket text
        title = state["ticket"][:80].strip()
        if len(state["ticket"]) > 80:
            title += "..."

        await client.create_ticket(
            ticket_id   = state["ticket_id"],
            ticket_type = ticket_type,
            severity    = severity,
            title       = title,
            description = state["ticket"],
            user_id = state.get("user_id", ""),
        )
        logger.info("L1 created ticket %s in Supabase", state["ticket_id"])
    except Exception as exc:
        # Never let DB write failure crash the agent
        logger.error("L1 failed to create ticket in Supabase: %s", exc)


async def l1_triage_node(state: AgentState) -> AgentState:
    """LangGraph node — classifies the incoming ticket and creates DB record."""

    with tracer.start_span("l1_triage") as span:
        span.set_attribute("ticket_id", state.get("ticket_id", ""))

        try:
            # ── Pull memory context ───────────────────────────────────
            recaller = Recaller()
            memory_context = await recaller.recall(
                query      = state["ticket"],
                ticket_id  = state["ticket_id"],
                ticket_type= 1,
            )

            # ── LLM classification ────────────────────────────────────
            llm = get_small_llm()
            system_prompt = L1_TRIAGE_PROMPT.format(memory_context=memory_context)

            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=state["ticket"]),
            ]

            response = await llm.ainvoke(messages)
            raw      = response.content.strip()

            # ── Parse JSON ────────────────────────────────────────────
            parsed      = json.loads(raw)
            ticket_type = int(parsed.get("ticket_type", 1))
            severity    = parsed.get("severity", "low").lower()
            reason      = parsed.get("reason", "")

            if ticket_type not in (1, 2, 3, 4): ticket_type = 1
            if severity not in ("low", "medium", "critical"): severity = "low"

            span.set_attribute("ticket_type", ticket_type)
            span.set_attribute("severity",    severity)

            # ── Create ticket in Supabase ─────────────────────────────
            await _create_ticket_in_db(state, ticket_type, severity, reason)

            # ── Update conversation history ───────────────────────────
            history = state.get("conversation_history", [])
            history.append({
                "role":    "l1_triage",
                "content": f"Classified as Type {ticket_type}, severity={severity}. {reason}",
            })

            logger.info(
                "L1 classified ticket %s → type=%d severity=%s",
                state.get("ticket_id"), ticket_type, severity,
            )

            return {
                **state,
                "ticket_type":           ticket_type,
                "severity":              severity,
                "classification_reason": reason,
                "conversation_history":  history,
            }

        except json.JSONDecodeError as exc:
            logger.error("L1 JSON parse failed for %s: %s", state.get("ticket_id"), exc)
            return {
                **state,
                "ticket_type":           1,
                "severity":              "low",
                "classification_reason": "Classification failed — defaulted to Type 1 low.",
                "retry_count":           state.get("retry_count", 0) + 1,
            }

        except Exception as exc:
            logger.error("L1 triage error for %s: %s", state.get("ticket_id"), exc)
            return {
                **state,
                "ticket_type":           1,
                "severity":              "low",
                "classification_reason": f"L1 error: {exc}",
                "retry_count":           state.get("retry_count", 0) + 1,
                "error":                 str(exc),
            }