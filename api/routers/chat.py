"""
api/routers/chat.py — Chat endpoints.

POST /chat        — process a ticket synchronously
POST /chat/stream — stream agent progress as SSE
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger

from api.schemas import ChatRequest, ChatResponse
from agents.decision_graph import process_ticket
from agents.state import AgentState
from memory.st_store import STStore

router = APIRouter()


def _state_to_response(state: AgentState) -> ChatResponse:
    return ChatResponse(
        ticket_id        = state.get("ticket_id", ""),
        resolution       = state.get("resolution"),
        ticket_type      = state.get("ticket_type"),
        severity         = state.get("severity"),
        runbook_used     = state.get("runbook_used"),
        action_taken     = state.get("action_taken"),
        cag_hit          = state.get("cag_hit", False),
        is_safe          = state.get("is_safe", True),
        guardrail_reason = state.get("guardrail_reason"),
        error            = state.get("error"),
        started_at       = state.get("started_at"),
        resolved_at      = state.get("resolved_at"),
    )


@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """
    Process an IT support ticket.

    If ticket_id is provided, this is a follow-up message on an existing
    conversation — fetch history from ST memory and include it.
    """
    logger.info(
        "POST /chat | user={} ticket_id={} message='{:.60s}'",
        request.user_id, request.ticket_id, request.message,
    )

    try:
        st = STStore()

        # ── Fetch existing conversation history if follow-up ──────────
        conversation_history = []
        if request.ticket_id:
            existing_turns = await st.get_recent(request.ticket_id, n=20)
            conversation_history = [
                {"role": t["role"], "content": t["content"]}
                for t in existing_turns
            ]
            logger.info(
                "Follow-up on ticket {} — loaded {} turns from ST memory",
                request.ticket_id, len(conversation_history),
            )

        # ── Save user message to ST memory ────────────────────────────
        final_state = await process_ticket(
            ticket               = request.message,
            user_id              = request.user_id,
            existing_ticket_id   = request.ticket_id,
            conversation_history = conversation_history,
        )

        # ── Save both user message and response to ST memory ──────────
        tid = final_state.get("ticket_id", "")
        await st.add_turn(tid, "user",  request.message, user_id=request.user_id or "")
        if final_state.get("resolution"):
            await st.add_turn(tid, "agent", final_state["resolution"], user_id=request.user_id or "")

        response = _state_to_response(final_state)
        logger.info(
            "POST /chat complete | ticket={} cag_hit={}",
            response.ticket_id, response.cag_hit,
        )
        return response

    except Exception as exc:
        logger.error("POST /chat failed: {}", exc)
        raise HTTPException(status_code=500, detail=f"Internal error: {exc}")


@router.post("/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    """Stream ticket processing as Server-Sent Events."""

    async def event_generator():
        try:
            yield _sse("start", "Processing your request...")

            st = STStore()
            conversation_history = []
            if request.ticket_id:
                existing_turns = await st.get_recent(request.ticket_id, n=20)
                conversation_history = [
                    {"role": t["role"], "content": t["content"]}
                    for t in existing_turns
                ]

            final_state = await process_ticket(
                ticket               = request.message,
                user_id              = request.user_id,
                existing_ticket_id   = request.ticket_id,
                conversation_history = conversation_history,
            )

            tid = final_state.get("ticket_id", "")
            await st.add_turn(tid, "user",  request.message, user_id=request.user_id or "")
            if final_state.get("resolution"):
                await st.add_turn(tid, "agent", final_state["resolution"], user_id=request.user_id or "")

            if final_state.get("cag_hit"):
                yield _sse("update", "Cache hit — instant answer retrieved")
            else:
                if final_state.get("ticket_type"):
                    yield _sse("update", f"L1: Type {final_state['ticket_type']}, {final_state.get('severity')}")
                if final_state.get("findings"):
                    yield _sse("update", "L2: Investigation complete")
                if final_state.get("resolution"):
                    yield _sse("update", "L3: Resolution ready")

            response = _state_to_response(final_state)
            yield _sse("done", state=response.model_dump())

        except Exception as exc:
            logger.error("Stream error: {}", exc)
            yield _sse("error", str(exc))

    return StreamingResponse(
        event_generator(),
        media_type = "text/event-stream",
        headers    = {
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


def _sse(event: str, data: str = "", state: dict = None) -> str:
    payload = {"event": event, "data": data}
    if state:
        payload["state"] = state
    return f"data: {json.dumps(payload)}\n\n"