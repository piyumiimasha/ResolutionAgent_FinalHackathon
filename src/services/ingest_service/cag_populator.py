"""
cag_populator.py — Seeds the CAG cache from high-frequency Type 1 patterns.

Scans all procedural_memory records in Qdrant,
groups Type 1 (Access & Identity) records by problem keyword clusters,
generates a canonical FAQ answer for each high-frequency cluster via Groq,
writes results to the cag_cache Qdrant collection.

Run after distill_transcripts.py:
    python scripts/rebuild_cag_cache.py
"""

import os
import asyncio

from groq import AsyncGroq
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    PointStruct, VectorParams, Distance,
    Filter, FieldCondition, MatchValue,
    PayloadSchemaType,  
)

from loguru import logger
from dotenv import load_dotenv

from infastructure.llm.embeddings import get_embedding
from infastructure.config import get_config, get_embedding_dimensions

load_dotenv()

GROQ_API_KEY      = os.getenv("GROQ_API_KEY")
QDRANT_URL        = os.getenv("QDRANT_URL")
QDRANT_API_KEY    = os.getenv("QDRANT_API_KEY")
SOURCE_COLLECTION = "procedural_memory"
CAG_COLLECTION    = "cag_cache"
GROQ_MODEL        = "llama-3.1-8b-instant"
SCROLL_LIMIT      = 100


CAG_ANSWER_PROMPT = """
You are an IT support FAQ generator.

Below are {count} similar IT support problems that all had the same type of resolution.
Generate a clear, step-by-step answer that covers this category of issue.

Requirements:
- Written for a non-technical employee
- Under 150 words
- Include specific steps where possible
- Professional tone

PROBLEMS AND RESOLUTIONS:
{examples}

Respond with ONLY the answer text. No preamble, no JSON.
"""

# Keyword clusters for Type 1 (Access & Identity) tickets
KEYWORD_MAP = {
    "vpn":            ["vpn", "tunnel", "remote access", "openvpn", "wireguard"],
    "password_reset": ["password", "passwd", "credentials", "reset password"],
    "mfa_totp":       ["mfa", "totp", "authenticator", "2fa", "otp", "token", "qr code", "clock drift"],
    "account_locked": ["locked", "lock out", "lockout", "account locked", "blocked", "3 attempts"],
    "sso":            ["sso", "single sign", "saml", "okta", "azure ad"],
    "permissions":    ["permission", "access denied", "403", "unauthorized", "role"],
}


async def _scroll_type1_records(qdrant: AsyncQdrantClient) -> list[dict]:
    """
    Scroll all Type 1 records from procedural_memory.

    Returns:
        List of payload dicts.
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
            limit        = SCROLL_LIMIT,
            offset       = offset,
            with_payload = True,
            with_vectors = False,
        )
        records.extend([r.payload for r in results])
        if next_offset is None:
            break
        offset = next_offset

    logger.info("Scrolled {} Type 1 records from procedural_memory.", len(records))
    return records


def _cluster_records(records: list[dict]) -> dict[str, list[dict]]:
    """
    Group records into clusters by keyword matching on problem field.

    Args:
        records: List of Qdrant payload dicts.

    Returns:
        Dict mapping cluster_label → list of matching records.
    """
    clusters: dict[str, list[dict]] = {k: [] for k in KEYWORD_MAP}
    clusters["other_access"] = []

    for record in records:
        problem_lower = record.get("problem", "").lower()
        matched = False
        for cluster, keywords in KEYWORD_MAP.items():
            if any(kw in problem_lower for kw in keywords):
                clusters[cluster].append(record)
                matched = True
                break
        if not matched:
            clusters["other_access"].append(record)

    # Remove empty clusters
    return {k: v for k, v in clusters.items() if v}


async def _generate_answer(
    client: AsyncGroq,
    cluster_label: str,
    records: list[dict],
) -> str:
    """
    Generate a canonical FAQ answer for a cluster via Groq.

    Args:
        client:        AsyncGroq client.
        cluster_label: Cluster name for logging.
        records:       Matching records (up to 5 used as examples).

    Returns:
        Generated answer string.
    """
    examples = "\n\n".join([
        f"Problem: {r.get('problem', '')}\nResolution: {r.get('resolution', '')}"
        for r in records[:5]
    ])

    response = await client.chat.completions.create(
        model       = GROQ_MODEL,
        messages    = [{
            "role":    "user",
            "content": CAG_ANSWER_PROMPT.format(
                count    = len(records),
                examples = examples,
            ),
        }],
        temperature = 0.3,
        max_tokens  = 200,
    )
    return response.choices[0].message.content.strip()


async def _ensure_cag_collection(qdrant: AsyncQdrantClient) -> None:
    """Create cag_cache collection if it doesn't exist."""
    dimensions  = get_embedding_dimensions()
    collections = {c.name for c in (await qdrant.get_collections()).collections}

    if CAG_COLLECTION not in collections:
        await qdrant.create_collection(
            collection_name = CAG_COLLECTION,
            vectors_config  = VectorParams(size=dimensions, distance=Distance.COSINE),
        )
        logger.info("Created Qdrant collection '{}'.", CAG_COLLECTION)

