"""
prompts.py — LLM prompt strings used by memory_ops.py (Distiller + Recaller).
Kept separate so prompts can be tuned without touching memory logic.
"""

# ── Distiller prompt ──────────────────────────────────────────────────────────

DISTILLATION_PROMPT = """
You are a knowledge extractor for an IT support system.

Analyse this resolved IT support conversation and extract ONLY facts that would
be valuable to remember for future incidents.

Focus on:
  - Infrastructure patterns   (e.g. "prod-cache-01 runs out of memory every ~30 days")
  - Config knowledge          (e.g. "Redis maxmemory was set to 2gb on 2024-06-15")
  - Team/ownership knowledge  (e.g. "Sarah from DevOps owns the Redis cluster")
  - Recurring failure modes   (e.g. "auth-service 502s correlate with deployments")
  - Resolution shortcuts      (e.g. "VPN issues almost always need credential reset, not reinstall")

DO NOT extract:
  - Conversational pleasantries ("thanks", "you're welcome")
  - Obvious facts ("the server was down")
  - False starts or failed attempts that were not the real resolution
  - Anything with low confidence or speculation

For each fact, assign a confidence score (0.0-1.0) and a category:
  server | service | employee | pattern | config

Respond with ONLY valid JSON array, no markdown:
[
  {
    "fact": "...",
    "confidence": 0.9,
    "category": "server"
  }
]

If no facts worth remembering exist, return: []

CONVERSATION:
{conversation}

RESOLUTION:
{resolution}
"""

# ── Recaller assembly prompt ──────────────────────────────────────────────────

RECALL_CONTEXT_PROMPT = """
You are investigating the following IT incident:
{current_query}

Here is relevant context retrieved from memory systems:

{assembled_context}

Use this context to inform your analysis. 
Prioritise recent conversation turns and high-confidence facts.
If memory suggests a known pattern or past fix, reference it explicitly.
"""

# ── Confidence threshold for LT storage ──────────────────────────────────────
# Facts below this threshold are discarded by the Distiller
LT_CONFIDENCE_THRESHOLD = 0.70