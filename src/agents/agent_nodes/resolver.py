"""
l3_resolver.py — L3 Resolution Agent node.

Responsibilities:
  1. Retrieve the correct runbook from Qdrant via RAG.
  2. Synthesise findings + runbook into a user-friendly resolution.
  3. Call Action MCP to execute the fix (mock restart/scale/patch).
  4. Write resolution (user-facing), runbook_used, action_taken to state.

This is the last agent node before write-back.
"""

import json
import logging
from langchain_core.messages import HumanMessage, SystemMessage

from agents.state import AgentState
from agents.prompts.agent_prompts import L3_RESOLVER_PROMPT
from services.rag_service.chain import RAGChain
from infrastructure.llm.factory import get_large_llm
from infrastructure.observability import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer()


async def _close_ticket_in_db(state: AgentState, resolution: str, runbook: str) -> None:
    """Close ticket in Supabase with resolution text and runbook used."""
    try:
        from mcp_servers.ticketing_server import TicketingMCPClient
        client = TicketingMCPClient()
        await client.close_ticket(
            ticket_id  = state["ticket_id"],
            resolution = resolution,
            runbook    = runbook,
        )
        logger.info("L3 closed ticket %s in Supabase", state["ticket_id"])
    except Exception as exc:
        logger.error("L3 failed to close ticket in Supabase: %s", exc)


async def _execute_action_mcp(action_plan: str, ticket_type: int, severity: str) -> str:
    """
    Calls the Action MCP server to execute the proposed fix.
    Returns a string describing what was executed.
    Handles MCP failures gracefully.
    """
    try:
        from mcp_servers.action_server import ActionMCPClient
        client = ActionMCPClient()
        result = await client.execute(
            action      = action_plan,
            ticket_type = ticket_type,
            severity    = severity,
        )
        return result or "Action executed."
    except Exception as exc:
        logger.error("Action MCP execution failed: %s", exc)
        return f"Action MCP unavailable — manual execution required."


async def l3_resolver_node(state: AgentState) -> AgentState:
    """
    LangGraph node — resolves the incident and returns a user-friendly message.

    Args:
        state: AgentState after L2 has set findings.

    Returns:
        Updated state with resolution, runbook_used, action_taken.
    """
    with tracer.start_span("l3_resolver") as span:
        span.set_attribute("ticket_id", state.get("ticket_id", ""))
        span.set_attribute("ticket_type", state.get("ticket_type", -1))

        try:
            # ── 1. Retrieve runbook via RAG ───────────────────────────
            rag = RAGChain()
            runbook_context, runbook_source = await rag.retrieve_and_format(
                query       = state.get("findings", state["ticket"]),
                ticket_type = state.get("ticket_type", 1),
            )
            span.set_attribute("runbook_source", runbook_source or "none")

            # ── 2. LLM synthesises resolution ─────────────────────────
            llm = get_large_llm()

            system_prompt = L3_RESOLVER_PROMPT.format(
                runbook_context = runbook_context or "No matching runbook found.",
                runbook_source  = runbook_source or "N/A",
            )

            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=(
                    f"Original ticket: {state['ticket']}\n\n"
                    f"L2 Findings:\n{state.get('findings', 'No findings available.')}\n\n"
                    f"Ticket Type: {state.get('ticket_type')} | Severity: {state.get('severity')}"
                )),
            ]

            response = await llm.ainvoke(messages)
            raw      = response.content.strip()

            # ── 3. Parse JSON response ────────────────────────────────
            try:
                # Strip markdown fences if LLM added them
                clean  = raw.replace("```json", "").replace("```", "").strip()
                parsed = json.loads(clean)

                user_message = parsed.get("user_message", "").strip()
                action_str   = parsed.get("action_mcp", "").strip()
                summary      = parsed.get("summary", "").strip()

            except (json.JSONDecodeError, KeyError):
                # Fallback: use raw response as user message
                logger.warning(
                    "L3 JSON parse failed for ticket %s — using raw response",
                    state.get("ticket_id"),
                )
                user_message = raw
                action_str   = ""
                summary      = raw[:120]

            # ── 4. Execute via Action MCP ─────────────────────────────
            action_result = ""
            if action_str:
                action_result = await _execute_action_mcp(
                    action_plan = action_str,
                    ticket_type = state.get("ticket_type", 1),
                    severity    = state.get("severity", "low"),
                )

            # ── 5. Final user-facing resolution ───────────────────────
            final_resolution = user_message

            # ── 6. Update conversation history ────────────────────────
            history = state.get("conversation_history", [])
            history.append({
                "role":    "l3_resolver",
                "content": final_resolution,
            })

            # ── 7. Close ticket in Supabase via Ticketing MCP ─────────
            await _close_ticket_in_db(
                state        = state,
                resolution   = final_resolution,
                runbook      = runbook_source or "",
            )

            logger.info(
                "L3 resolved ticket %s using runbook: %s",
                state.get("ticket_id"), runbook_source,
            )

            return {
                **state,
                "resolution":           final_resolution,
                "runbook_used":         runbook_source,
                "action_taken":         action_result or action_str,
                "conversation_history": history,
            }

        except Exception as exc:
            logger.error("L3 resolver error for ticket %s: %s", state.get("ticket_id"), exc)
            fallback = (
                "I've reviewed your ticket and flagged it for manual review. "
                "An engineer will follow up shortly. "
                f"Reference ID: {state.get('ticket_id', '')}"
            )
            return {
                **state,
                "resolution":  fallback,
                "runbook_used":None,
                "action_taken":"none — error occurred",
                "retry_count": state.get("retry_count", 0) + 1,
                "error":       str(exc),
            }