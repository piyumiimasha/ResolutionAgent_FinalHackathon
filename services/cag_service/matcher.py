"""
cag_service/matcher.py — Semantic similarity matcher for CAG cache lookups.

Provides the scoring logic used by cache.py to decide if a query
is close enough to a cached question to be a hit.

Also used standalone for testing threshold calibration.
"""

from loguru import logger
from infastructure.config import get_config


class CAGMatcher:
    """
    Computes whether two texts are similar enough for a cache hit.

    The cache.py already gets a cosine similarity score from Qdrant —
    this class wraps the threshold logic and provides helpers for
    threshold calibration and score explanation.
    """

    def __init__(self):
        cfg = get_config()
        self._threshold = cfg.get("cag", {}).get("similarity_threshold", 0.82)

    def is_hit(self, score: float) -> bool:
        """
        Decide if a similarity score constitutes a cache hit.

        Args:
            score: Cosine similarity score from Qdrant (0.0 - 1.0).

        Returns:
            True if score meets or exceeds threshold.
        """
        return score >= self._threshold

    def explain(self, query: str, cached_query: str, score: float) -> str:
        """
        Human-readable explanation of a cache match decision.
        Useful for debugging threshold tuning.

        Args:
            query:        Incoming ticket text.
            cached_query: The cached question it was compared against.
            score:        Cosine similarity score.

        Returns:
            Explanation string.
        """
        verdict = "HIT" if self.is_hit(score) else "MISS"
        return (
            f"CAG {verdict} | score={score:.3f} threshold={self._threshold:.3f}\n"
            f"  Query : {query[:80]}\n"
            f"  Cached: {cached_query[:80]}"
        )

    def score_band(self, score: float) -> str:
        """
        Categorise a similarity score into a band for logging.

        Returns:
            "strong_hit" | "weak_hit" | "near_miss" | "miss"
        """
        if score >= 0.92:   return "strong_hit"
        if score >= self._threshold: return "weak_hit"
        if score >= 0.70:   return "near_miss"
        return "miss"