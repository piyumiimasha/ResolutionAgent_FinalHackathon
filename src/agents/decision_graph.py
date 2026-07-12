"""
decision_graph.py — Outer LangGraph StateGraph.

Two paths:
  New ticket:   guardrail → CAG router → (hit: return) → swarm (L1→L2→L3)
  Follow-up:    guardrail → follow_up_node (direct conversational reply)
"""

import logging
import uuid
from datetime import datetime, timezone

from langgraph.graph import StateGraph, END

from agents.state import AgentState
from agents.guardrail import guardrail_node
from agents.router import router_node
from agents.orchastrator import run_swarm
from infrastructure.observability import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer()


# ── Follow-up node ────────────────────────────────────────────────────────────

async def follow_up_node(state: AgentState) -> AgentState:
    """
    Handles follow-up messages in an existing conversation.
    Skips L1/L2/L3 swarm entirely — just gives a direct conversational reply
    using the conversation history as context.
    """
    from langchain_core.messages import HumanMessage, SystemMessage
    from infrastructure.llm.factory import get_large_llm
    import json

    history  = state.get("conversation_history", [])
    new_msg  = state.get("original_message", state["ticket"])

    # Format conversation history for the LLM
    history_text = "\n".join([
        f"{t.get('role', 'unknown').upper()}: {t.get('content', '')}"
        for t in history[-8:]   # last 8 turns
    ])

    prompt = f"""You are an IT support engineer having an ongoing conversation with an employee.

CONVERSATION SO FAR:
{history_text}

EMPLOYEE'S NEW MESSAGE:
{new_msg}

Instructions:
- Read the full conversation carefully.
- Respond ONLY to what the employee just said in their new message.
- Do NOT repeat your previous answer.
- Do NOT re-explain what was already covered.
- If they gave you new information (e.g. "I got a new phone"), use it to refine your answer.
- If they said something already worked, acknowledge it and ask if they need anything else.
- If they have a new problem, address it directly.
- Be concise — 2 to 4 sentences maximum.
- Respond as a friendly IT support engineer, NOT as a formal report.

Respond in JSON:
{{
  "user_message": "<your 2-4 sentence reply>",
  "action_mcp": "",
  "summary": "<one line summary>"
}}"""

    try:
        llm      = get_large_llm()
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        raw      = response.content.strip()

        clean  = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean)
        reply  = parsed.get("user_message", "").strip()

    except Exception as exc:
        logger.error("follow_up_node LLM failed: %s", exc)
        reply = "Thanks for the update. Could you give me a bit more detail so I can help further?"

    history = state.get("conversation_history", [])
    history.append({"role": "agent", "content": reply})

    return {
        **state,
        "resolution":           reply,
        "conversation_history": history,
    }


# ── Conditional edges ─────────────────────────────────────────────────────────

def _route_after_guardrail(state: AgentState) -> str:
    if not state.get("is_safe", False):
        return "end"
    # Follow-ups skip CAG and swarm entirely
    if state.get("is_followup", False):
        return "follow_up"
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
    graph.add_node("follow_up", follow_up_node)

    graph.set_entry_point("guardrail")

    graph.add_conditional_edges(
        "guardrail",
        _route_after_guardrail,
        {"end": END, "router": "router", "follow_up": "follow_up"},
    )
    graph.add_conditional_edges(
        "router",
        _route_after_router,
        {"end": END, "swarm": "swarm"},
    )
    graph.add_edge("swarm",     END)
    graph.add_edge("follow_up", END)

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
        existing_ticket_id:   If set → follow-up on existing ticket.
        conversation_history: Prior turns from ST memory.
    """
    is_followup  = bool(existing_ticket_id and conversation_history)
    ticket_id    = existing_ticket_id or str(uuid.uuid4())

    initial_state: AgentState = {
        "ticket_id":             ticket_id,
        "ticket":                ticket,          # raw message — no [CONVERSATION SO FAR] prefix
        "original_message":      ticket,          # kept separate for follow_up_node
        "user_id":               user_id,
        "is_safe":               False,
        "is_followup":           is_followup,     # ← tells guardrail which route to take
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
        span.set_attribute("ticket_id",  ticket_id)
        span.set_attribute("is_followup", is_followup)
        try:
            graph       = get_decision_graph()
            final_state = await graph.ainvoke(initial_state)
            final_state["resolved_at"] = datetime.now(timezone.utc).isoformat()
            return final_state
        except Exception as exc:
            logger.error("Decision graph failed for ticket %s: %s", ticket_id, exc)
            return {**initial_state, "error": str(exc)}