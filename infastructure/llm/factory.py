"""
llm/factory.py — LLM factory functions.

Returns configured LangChain ChatGroq instances.
All agent code calls get_small_llm() or get_large_llm() — never instantiates
models directly. This means swapping providers requires changing only this file.

Small LLM → fast + cheap → guardrail, l1_triage, distiller
Large LLM → reasoning   → l2_investigator, l3_resolver
"""

import os
from functools import lru_cache

from langchain_groq import ChatGroq
from langchain_core.language_models import BaseChatModel
from loguru import logger
from dotenv import load_dotenv

from infrastructure.config import get_config, get_models, get_groq_model

load_dotenv()


def _build_groq_llm(tier: str) -> ChatGroq:
    """
    Build a ChatGroq instance for the given model tier.

    Args:
        tier: "small" | "large" | "reason"

    Returns:
        Configured ChatGroq instance.
    """
    cfg     = get_config()
    models  = get_models()

    model_name  = get_groq_model(tier)
    model_cfg   = models["groq"]["chat"][tier]

    # Per-tier temperature from param.yaml
    llm_cfg     = cfg.get("llm", {})
    temp_map    = llm_cfg.get("temperature", {})
    temperature = temp_map.get(tier, temp_map.get("default", 0.1))

    # Per-tier max_tokens from param.yaml
    tokens_map  = llm_cfg.get("max_tokens", {})
    max_tokens  = tokens_map.get(tier, tokens_map.get("default", 1000))

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY not set in .env")

    logger.debug(
        "Building Groq LLM | tier={} model={} temp={} max_tokens={}",
        tier, model_name, temperature, max_tokens,
    )

    return ChatGroq(
        model        = model_name,
        api_key      = api_key,
        temperature  = temperature,
        max_tokens   = max_tokens,
    )


# ── Cached singletons — built once, reused across all agent calls ─────────────

@lru_cache(maxsize=1)
def get_small_llm() -> BaseChatModel:
    """
    Returns the small/fast LLM (llama-3.1-8b-instant).

    Used by:
      - guardrail.py     (PASS/FAIL classification)
      - l1_triage.py     (ticket type + severity classification)
      - distiller.py     (bulk transcript extraction)
      - cag_populator.py (FAQ answer generation)
    """
    return _build_groq_llm("small")


@lru_cache(maxsize=1)
def get_large_llm() -> BaseChatModel:
    """
    Returns the large/reasoning LLM (llama-3.3-70b-versatile).

    Used by:
      - l2_investigator.py  (synthesise metrics + history into findings)
      - l3_resolver.py      (propose + plan remediation from runbook)
    """
    return _build_groq_llm("large")


@lru_cache(maxsize=1)
def get_reason_llm() -> BaseChatModel:
    """
    Returns the reasoning LLM (deepseek-r1-distill-llama-70b).

    Optional — available for Type 4 critical outages that need
    chain-of-thought reasoning. Not wired to any agent by default.
    """
    return _build_groq_llm("reason")


def get_llm_for_agent(agent_name: str) -> BaseChatModel:
    """
    Resolve the correct LLM for an agent using the routing table in models.yaml.

    Args:
        agent_name: e.g. "l1_triage", "l2_investigator", "guardrail"

    Returns:
        Appropriate LLM instance.
    """
    models   = get_models()
    routing  = models.get("routing", {})
    tier     = routing.get(agent_name, "small")

    if tier == "small":
        return get_small_llm()
    elif tier == "large":
        return get_large_llm()
    elif tier == "reason":
        return get_reason_llm()
    elif tier == "none":
        raise ValueError(f"Agent '{agent_name}' does not use an LLM (tier=none in models.yaml)")
    else:
        logger.warning("Unknown tier '{}' for agent '{}' — defaulting to small", tier, agent_name)
        return get_small_llm()