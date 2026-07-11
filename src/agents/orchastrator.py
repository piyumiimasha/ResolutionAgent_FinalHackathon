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
    


"""
AgentOrchestrator — voice fast-path wrapper around the IT Support swarm.

Append this to the bottom of src/agents/orchestrator.py
(after the existing run_swarm / build_swarm_graph code).

Voice fast-path design:
    achat_stream_fast() skips the full L1→L2→L3 fan-out and goes directly
    to a single streaming LLM call. This keeps first-token latency under
    ~800 ms from transcript receipt, well within the 2s perceived-latency
    target (Deepgram TTS TTFB ~200 ms + LiveKit audio buffer ~100 ms).

[interrupted] memory tagging:
    On barge-in, LiveKit fires asyncio.CancelledError into achat_stream_fast().
    We catch it here, save the partial answer to ShortTermMemoryStore tagged
    "[interrupted]", then re-raise so the voice adapter knows the task ended.
    Memory integrity rule: unsaid content is NEVER saved.
"""

import asyncio
import time
from typing import AsyncGenerator

from loguru import logger
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from memory.schemas import ConversationTurn
from memory.st_store import STStore
from infrastructure.config import get_config

_cfg = get_config()  
_ST_MAX_TURNS   = _cfg.get("memory", {}).get("st_max_turns", 30)
_ST_TTL_SECONDS = _cfg.get("memory", {}).get("st_ttl_seconds", 86400)
# ── Voice fast-path system prompt ─────────────────────────────────────────────

_VOICE_SYSTEM_PROMPT = """\
You are the Zuu Crew AI IT Support assistant on a voice call.
Keep replies short, clear, and action-oriented — 1 to 3 sentences maximum.
No bullet points, no markdown, no asterisks.
If giving steps, use natural speech: "First... then... finally..."
Always confirm what you are investigating before giving the answer."""


# ── Orchestrator ──────────────────────────────────────────────────────────────

class AgentOrchestrator:
    """
    Thin wrapper that exposes the IT Support swarm to the voice layer.

    The voice pipeline only calls achat_stream_fast(). The full L1→L2→L3
    swarm graph (run_swarm) is called separately by the HTTP chat endpoint
    via decision_graph — the orchestrator does not duplicate that logic.

    Attributes
    ----------
    llm_fast : ChatGroq
        Groq Llama-3.3-70B — used for the voice fast-path.
        ~500-800 ms TTFT, ~200 tok/s. Low enough for sub-2s perceived latency.
    st_store : STStore
        Used to save [interrupted] partials and completed voice turns.
    """

    def __init__(self) -> None:
        self.llm_fast = ChatGroq(
            model="llama-3.3-70b-versatile",
            temperature=0.0,
            streaming=True,
        )
        self.st_store = STStore()
        logger.info("AgentOrchestrator ready (voice fast-path: Groq llama-3.3-70b)")

    async def achat_stream_fast(
        self,
        user_message: str,
        user_id: str,
        session_id: str,
    ) -> AsyncGenerator[tuple[str, object], None]:
        """
        Voice fast-path: stream tokens directly from a single LLM call.

        Yields
        ------
        ("token",   str)     — each incremental token as it arrives
        ("partial", str)     — running concatenation of tokens emitted so far
        ("final",   object)  — simple response object after the last token

        On asyncio.CancelledError (barge-in):
            Saves the partial answer to ShortTermMemoryStore with the
            "[interrupted]" suffix, then re-raises CancelledError so the
            voice adapter knows the task ended early.
            Unsaid content is NEVER saved.
        """
        # Save the user turn to short-term memory
        try:
            self.st_store.append(
                ConversationTurn(
                    user_id=user_id,
                    session_id=session_id,
                    role="user",
                    content=user_message,
                    ts=time.time(),
                ),
                max_turns=_ST_MAX_TURNS,
                ttl_seconds=_ST_TTL_SECONDS,
            )
        except Exception as e:
            logger.warning(f"ST save (user turn) failed (non-fatal): {e}")

        messages = [
            SystemMessage(content=_VOICE_SYSTEM_PROMPT),
            HumanMessage(content=user_message),
        ]

        spoken_parts: list[str] = []

        try:
            async for chunk in self.llm_fast.astream(messages):
                token: str = chunk.content or ""
                if not token:
                    continue
                spoken_parts.append(token)
                yield ("token", token)
                yield ("partial", "".join(spoken_parts))

            full_answer = "".join(spoken_parts).strip()

            # Save completed assistant turn to short-term memory
            try:
                self.st_store.append(
                    ConversationTurn(
                        user_id=user_id,
                        session_id=session_id,
                        role="assistant",
                        content=full_answer,
                        ts=time.time(),
                    ),
                    max_turns=_ST_MAX_TURNS,
                    ttl_seconds=_ST_TTL_SECONDS,
                )
            except Exception as e:
                logger.warning(f"ST save (assistant turn) failed (non-fatal): {e}")

            # Simple response object — voice adapter reads .route and .tier
            yield ("final", _VoiceResponse(answer=full_answer))

        except asyncio.CancelledError:
            # ── Barge-in: save only what was spoken, tagged [interrupted] ──
            partial = "".join(spoken_parts).strip()
            if partial:
                try:
                    self.st_store.append(
                        ConversationTurn(
                            user_id=user_id,
                            session_id=session_id,
                            role="assistant",
                            content=f"{partial} [interrupted]",
                            ts=time.time(),
                        ),
                        max_turns=_ST_MAX_TURNS,
                        ttl_seconds=_ST_TTL_SECONDS,
                    )
                    logger.info(
                        f"[interrupted] Saved partial to ST memory "
                        f"({len(partial)} chars) for session={session_id}"
                    )
                except Exception as e:
                    logger.warning(f"ST save ([interrupted] partial) failed (non-fatal): {e}")
            raise  # Must re-raise so voice adapter logs it correctly


class _VoiceResponse:
    """Minimal response object yielded as the 'final' tuple payload."""
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.route = "voice_fast"
        self.tier = "L1"


# ── Factory function called by voice/agent.py ─────────────────────────────────

def build_agent() -> AgentOrchestrator:
    """Build and return the AgentOrchestrator. Called once per worker process."""
    return AgentOrchestrator()