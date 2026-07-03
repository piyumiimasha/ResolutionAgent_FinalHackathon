"""
cag_populator.py — Identifies high-frequency Tier 1 questions from distilled
records and seeds them into the CAG cache.

Logic:
  - Scans all procedural memory records in Qdrant
  - Groups by ticket_type_int == 1 (Access & Identity) — these are CAG candidates
  - Uses Groq to cluster similar problems and generate a canonical FAQ answer
  - Writes high-frequency clusters into the CAG cache (Qdrant cag_cache collection)

Run after distill_transcripts.py:
    python scripts/rebuild_cag_cache.py
"""

import os
import json
import asyncio
from collections import defaultdict

from groq import AsyncGroq
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance, Filter, FieldCondition, MatchValue
from loguru import logger
from dotenv import load_dotenv

from infastructure.llm.embeddings import get_embedding

load_dotenv()

QDRANT_URL          = os.getenv("QDRANT_URL")
QDRANT_API_KEY      = os.getenv("QDRANT_API_KEY")
GROQ_API_KEY        = os.getenv("GROQ_API_KEY")
SOURCE_COLLECTION   = "procedural_memory"
CAG_COLLECTION      = "cag_cache"
GROQ_MODEL          = "llama-3.1-8b-instant"
VECTOR_SIZE         = 1536
MIN_FREQUENCY       = int(os.getenv("CAG_MIN_FREQUENCY", "5"))   # min occurrences to cache
SCROLL_LIMIT        = 100   # records per scroll page


CAG_ANSWER_PROMPT = """
You are an IT support FAQ generator.

Below are {count} similar IT support problems that all had the same type of resolution.
Generate a clear, concise, step-by-step answer that covers this category of issue.

The answer should be:
- Written for a non-technical employee
- Under 150 words
- Include specific steps where possible
- Professional in tone

PROBLEMS AND RESOLUTIONS:
{examples}

Respond with ONLY the answer text. No preamble, no JSON.
"""


async def _scroll_tier1_records(qdrant: AsyncQdrantClient) -> list[dict]:
    """
    Scroll through all Type 1 (Access & Identity) records in procedural_memory.

    Returns:
        List of payload dicts for all Type 1 records.
    """
    records = []
    offset  = None

    while True:
        results, next_offset = await qdrant.scroll(
            collection_name = SOURCE_COLLECTION,
            scroll_filter   = Filter(
                must=[FieldCondition(
                    key   = "ticket_type_int",
                    match = MatchValue(value=1),
                )]
            ),
            limit           = SCROLL_LIMIT,
            offset          = offset,
            with_payload    = True,
            with_vectors    = False,
        )

        records.extend([r.payload for r in results])
        if next_offset is None:
            break
        offset = next_offset

    logger.info("Scrolled {} Type 1 records from procedural memory.", len(records))
    return records


def _cluster_by_keywords(records: list[dict]) -> dict[str, list[dict]]:
    """
    Simple keyword-based clustering to group similar problems.
    Groups by key terms found in the problem field.

    Args:
        records: List of payload dicts from Qdrant.

    Returns:
        Dict mapping cluster label → list of records.
    """
    # Keyword clusters for Access & Identity type tickets
    clusters = {
        "vpn":              [],
        "password_reset":   [],
        "mfa_totp":         [],
        "account_locked":   [],
        "sso":              [],
        "permissions":      [],
        "other_access":     [],
    }

    keyword_map = {
        "vpn":            ["vpn", "tunnel", "remote access", "openvpn", "wireguard"],
        "password_reset": ["password", "passwd", "credentials", "reset password"],
        "mfa_totp":       ["mfa", "totp", "authenticator", "2fa", "otp", "token", "qr code"],
        "account_locked": ["locked", "lock out", "lockout", "account locked", "blocked"],
        "sso":            ["sso", "single sign", "saml", "okta", "azure ad"],
        "permissions":    ["permission", "access denied", "403", "unauthorized", "role"],
    }

    for record in records:
        problem_lower = record.get("problem", "").lower()
        matched = False
        for cluster, keywords in keyword_map.items():
            if any(kw in problem_lower for kw in keywords):
                clusters[cluster].append(record)
                matched = True
                break
        if not matched:
            clusters["other_access"].append(record)

    # Remove empty clusters
    return {k: v for k, v in clusters.items() if v}


