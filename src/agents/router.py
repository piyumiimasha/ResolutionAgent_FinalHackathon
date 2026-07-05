"""
router.py — Second node in decision_graph (runs only if guardrail passed).

Responsibilities:
  - Query the CAG cache with the incoming ticket text.
  - If similarity score >= threshold → set cag_hit=True, attach cached answer.
  - If miss → set cag_hit=False, let decision_graph route to the swarm.

This node never calls the LLM. It is a pure cache lookup.
"""

import logging
from agents.state import AgentState
from services.cag_service.cache import CAGCache
from infrastructure.observability import get_tracer
from infrastructure.config import get_config

logger = logging.getLogger(__name__)
tracer = get_tracer()
config = get_config()

# Lazily initialised — avoids import-time Qdrant connection
_cag_cache: CAGCache | None = None


def _get_cache() -> CAGCache:
    global _cag_cache
    if _cag_cache is None:
        _cag_cache = CAGCache()
    return _cag_cache


async def router_node(state: AgentState) -> AgentState:
    """
    LangGraph node — CAG cache lookup.

    Args:
        state: AgentState after guardrail has passed (is_safe=True).

    Returns:
        Updated state with `cag_hit` and optionally `cag_answer` set.
    """
    ticket = state.get("ticket", "")

    with tracer.start_span("router_cag_lookup") as span:
        span.set_attribute("ticket_id", state.get("ticket_id", ""))

        try:
            cache = _get_cache()
            cached_answer = await cache.get(ticket)

            if cached_answer:
                logger.info(
                    "CAG cache HIT for ticket %s — skipping swarm",
                    state.get("ticket_id"),
                )
                span.set_attribute("cag_hit", True)
                return {
                    **state,
                    "cag_hit": True,
                    "cag_answer": cached_answer,
                    # Populate resolution too so the API response is uniform
                    "resolution": cached_answer,
                }
            else:
                logger.info(
                    "CAG cache MISS for ticket %s — routing to swarm",
                    state.get("ticket_id"),
                )
                span.set_attribute("cag_hit", False)
                return {**state, "cag_hit": False, "cag_answer": None}

        except Exception as exc:
            # Cache errors should never bring down the system
            # Fail open → route to swarm as if it were a miss
            logger.error("CAG cache lookup failed: %s — routing to swarm", exc)
            span.set_attribute("cag_hit", False)
            span.set_attribute("error", str(exc))
            return {**state, "cag_hit": False, "cag_answer": None}