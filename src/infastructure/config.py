"""
config.py — Central configuration loader.

Loads param.yaml and models.yaml once at startup.
Every other module imports from here — never reads YAML directly.

Usage:
    from infrastructure.config import get_config, get_models
    cfg    = get_config()
    models = get_models()

    small_model = models["groq"]["chat"]["small"]["model"]
    cag_threshold = cfg["cag"]["similarity_threshold"]
"""

import os
from pathlib import Path
from functools import lru_cache
from typing import Any

import yaml
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

# Resolve config directory relative to project root
_PROJECT_ROOT = Path(__file__).parent.parent.parent   # infastructure/config.py → repo root
_CONFIG_DIR   = _PROJECT_ROOT / "config"


def _load_yaml(filename: str) -> dict:
    """Load and parse a YAML config file."""
    path = _CONFIG_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def get_config() -> dict[str, Any]:
    """
    Load and return param.yaml (cached after first call).

    Returns:
        Full param.yaml as a nested dict.
    """
    cfg = _load_yaml("param.yaml")
    logger.debug("Loaded param.yaml (environment={})", cfg.get("environment", "unknown"))
    return cfg


@lru_cache(maxsize=1)
def get_models() -> dict[str, Any]:
    """
    Load and return models.yaml (cached after first call).

    Returns:
        Full models.yaml as a nested dict.
    """
    models = _load_yaml("models.yaml")
    logger.debug("Loaded models.yaml")
    return models


# ── Convenience helpers ───────────────────────────────────────────────────────

def get_groq_model(tier: str) -> str:
    """
    Resolve a Groq model name by tier.

    Args:
        tier: "small" | "large" | "reason"

    Returns:
        Model name string e.g. "llama-3.1-8b-instant"
    """
    models = get_models()
    try:
        return models["groq"]["chat"][tier]["model"]
    except KeyError:
        raise ValueError(f"Unknown Groq model tier: '{tier}'. Must be small | large | reason")


def get_embedding_model() -> str:
    """Return the configured embedding model name."""
    models = get_models()
    return models["openrouter"]["embedding"]["default"]["model"]


def get_embedding_dimensions() -> int:
    """Return vector dimensions for the configured embedding model."""
    models = get_models()
    return models["openrouter"]["embedding"]["default"]["dimensions"]


def is_production() -> bool:
    """True if running in production environment."""
    cfg = get_config()
    return cfg.get("environment", "development") == "production"


def get_env(key: str, required: bool = True) -> str:
    """
    Get an environment variable with optional required check.

    Args:
        key:      Environment variable name.
        required: Raise if missing (default True).

    Returns:
        Value string.
    """
    value = os.getenv(key, "")
    if required and not value:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"Check your .env file."
        )
    return value