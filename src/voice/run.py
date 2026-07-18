"""
Voice worker entrypoint — Zuu Crew AI IT Support Swarm.

Run this to start a LiveKit agent worker that joins rooms and runs the
full Silero VAD → Deepgram STT → LangGraph → Deepgram TTS pipeline.

Usage::

    # From project root (recommended)
    PYTHONPATH=src python -m voice.run

    # Or via Makefile
    make voice

Required env vars (loaded from .env):
    LIVEKIT_URL             wss://your-project.livekit.cloud
    LIVEKIT_API_KEY
    LIVEKIT_API_SECRET
    DEEPGRAM_API_KEY        Used for both STT (Nova-3) and TTS (Aura 2)

Optional (only needed if tts_provider=elevenlabs in param.yaml):
    ELEVEN_API_KEY

Note: env var key names that tripped us up during setup:
    SUPABASE_SERVICE_ROLE_KEY  (not SUPABASE_KEY)
    LANGFUSE_HOST              (not LANGFUSE_BASE_URL)
    DEEPGRAM_API_KEY           (not DEEPGRAM_KEY)
"""

from __future__ import annotations

import os
import sys

# Ensure src/ is on the path when running ``python src/voice/run.py``
# or ``python -m voice.run`` from any directory.
src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

from dotenv import find_dotenv, load_dotenv

# Walk up from cwd to find .env regardless of invocation directory.
# Without this, dotenv silently does nothing if cwd isn't the project root,
# and Supabase / Qdrant / Langfuse connections fail at import time.
load_dotenv(find_dotenv(usecwd=True))

from livekit.agents import JobContext, WorkerOptions, cli
from loguru import logger

from infrastructure.log import configure_logging
from voice.agent import create_and_start_agent
from voice.config import load_voice_config, validate_voice_env


async def entrypoint(ctx: JobContext) -> None:
    """Called by the LiveKit worker for each new room session."""
    await create_and_start_agent(ctx)


def main() -> None:
    configure_logging()
    cfg = load_voice_config()

    print()
    print("  Zuu Crew AI — IT Support Voice Worker")
    print(f"  {'─' * 50}")
    print(f"  LiveKit URL   : {cfg.livekit_url or '(unset — check .env)'}")
    print(f"  STT           : {cfg.stt_provider}/{cfg.stt_model}")
    print(f"  TTS           : {cfg.tts_provider}/{cfg.tts_model}")
    print(f"  VAD threshold : {cfg.vad_threshold}")
    print(f"  Silence       : {cfg.silence_threshold_ms} ms")
    print(f"  Endpointing   : {cfg.min_endpointing_delay} s")
    print(f"  Interruptions : {cfg.interruption_enabled}")
    print(f"  {'─' * 50}")
    print()

    validate_voice_env()

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            # Qdrant + Supabase + Langfuse connections at import time can
            # take 10-20s on cold start. Default timeout is 10s, which causes
            # the worker to kill+respawn in a loop. 60s gives it breathing room.
            initialize_process_timeout=60.0,
            # One idle subprocess — they all do the same heavy import-time
            # work and starve each other on a laptop.
            num_idle_processes=1,
        )
    )


if __name__ == "__main__":
    main()
