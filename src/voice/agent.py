"""
LiveKit voice agent factory — Zuu Crew AI IT Support Swarm.

Wires the four pieces (Silero VAD, Deepgram STT, LangGraph adapter,
Deepgram TTS) into a ``livekit.agents.voice.Agent`` and starts an
``AgentSession`` for each room.

This file owns the LiveKit-specific lifecycle. The IT support orchestrator
itself never sees LiveKit — it just receives text and returns tokens.

Barge-in (Part 3, criterion 2):
    ``allow_interruptions=True`` is set on the Agent.
    On ``agent_speech_interrupted``, ``adapter.cancel_current()`` is called,
    which fires CancelledError into ``LangGraphLLMStream._run()``.
    The orchestrator then saves the partial with ``[interrupted]`` tag.

Latency HUD (Part 3, criterion 3):
    After every completed turn, ``adapter.last_latency`` is published over
    the LiveKit data channel (topic="latency") so the index.html UI can
    display timing numbers live.

Compatible with **livekit-agents >= 1.5.0**.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from loguru import logger

from livekit import rtc
from livekit.agents import AutoSubscribe, JobContext
from livekit.agents.voice import Agent, AgentSession
from livekit.plugins import silero

from agents.orchestrator import AgentOrchestrator, build_agent
from voice.adapter import LangGraphLLMAdapter
from voice.config import VoiceConfig, load_voice_config
from voice.pipeline import (
    SessionManager,
    VoiceSession,
    on_agent_speech_finished,
    on_agent_speech_interrupted,
    on_agent_speech_started,
    on_user_speech_committed,
    on_user_speech_started,
)
from voice.stt import make_stt
from voice.tts import make_tts


# ── Module-level singletons (one per worker process) ───────────

_orchestrator: Optional[AgentOrchestrator] = None
_session_manager = SessionManager()


async def _get_orchestrator() -> AgentOrchestrator:
    """Lazy-init the IT Support LangGraph agent. Cached for the worker lifetime."""
    global _orchestrator
    if _orchestrator is None:
        logger.info("Building IT Support agent graph (one-time cold start)...")
        # build_agent() is sync (DB connections, Qdrant, model loads).
        # Push off the event loop so we don't block other rooms during startup.
        _orchestrator = await asyncio.to_thread(build_agent)
        logger.success("IT Support agent ready ✓")
        # Pre-warm the LLM HTTPS connection pool before the first real call.
        await _warm_llm_connection(_orchestrator)
    return _orchestrator


async def _warm_llm_connection(orchestrator: AgentOrchestrator) -> None:
    """Pre-warm the HTTPS connection pool to the LLM provider.

    The first call pays DNS + TLS + HTTP/2 setup (~300-700 ms). Firing a
    tiny warm-up call at worker boot means call #1 from the user doesn't
    pay that tax on top of its real LLM latency.
    """
    try:
        from langchain_core.messages import HumanMessage
        llm = getattr(orchestrator, "llm_fast", None) or getattr(orchestrator, "llm_chat", None)
        if llm is None:
            logger.debug("No llm_fast/llm_chat found on orchestrator — skipping warm-up")
            return
        t0 = time.perf_counter()
        await llm.ainvoke(
            [HumanMessage(content="ping")],
            config={"tags": ["__warmup__"]},
        )
        ms = int((time.perf_counter() - t0) * 1000)
        logger.success(f"LLM connection warm — first call took {ms} ms ✓")
    except Exception as e:
        logger.warning(f"LLM warm-up failed (non-fatal): {e}")


# ── Agent instructions ─────────────────────────────────────────

VOICE_INSTRUCTIONS = (
    "You are the Zuu Crew AI IT Support voice assistant. "
    "You are on a voice call — keep replies short, clear, and action-oriented. "
    "Aim for 1-3 sentences maximum. The technician is listening, not reading: "
    "no bullet points, no markdown, no asterisks. "
    "If you need to give steps, say them as a natural sequence: "
    "'First... then... finally...' "
    "Always confirm what you are investigating before giving the answer."
)


# ── Voice agent builder ────────────────────────────────────────

def build_voice_agent(
    *,
    participant: rtc.RemoteParticipant,
    room_name: str,
    cfg: VoiceConfig,
    orchestrator: AgentOrchestrator,
) -> tuple[Agent, VoiceSession]:
    """Build a configured LiveKit ``Agent`` for one participant.

    Returns the ``Agent`` (for ``AgentSession`` to start) and the
    ``VoiceSession`` record (for event handlers to update).
    """
    user_id = participant.identity or "unknown-user"
    session = _session_manager.get_or_create(
        participant_id=participant.identity,
        user_id=user_id,
        room_name=room_name,
    )
    logger.info(f"Participant joined: {user_id} → {session.session_id}")

    # The LangGraph ↔ LiveKit bridge.
    adapter = LangGraphLLMAdapter(
        orchestrator=orchestrator,
        user_id=user_id,
        session_id=session.session_id,
    )

    # Silero VAD — runs on device, no API call. Fast barge-in detection.
    vad = silero.VAD.load(
        min_silence_duration=cfg.silence_threshold_ms / 1000.0,
        activation_threshold=cfg.vad_threshold,
        sample_rate=16000,  # Deepgram STT + Silero expect 16 kHz
    )
    stt = make_stt(cfg)
    tts = make_tts(cfg)

    agent = Agent(
        instructions=VOICE_INSTRUCTIONS,
        stt=stt,
        llm=adapter,
        tts=tts,
        vad=vad,
        allow_interruptions=cfg.interruption_enabled,  # Required for barge-in
        min_endpointing_delay=cfg.min_endpointing_delay,
    )

    logger.success(
        f"Voice agent ready — "
        f"STT={cfg.stt_provider}/{cfg.stt_model} | "
        f"TTS={cfg.tts_provider}/{cfg.tts_model} | "
        f"interruptions={cfg.interruption_enabled} | "
        f"endpointing={cfg.min_endpointing_delay}s"
    )

    # Stash the adapter on the agent for barge-in cancellation hooks.
    agent._lg_adapter = adapter  # type: ignore[attr-defined]
    return agent, session


# ── Worker entrypoint helper ───────────────────────────────────

async def create_and_start_agent(ctx: JobContext) -> AgentSession:
    """Connect to the room, wait for a participant, build + start the agent.

    Called from ``voice.run.entrypoint``. Handles the full per-room
    lifecycle including event wiring and disconnect cleanup.
    """
    cfg = load_voice_config()
    orchestrator = await _get_orchestrator()

    # Connect (audio only — video tracks not needed for IT support calls).
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # Wait for the first remote participant.
    participant = await ctx.wait_for_participant()

    agent, session = build_voice_agent(
        participant=participant,
        room_name=ctx.room.name,
        cfg=cfg,
        orchestrator=orchestrator,
    )

    agent_session = AgentSession()

    # ── Wire LiveKit events to our session record ──────────────

    @agent_session.on("user_state_changed")
    def _on_user_state(ev):
        new = getattr(ev, "new_state", None)
        if new == "speaking":
            on_user_speech_started(session)
        elif new in ("listening", "away") and session.is_user_speaking:
            session.is_user_speaking = False

    @agent_session.on("agent_state_changed")
    def _on_agent_state(ev):
        new = getattr(ev, "new_state", None)
        if new == "speaking":
            on_agent_speech_started(session)
        elif new == "listening" and session.is_agent_speaking:
            on_agent_speech_finished(session)
            # Publish latency telemetry to the browser HUD over the
            # LiveKit data channel. Best-effort — never break the call.
            _publish_latency_hud(ctx, agent)

    @agent_session.on("agent_false_interruption")
    def _on_false_interrupt(_ev):
        # Silero detected noise, not real speech — agent continues.
        pass

    @agent_session.on("conversation_item_added")
    def _on_item(ev):
        # conversation_item_added fires for both user and agent turns.
        # We use it as the "user committed" signal (user_speech_committed
        # was renamed/removed in livekit-agents 1.5.x).
        item = getattr(ev, "item", None)
        if item is not None and getattr(item, "role", None) == "user":
            text = getattr(item, "text_content", None) or ""
            if text:
                on_user_speech_committed(session, text)

    # ── Barge-in hook ──────────────────────────────────────────
    # Cancel in-flight agent task when user speaks over the agent.
    # adapter.cancel_current() fires CancelledError into LangGraphLLMStream._run(),
    # which the orchestrator catches to write the [interrupted] memory tag.
    adapter: LangGraphLLMAdapter = agent._lg_adapter  # type: ignore[attr-defined]

    try:
        @agent_session.on("agent_speech_interrupted")
        def _on_interrupted(_ev):
            on_agent_speech_interrupted(session)
            adapter.cancel_current()
    except Exception:
        # Some 1.5.x builds don't have this event — state-change fallback only.
        logger.debug(
            "agent_speech_interrupted event not registered in this SDK version; "
            "relying on state-change driven cancellation"
        )

    # ── Disconnect cleanup ──────────────────────────────────────
    @ctx.room.on("participant_disconnected")
    def _on_disconnect(p: rtc.RemoteParticipant):
        if p.identity == participant.identity:
            _session_manager.end_session(p.identity)

    # ── Go live ─────────────────────────────────────────────────
    await agent_session.start(agent, room=ctx.room)

    # Greet the IT user. allow_interruptions=True so they can immediately
    # start talking if they know what they need.
    await agent_session.say(
        "Hi! Zuu Crew IT Support here. What's the issue you're experiencing today?",
        allow_interruptions=True,
    )

    return agent_session


# ── Internal helpers ───────────────────────────────────────────

def _publish_latency_hud(ctx: JobContext, agent: Agent) -> None:
    """Publish last-turn latency to the browser HUD via LiveKit data channel."""
    try:
        payload = getattr(agent, "_lg_adapter", None)
        last = getattr(payload, "last_latency", None) if payload else None
        if last:
            msg = json.dumps({"type": "latency", **last}).encode("utf-8")
            asyncio.create_task(
                ctx.room.local_participant.publish_data(
                    msg, reliable=True, topic="latency"
                )
            )
    except Exception as e:
        logger.debug(f"publish latency HUD failed (non-fatal): {e}")
