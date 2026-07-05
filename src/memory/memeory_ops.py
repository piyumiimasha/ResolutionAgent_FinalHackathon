"""
memory_ops.py — Distiller and Recaller.

The most important file in the memory folder.

Distiller: Decides what's worth saving to LT memory after a conversation.
Recaller:  Assembles context from all 4 tiers before each agent LLM call.

Both are token-budgeted — they never exceed MAX_CONTEXT_TOKENS.

Called by:
  - orchestrator.py write_back_node → Distiller.distill()
  - l1_triage, l2_investigator, l3_resolver → Recaller.recall()
"""

import os
import json
from datetime import datetime, timezone

from groq import AsyncGroq
from loguru import logger
from dotenv import load_dotenv

from memory.schemas import ConversationTurn, MemoryFact, RecalledContext
from memory.st_store import STStore
from memory.lt_store import LTStore
from memory.episodic_store import EpisodicStore
from memory.procedural_store import ProceduralStore
from memory.prompts import DISTILLATION_PROMPT, LT_CONFIDENCE_THRESHOLD

load_dotenv()

GROQ_API_KEY       = os.getenv("GROQ_API_KEY")
DISTILL_MODEL      = "llama-3.1-8b-instant"   # fast + cheap
MAX_CONTEXT_TOKENS = int(os.getenv("MAX_CONTEXT_TOKENS", "1500"))


# ── Distiller ─────────────────────────────────────────────────────────────────

class Distiller:
    """
    Runs after a ticket resolves (in write_back_node).

    Takes the full conversation history, sends to Groq LLM,
    extracts durable facts, saves high-confidence ones to LT memory.

    Usage:
        distiller = Distiller()
        await distiller.distill(conversation_history, resolution)
    """

    def __init__(self):
        self._groq   = AsyncGroq(api_key=GROQ_API_KEY)
        self._lt     = LTStore()

    async def distill(
        self,
        turns:      list[ConversationTurn],
        resolution: str = "",
        source:     str = "",
    ) -> list[MemoryFact]:
        """
        Extract and store durable facts from a resolved conversation.

        Args:
            turns:      Full conversation history (list of ConversationTurn).
            resolution: Final resolution text from L3.
            source:     ticket_id for attribution.

        Returns:
            List of MemoryFact dicts that were stored (confidence >= threshold).
        """
        if not turns:
            return []

        # ── Format conversation for LLM ───────────────────────────────
        conversation_text = "\n".join([
            f"{t['role'].upper()}: {t['content']}"
            for t in turns
        ])

        try:
            response = await self._groq.chat.completions.create(
                model    = DISTILL_MODEL,
                messages = [{
                    "role":    "user",
                    "content": DISTILLATION_PROMPT.format(
                        conversation = conversation_text,
                        resolution   = resolution or "Not recorded.",
                    ),
                }],
                temperature     = 0.1,
                max_tokens      = 500,
                response_format = {"type": "json_object"},
            )

            raw = response.choices[0].message.content.strip()

            # Handle both array and object responses
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                # LLM wrapped it in an object e.g. {"facts": [...]}
                facts_raw = parsed.get("facts", parsed.get("items", []))
            else:
                facts_raw = parsed

        except json.JSONDecodeError as exc:
            logger.error("Distiller JSON parse error: {}", exc)
            return []
        except Exception as exc:
            logger.error("Distiller LLM call failed: {}", exc)
            return []

        # ── Filter by confidence and store ────────────────────────────
        stored_facts = []
        for item in facts_raw:
            confidence = float(item.get("confidence", 0.0))
            if confidence < LT_CONFIDENCE_THRESHOLD:
                logger.debug(
                    "Distiller discarding low-confidence fact ({:.0%}): {:.60s}",
                    confidence, item.get("fact", ""),
                )
                continue

            fact = MemoryFact(
                fact       = item.get("fact", "").strip(),
                confidence = confidence,
                category   = item.get("category", "pattern"),
                source     = source,
                created_at = datetime.now(timezone.utc).isoformat(),
            )

            if fact["fact"]:
                await self._lt.store_fact(fact)
                stored_facts.append(fact)
                logger.debug(
                    "Distiller stored fact [{} {:.0%}]: {:.80s}",
                    fact["category"], confidence, fact["fact"],
                )

        logger.info(
            "Distiller: {} facts extracted, {} stored (threshold {:.0%})",
            len(facts_raw), len(stored_facts), LT_CONFIDENCE_THRESHOLD,
        )
        return stored_facts


