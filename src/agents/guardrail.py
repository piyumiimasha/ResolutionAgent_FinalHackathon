"""
guardrail.py — First node in decision_graph.

Responsibilities:
  1. Block harmful / off-topic inputs before anything else runs.
  2. Verify the message is actually an IT support request.

If this node fails → state["is_safe"] = False and the graph ends here.
No further nodes (router, swarm) are ever called.
"""

import logging
from langchain_core.messages import HumanMessage, SystemMessage
from agents.state import AgentState
from agents.prompts.agent_prompts import GUARDRAIL_PROMPT
from infastructure.llm.factory import get_small_llm
from infastructure.observability import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer()

# Keywords that immediately fail the guardrail without an LLM call
_HARD_BLOCK_KEYWORDS = [
    "bomb", "weapon", "hack", "exploit", "malware",
    "kill", "suicide", "self-harm",
]


def _hard_block_check(text: str) -> bool:
    """
    Fast pre-check before calling the LLM.
    Returns True if the message should be blocked immediately.
    """
    lowered = text.lower()
    return any(kw in lowered for kw in _HARD_BLOCK_KEYWORDS)


async def guardrail_node(state: AgentState) -> AgentState:
    """
    LangGraph node — runs first in decision_graph.

    Args:
        state: Current AgentState with at least `ticket` populated.

    Returns:
        Updated state with `is_safe` and optionally `guardrail_reason` set.
    """
    ticket = state.get("ticket", "")

    with tracer.start_span("guardrail") as span:
        span.set_attribute("ticket_id", state.get("ticket_id", ""))

        # ── Hard block: no LLM needed ─────────────────────────────────
        if _hard_block_check(ticket):
            logger.warning("Guardrail hard-blocked ticket: %s", state.get("ticket_id"))
            span.set_attribute("result", "hard_blocked")
            return {
                **state,
                "is_safe": False,
                "guardrail_reason": "Message contains disallowed content.",
            }

        # ── LLM-based intent check ────────────────────────────────────
        try:
            llm = get_small_llm()   # small/fast model — no need for large here
            messages = [
                SystemMessage(content=GUARDRAIL_PROMPT),
                HumanMessage(content=ticket),
            ]
            response = await llm.ainvoke(messages)
            verdict = response.content.strip().upper()

            if verdict.startswith("PASS"):
                span.set_attribute("result", "pass")
                return {**state, "is_safe": True, "guardrail_reason": None}
            else:
                reason = verdict.replace("FAIL:", "").strip()
                logger.info("Guardrail LLM blocked ticket %s: %s", state.get("ticket_id"), reason)
                span.set_attribute("result", "llm_blocked")
                return {
                    **state,
                    "is_safe": False,
                    "guardrail_reason": reason or "Not a valid IT support request.",
                }

        except Exception as exc:
            # Fail open — if guardrail errors, log and allow through
            # Better to handle a bad ticket than drop a real incident
            logger.error("Guardrail LLM error: %s — failing open", exc)
            span.set_attribute("result", "error_fail_open")
            return {**state, "is_safe": True, "guardrail_reason": None}