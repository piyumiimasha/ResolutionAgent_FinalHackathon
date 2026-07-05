"""
llm/embeddings.py — Embedding factory via OpenRouter.

OpenRouter exposes OpenAI-compatible /embeddings endpoint,
so we use the openai client with a custom base_url.

All vector stores (Qdrant procedural_memory, cag_cache, runbooks)
and Supabase pgvector (lt_facts) use this single function.

Usage:
    from infrastructure.llm.embeddings import get_embedding, get_embeddings_batch

    vector = await get_embedding("Redis OOM on prod-cache-01")
    # returns list[float] of length 1536
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import os
import asyncio
from functools import lru_cache
from typing import List

from openai import AsyncOpenAI
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from dotenv import load_dotenv

from config import get_config, get_models, get_embedding_model, get_embedding_dimensions

load_dotenv()


@lru_cache(maxsize=1)
def _get_openrouter_client() -> AsyncOpenAI:
    """
    Build and cache the AsyncOpenAI client pointed at OpenRouter.
    OpenRouter is OpenAI API-compatible — only base_url changes.
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENROUTER_API_KEY not set in .env")

    models  = get_models()
    base_url = models["openrouter"]["embedding"]["default"].get(
        "base_url",
        "https://openrouter.ai/api/v1",
    )

    return AsyncOpenAI(
        api_key  = api_key,
        base_url = base_url,
    )


@retry(
    stop              = stop_after_attempt(3),
    wait              = wait_exponential(multiplier=1, min=1, max=8),
    retry             = retry_if_exception_type(Exception),
    reraise           = True,
)
async def get_embedding(text: str) -> List[float]:
    """
    Embed a single text string via OpenRouter (text-embedding-3-small).

    Args:
        text: Text to embed. Will be truncated if too long.

    Returns:
        List of 1536 floats (cosine-normalised).
    """
    if not text or not text.strip():
        logger.warning("get_embedding called with empty text — returning zero vector")
        return [0.0] * get_embedding_dimensions()

    # Truncate to ~8000 chars to stay within model token limit
    text = text.strip()[:8000]

    client = _get_openrouter_client()
    model  = get_embedding_model()

    try:
        response = await client.embeddings.create(
            model = model,
            input = text,
        )
        vector = response.data[0].embedding
        logger.debug("Embedded {} chars → {} dims", len(text), len(vector))
        return vector

    except Exception as exc:
        logger.error("Embedding failed for text '{:.40s}...': {}", text, exc)
        raise


async def get_embeddings_batch(texts: List[str]) -> List[List[float]]:
    """
    Embed multiple texts in batches to respect API rate limits.

    Args:
        texts: List of strings to embed.

    Returns:
        List of embedding vectors in the same order as input.
    """
    cfg        = get_config()
    batch_size = cfg.get("embedding", {}).get("batch_size", 50)
    client     = _get_openrouter_client()
    model      = get_embedding_model()
    all_vectors: List[List[float]] = []

    for i in range(0, len(texts), batch_size):
        batch        = texts[i : i + batch_size]
        clean_batch  = [t.strip()[:8000] if t and t.strip() else " " for t in batch]

        try:
            response = await client.embeddings.create(
                model = model,
                input = clean_batch,
            )
            # OpenRouter returns embeddings in order
            batch_vectors = [item.embedding for item in sorted(response.data, key=lambda x: x.index)]
            all_vectors.extend(batch_vectors)

            logger.debug(
                "Batch embedded {}/{} texts",
                min(i + batch_size, len(texts)), len(texts),
            )

            # Small delay between batches to avoid rate limiting
            if i + batch_size < len(texts):
                await asyncio.sleep(0.5)

        except Exception as exc:
            logger.error("Batch embedding failed at index {}: {}", i, exc)
            # Fill failed batch with zero vectors so pipeline doesn't crash
            all_vectors.extend([[0.0] * get_embedding_dimensions()] * len(batch))

    return all_vectors