async def _generate_cag_answer(
    groq_client: AsyncGroq,
    cluster_label: str,
    records: list[dict],
) -> str:
    """
    Generate a canonical FAQ answer for a cluster using Groq.

    Args:
        groq_client:   AsyncGroq client.
        cluster_label: Name of the cluster (e.g. "vpn").
        records:       List of similar records from procedural memory.

    Returns:
        Generated answer string.
    """
    # Take up to 5 examples to keep prompt short
    examples = "\n\n".join([
        f"Problem: {r.get('problem', '')}\nResolution: {r.get('resolution', '')}"
        for r in records[:5]
    ])

    response = await groq_client.chat.completions.create(
        model   = GROQ_MODEL,
        messages= [{
            "role": "user",
            "content": CAG_ANSWER_PROMPT.format(count=len(records), examples=examples),
        }],
        temperature = 0.3,
        max_tokens  = 200,
    )
    return response.choices[0].message.content.strip()


async def _ensure_cag_collection(qdrant: AsyncQdrantClient) -> None:
    """Create CAG cache collection if it doesn't exist."""
    collections = await qdrant.get_collections()
    names = [c.name for c in collections.collections]
    if CAG_COLLECTION not in names:
        await qdrant.create_collection(
            collection_name = CAG_COLLECTION,
            vectors_config  = VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        logger.info("Created Qdrant collection '{}'.", CAG_COLLECTION)


async def run_cag_population() -> dict:
    """
    Main CAG population pipeline.

    1. Scroll Type 1 records from procedural_memory
    2. Cluster by problem keywords
    3. For clusters with >= MIN_FREQUENCY records, generate an FAQ answer
    4. Embed the cluster label and upsert into cag_cache

    Returns:
        Summary dict.
    """
    groq_client = AsyncGroq(api_key=GROQ_API_KEY)
    qdrant      = AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)

    await _ensure_cag_collection(qdrant)

    # ── 1. Get all Type 1 records ─────────────────────────────────────
    records  = await _scroll_tier1_records(qdrant)
    clusters = _cluster_by_keywords(records)

    logger.info("Clusters found: {}", {k: len(v) for k, v in clusters.items()})

    # ── 2. Process each cluster ───────────────────────────────────────
    points  = []
    seeded  = 0
    skipped = 0

    for point_id, (label, cluster_records) in enumerate(clusters.items()):
        if len(cluster_records) < MIN_FREQUENCY:
            logger.info(
                "Skipping cluster '{}' — only {} records (min: {})",
                label, len(cluster_records), MIN_FREQUENCY,
            )
            skipped += 1
            continue

        logger.info(
            "Generating CAG answer for cluster '{}' ({} records)...",
            label, len(cluster_records),
        )

        answer  = await _generate_cag_answer(groq_client, label, cluster_records)
        vector  = await get_embedding(f"{label} {cluster_records[0].get('problem', '')}")

        points.append(PointStruct(
            id      = point_id,
            vector  = vector,
            payload = {
                "cluster_label":    label,
                "answer":           answer,
                "frequency":        len(cluster_records),
                "ticket_type_int":  1,
                "ticket_type_str":  "access_identity",
                "source":           "cag_populator",
            },
        ))
        seeded += 1

    # ── 3. Upsert to Qdrant ───────────────────────────────────────────
    if points:
        await qdrant.upsert(collection_name=CAG_COLLECTION, points=points)

    summary = {
        "total_type1_records": len(records),
        "clusters_found":      len(clusters),
        "clusters_seeded":     seeded,
        "clusters_skipped":    skipped,
        "min_frequency":       MIN_FREQUENCY,
    }
    logger.info("CAG population complete: {}", summary)
    return summary