async def _ensure_procedural_index(qdrant: AsyncQdrantClient) -> None:
    """Create payload index on ticket_type_int in procedural_memory if missing."""
    try:
        await qdrant.create_payload_index(
            collection_name = SOURCE_COLLECTION,
            field_name      = "ticket_type_int",
            field_schema    = PayloadSchemaType.INTEGER,
        )
        logger.info("Payload index on 'ticket_type_int' ready.")
    except Exception as exc:
        if "already exists" in str(exc).lower():
            logger.debug("Index already exists, skipping.")
        else:
            raise

async def run_cag_population() -> dict:
    """
    Main CAG population pipeline.

    1. Scroll Type 1 records from procedural_memory
    2. Cluster by keyword
    3. For clusters >= MIN_FREQUENCY, generate FAQ answer via Groq
    4. Embed cluster label + upsert to cag_cache

    Returns:
        Summary dict.
    """
    cfg         = get_config()
    min_freq    = cfg.get("cag", {}).get("min_frequency", 5)

    groq_client = AsyncGroq(api_key=GROQ_API_KEY)
    qdrant      = AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None)

    await _ensure_cag_collection(qdrant)
    await _ensure_procedural_index(qdrant)

    # ── 1. Get all Type 1 records ─────────────────────────────────────
    records  = await _scroll_type1_records(qdrant)
    clusters = _cluster_records(records)

    logger.info(
        "Clusters: {}",
        {k: len(v) for k, v in clusters.items()},
    )

    # ── 2. Process each cluster ───────────────────────────────────────
    points  = []
    seeded  = 0
    skipped = 0

    for point_id, (label, cluster_records) in enumerate(clusters.items()):
        if len(cluster_records) < min_freq:
            logger.info(
                "Skipping '{}' — {} records < min_frequency {}",
                label, len(cluster_records), min_freq,
            )
            skipped += 1
            continue

        logger.info(
            "Generating CAG answer for '{}' ({} records)...",
            label, len(cluster_records),
        )

        answer = await _generate_answer(groq_client, label, cluster_records)

        # Embed using the cluster label + first example problem
        embed_text = f"{label} {cluster_records[0].get('problem', '')}"
        vector     = await get_embedding(embed_text)

        points.append(PointStruct(
            id      = point_id,
            vector  = vector,
            payload = {
                "cluster_label":   label,
                "answer":          answer,
                "frequency":       len(cluster_records),
                "ticket_type_int": 1,
                "ticket_type_str": "access_identity",
                "source":          "cag_populator",
            },
        ))
        seeded += 1

    # ── 3. Upsert to Qdrant ───────────────────────────────────────────
    if points:
        await qdrant.upsert(collection_name=CAG_COLLECTION, points=points)
        logger.info("Upserted {} CAG entries to '{}'.", len(points), CAG_COLLECTION)

    summary = {
        "total_type1_records": len(records),
        "clusters_found":      len(clusters),
        "clusters_seeded":     seeded,
        "clusters_skipped":    skipped,
        "min_frequency":       min_freq,
    }
    logger.info("CAG population complete: {}", summary)
    return summary