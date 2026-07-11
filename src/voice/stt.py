"""
STT adapter — Deepgram Nova-3 via the LiveKit plugin.

Kept as a separate file (rather than inlined in ``agent.py``) so switching
STT providers (e.g. to Whisper or AssemblyAI) is a single-file change.

Deepgram Nova-3 gives us:
  - Real-time streaming transcription (no round-trip wait)
  - ~100-200ms word-level latency, well under our 2s voice fast-path target
  - Automatic punctuation + interim results for responsive barge-in detection

Compatible with **livekit-agents >= 1.5.0**.
"""

from __future__ import annotations

from livekit.plugins import deepgram

from voice.config import VoiceConfig


def make_stt(cfg: VoiceConfig):
    """Build the Deepgram streaming STT plugin from ``cfg``.

    The LiveKit Deepgram plugin handles the websocket lifecycle, audio
    framing, and interim/final result events. We just hand it the model
    name and language.

    The API key is picked up automatically from the ``DEEPGRAM_API_KEY``
    environment variable by the plugin — no need to pass it explicitly here.
    """
    return deepgram.STT(
        model=cfg.stt_model,
        language=cfg.stt_language,
    )
