"""
Voice module — Zuu Crew AI IT Support Swarm.

Provides the LiveKit voice pipeline: VAD → STT → LangGraph → TTS.

Public API::

    from voice.agent import create_and_start_agent
    from voice.config import load_voice_config, validate_voice_env
    from voice.adapter import LangGraphLLMAdapter

Entry point::

    python -m voice.run
"""
