"""
Voice pipeline configuration — Zuu Crew AI IT Support Swarm.

All settings are read from ``config/param.yaml`` under the ``voice:``
section, with credentials resolved from environment variables.

Usage::

    from voice.config import load_voice_config, validate_voice_env
    cfg = load_voice_config()
    validate_voice_env()        # raises if any required env var missing
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

# Note: intentional spelling — must match your project's folder name.
from infastructure.config import _PARAMS, _get_nested


# ── Dataclass ──────────────────────────────────────────────────

@dataclass
class VoiceConfig:
    """Resolved voice pipeline settings (param.yaml + env)."""

    # STT
    stt_provider: str = "deepgram"
    stt_model: str = "nova-3"
    stt_language: str = "en"

    # TTS
    # tts_provider ∈ {"elevenlabs", "deepgram"}
    # tts_voice_id is only used by ElevenLabs.
    tts_provider: str = "deepgram"
    tts_model: str = "aura-2-en-us"
    tts_voice_id: str = ""   # ElevenLabs only — blank for Deepgram

    # VAD + end-of-utterance policy.
    # Tuned for snappy IT support voice: silence_threshold_ms is how long
    # Silero waits before declaring speech ended; min_endpointing_delay is
    # the extra buffer LiveKit waits before handing the transcript to the LLM.
    # Lower = faster turn-taking but more false interruptions on hesitant speech.
    vad_threshold: float = 0.5
    silence_threshold_ms: int = 300
    min_endpointing_delay: float = 0.3

    # Pipeline behaviour
    interruption_enabled: bool = True   # Required for barge-in scoring
    sample_rate: int = 16000

    # Credentials (resolved from environment, never logged)
    deepgram_api_key: Optional[str] = field(default=None, repr=False)
    eleven_api_key: Optional[str] = field(default=None, repr=False)
    livekit_url: Optional[str] = None
    livekit_api_key: Optional[str] = field(default=None, repr=False)
    livekit_api_secret: Optional[str] = field(default=None, repr=False)


# ── Loader ─────────────────────────────────────────────────────

def load_voice_config() -> VoiceConfig:
    """Build a ``VoiceConfig`` from ``config/param.yaml`` + environment variables.

    YAML ``voice:`` section values override the dataclass defaults;
    env vars fill credentials. Missing YAML keys use dataclass defaults silently.
    """
    v = _get_nested(_PARAMS, "voice", default={}) or {}

    cfg = VoiceConfig(
        stt_provider=v.get("stt_provider", "deepgram"),
        stt_model=v.get("stt_model", "nova-3"),
        stt_language=v.get("stt_language", "en"),
        tts_provider=v.get("tts_provider", "deepgram"),
        tts_model=v.get("tts_model", "aura-2-en-us"),
        tts_voice_id=v.get("tts_voice_id", ""),
        vad_threshold=float(v.get("vad_threshold", 0.5)),
        silence_threshold_ms=int(v.get("silence_threshold_ms", 300)),
        min_endpointing_delay=float(v.get("min_endpointing_delay", 0.3)),
        interruption_enabled=bool(v.get("interruption_enabled", True)),
        sample_rate=int(v.get("sample_rate", 16000)),
        deepgram_api_key=os.getenv("DEEPGRAM_API_KEY"),
        eleven_api_key=os.getenv("ELEVEN_API_KEY"),
        livekit_url=os.getenv("LIVEKIT_URL"),
        livekit_api_key=os.getenv("LIVEKIT_API_KEY"),
        livekit_api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )

    tts_voice = (
        f"voice={cfg.tts_voice_id}" if cfg.tts_provider == "elevenlabs" else ""
    )
    logger.debug(
        f"Voice config: STT={cfg.stt_provider}/{cfg.stt_model}, "
        f"TTS={cfg.tts_provider}/{cfg.tts_model} {tts_voice}, "
        f"VAD threshold={cfg.vad_threshold}, "
        f"silence={cfg.silence_threshold_ms}ms, "
        f"endpointing={cfg.min_endpointing_delay}s, "
        f"interrupt={cfg.interruption_enabled}"
    )
    return cfg


# ── Env validation ─────────────────────────────────────────────

_BASE_REQUIRED = {
    "DEEPGRAM_API_KEY": "Deepgram (STT + optionally TTS)",
    "LIVEKIT_URL": "LiveKit server URL (wss://... for cloud)",
    "LIVEKIT_API_KEY": "LiveKit API key",
    "LIVEKIT_API_SECRET": "LiveKit API secret",
}

_TTS_REQUIRED: dict[str, dict[str, str]] = {
    "elevenlabs": {"ELEVEN_API_KEY": "ElevenLabs (TTS)"},
    "deepgram": {},   # DEEPGRAM_API_KEY already covered above
}


def validate_voice_env() -> None:
    """Check that every voice-required env var is set.

    TTS provider from ``param.yaml voice.tts_provider`` determines whether
    ``ELEVEN_API_KEY`` is also required. Raises ``EnvironmentError``
    listing every missing variable at once.
    """
    cfg = load_voice_config()
    required = dict(_BASE_REQUIRED)
    required.update(_TTS_REQUIRED.get(cfg.tts_provider, {}))

    missing = [
        f"  - {var}  ({desc})"
        for var, desc in required.items()
        if not os.getenv(var)
    ]
    if missing:
        raise EnvironmentError(
            f"Missing voice pipeline env vars (tts_provider={cfg.tts_provider}):\n"
            + "\n".join(missing)
            + "\n\nSet them in .env (see .env.example) before running.\n"
            + "Key names: DEEPGRAM_API_KEY, LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET"
        )
    logger.success(f"Voice env vars OK (TTS provider: {cfg.tts_provider})")