# ── Recaller ──────────────────────────────────────────────────────────────────

class Recaller:
    """
    Assembles memory context from all 4 tiers before each agent LLM call.

    Priority order (highest → lowest):
      1. Short-term  — what was just said in this conversation
      2. Long-term   — durable facts about this system/server
      3. Episodic    — past incidents of the same ticket type
      4. Procedural  — historical SOPs from 1050 distilled transcripts

    Token-budgeted — stops adding context when MAX_CONTEXT_TOKENS is reached.

    Usage:
        recaller = Recaller()
        context_str = await recaller.recall(query="Redis OOM", ticket_id="TXN-001")
        # Inject context_str into your agent's system prompt
    """

    def __init__(self):
        self._st          = STStore()
        self._lt          = LTStore()
        self._episodic    = EpisodicStore()
        self._procedural  = ProceduralStore()

    async def recall(
        self,
        query:       str,
        ticket_id:   str,
        ticket_type: int = 1,
    ) -> str:
        """
        Pull and assemble memory context into a prompt-ready string.

        Args:
            query:       Current incident description / search query.
            ticket_id:   Active ticket ID (for ST memory lookup).
            ticket_type: Ticket type for episodic + procedural filtering.

        Returns:
            Formatted string ready to inject into a system prompt.
        """
        context = RecalledContext()
        token_budget = MAX_CONTEXT_TOKENS

        # ── Tier 1: Short-term (highest priority) ─────────────────────
        try:
            st_turns = await self._st.get_recent(ticket_id, n=8)
            if st_turns:
                context.recent_turns = st_turns
                token_budget -= self._estimate_tokens(
                    " ".join(t["content"] for t in st_turns)
                )
                logger.debug("Recaller: {} ST turns loaded", len(st_turns))
        except Exception as exc:
            logger.warning("Recaller ST fetch failed: {}", exc)

        # ── Tier 2: Long-term facts ────────────────────────────────────
        if token_budget > 200:
            try:
                lt_facts = await self._lt.search(query, top_k=5)
                if lt_facts:
                    context.known_facts = lt_facts
                    token_budget -= self._estimate_tokens(
                        " ".join(f["fact"] for f in lt_facts)
                    )
                    logger.debug("Recaller: {} LT facts loaded", len(lt_facts))
            except Exception as exc:
                logger.warning("Recaller LT search failed: {}", exc)

        # ── Tier 3: Episodic past incidents ───────────────────────────
        if token_budget > 200:
            try:
                episodes = await self._episodic.get_similar_episodes(
                    ticket_type  = ticket_type,
                    limit        = 2,
                    resolved_only= True,
                )
                if episodes:
                    context.past_episodes = episodes
                    token_budget -= self._estimate_tokens(
                        " ".join(ep.get("resolution", "") or "" for ep in episodes)
                    )
                    logger.debug("Recaller: {} past episodes loaded", len(episodes))
            except Exception as exc:
                logger.warning("Recaller episodic fetch failed: {}", exc)

        # ── Tier 4: Procedural SOPs ────────────────────────────────────
        if token_budget > 200:
            try:
                sops = await self._procedural.search_similar(
                    problem     = query,
                    ticket_type = ticket_type,
                    top_k       = 3,
                )
                if sops:
                    context.past_sops = sops
                    logger.debug("Recaller: {} procedural SOPs loaded", len(sops))
            except Exception as exc:
                logger.warning("Recaller procedural search failed: {}", exc)

        context.total_tokens_used = MAX_CONTEXT_TOKENS - token_budget
        logger.debug(
            "Recaller assembled context: ST={} LT={} Ep={} SOPs={} (~{} tokens)",
            len(context.recent_turns), len(context.known_facts),
            len(context.past_episodes), len(context.past_sops),
            context.total_tokens_used,
        )

        return context.to_prompt_string()

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """
        Rough token estimate — 1 token ≈ 4 characters.
        Good enough for budget tracking without a tokenizer dependency.
        """
        return len(text) // 4