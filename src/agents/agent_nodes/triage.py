"""
l1_triage.py — L1 Triage Agent node.

Responsibilities:
  1. Classify the ticket into Type 1/2/3/4.
  2. Assign severity: low / medium / critical.
  3. Create/update the ticket record via Ticketing MCP.
  4. Write ticket_type, severity, classification_reason to state.

Does NOT investigate or resolve. Only classifies and routes.
"""

import json
import logging
from langchain_core.messages import HumanMessage, SystemMessage

from agents.state import AgentState
from agents.prompts.agent_prompts import L1_TRIAGE_PROMPT
from memory.memory_ops import Recaller
from infastructure.llm.factory import get_small_llm
from infastructure.observability import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer()


async def l1_triage_node(state: AgentState) -> AgentState:
    """
    LangGraph node — classifies the incoming ticket.

    Args:
        state: AgentState with `ticket` populated.

    Returns:
        Updated state with ticket_type, severity, classification_reason.
    """
    with tracer.start_span("l1_triage") as span:
        span.set_attribute("ticket_id", state.get("ticket_id", ""))

        try:
            # ── Pull relevant memory context ──────────────────────────
            recaller = Recaller()
            memory_context = await recaller.recall(
                query=state["ticket"],
                ticket_id=state["ticket_id"],
            )

            # ── Build prompt ──────────────────────────────────────────
            llm = get_small_llm()
            system_prompt = L1_TRIAGE_PROMPT.format(memory_context=memory_context)

            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=state["ticket"]),
            ]

            # ── LLM call ──────────────────────────────────────────────
            response = await llm.ainvoke(messages)
            raw = response.content.strip()

            # ── Parse JSON response ───────────────────────────────────
            # LLM is prompted to return JSON only
            parsed = json.loads(raw)
            ticket_type = int(parsed.get("ticket_type", 1))
            severity = parsed.get("severity", "low").lower()
            reason = parsed.get("reason", "")

            # Validate
            if ticket_type not in (1, 2, 3, 4):
                ticket_type = 1
            if severity not in ("low", "medium", "critical"):
                severity = "low"

            span.set_attribute("ticket_type", ticket_type)
            span.set_attribute("severity", severity)

            # ── Append to conversation history ────────────────────────
            history = state.get("conversation_history", [])
            history.append({
                "role": "l1_triage",
                "content": f"Classified as Type {ticket_type}, severity={severity}. {reason}",
            })

            logger.info(
                "L1 classified ticket %s → type=%d severity=%s",
                state.get("ticket_id"), ticket_type, severity,
            )

            return {
                **state,
                "ticket_type": ticket_type,
                "severity": severity,
                "classification_reason": reason,
                "conversation_history": history,
            }

        except json.JSONDecodeError as exc:
            logger.error("L1 failed to parse LLM JSON for ticket %s: %s", state.get("ticket_id"), exc)
            # Fallback defaults — never crash, always keep the graph moving
            return {
                **state,
                "ticket_type": 1,
                "severity": "low",
                "classification_reason": "Classification failed — defaulted to Type 1 low.",
                "retry_count": state.get("retry_count", 0) + 1,
            }

        except Exception as exc:
            logger.error("L1 triage error for ticket %s: %s", state.get("ticket_id"), exc)
            return {
                **state,
                "ticket_type": 1,
                "severity": "low",
                "classification_reason": f"L1 error: {exc}",
                "retry_count": state.get("retry_count", 0) + 1,
                "error": str(exc),
            }