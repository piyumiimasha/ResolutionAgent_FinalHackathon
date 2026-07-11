"""
TTS adapter — provider-dispatched factory.

Two providers supported, switchable via ``config/param.yaml`` ``voice.tts_provider``:

  - ``deepgram``    (default) — Deepgram Aura 2, ~200 ms TTFB, uses DEEPGRAM_API_KEY
  - ``elevenlabs``            — premium voices, ~250 ms TTFB, requires ELEVEN_API_KEY

Default is Deepgram because IT support users care about fast, clear responses
over premium voice quality, and Deepgram reuses the same API key as STT.

Switching providers: change ``voice.tts_provider`` in ``config/param.yaml``.
No code changes required.

Compatible with **livekit-agents >= 1.5.0**.
"""

from __future__ import annotations

from loguru import logger

from voice.config import VoiceConfig


def make_tts(cfg: VoiceConfig):
    """Build the streaming TTS plugin for the configured provider.

    Both plugins return audio chunks as synthesis starts, so the user
    hears the beginning of the response while the rest is still being
    generated — critical for sub-2s perceived latency.
    """
    provider = cfg.tts_provider.lower()

    if provider == "deepgram":
        from livekit.plugins import deepgram

        model = cfg.tts_model or "aura-2-en-us"
        logger.debug(f"TTS = Deepgram Aura 2 (model={model})")
        return deepgram.TTS(model=model)

    if provider == "elevenlabs":
        # Lazy import — keeps the module importable even when the
        # elevenlabs plugin isn't installed.
        from livekit.plugins import elevenlabs

        if not cfg.eleven_api_key:
            raise EnvironmentError(
                "ELEVEN_API_KEY not set — required for tts_provider=elevenlabs.\n"
                "Get one at https://elevenlabs.io/app/settings/api-keys "
                "and add it to your .env file, or switch to tts_provider=deepgram."
            )

        logger.debug(
            f"TTS = ElevenLabs (model={cfg.tts_model}, voice_id={cfg.tts_voice_id})"
        )
        return elevenlabs.TTS(
            model=cfg.tts_model,
            voice_id=cfg.tts_voice_id,
            api_key=cfg.eleven_api_key,
        )

    raise ValueError(
        f"Unknown tts_provider: {cfg.tts_provider!r}. "
        f"Supported: 'deepgram', 'elevenlabs'."
    )
