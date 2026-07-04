"""
ingest_runbooks.py — Chunk and embed runbook markdown files into Qdrant.

Reads all .md files from the runbooks/ directory,
splits into chunks, embeds via OpenRouter,
and upserts into the Qdrant `runbooks` collection.

Run once before starting the app (after schema.sql and distill_transcripts):
    python scripts/ingest_runbooks.py

Can be re-run anytime when runbooks are updated.
"""

import os
import re
import asyncio
from pathlib import Path

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance
from loguru import logger
from dotenv import load_dotenv

from infastructure.llm.embeddings import get_embeddings_batch
from infastructure.config import get_config, get_embedding_dimensions

load_dotenv()

RUNBOOKS_DIR    = Path("runbooks")
COLLECTION_NAME = "runbooks"
QDRANT_URL      = os.getenv("QDRANT_URL")
QDRANT_API_KEY  = os.getenv("QDRANT_API_KEY")


def _chunk_markdown(text: str, source: str, chunk_size: int, overlap: int) -> list[dict]:
    """
    Split a markdown file into overlapping chunks.
    Tries to split on headings first, then falls back to character splits.

    Args:
        text:       Full markdown text.
        source:     Filename for payload attribution.
        chunk_size: Target chars per chunk.
        overlap:    Overlap between chunks.

    Returns:
        List of chunk dicts with text, source, heading, chunk_index.
    """
    chunks  = []
    # Split on markdown headings (## or ###)
    sections = re.split(r'\n(#{1,3} .+)\n', text)

    current_heading = ""
    current_text    = ""

    for part in sections:
        if re.match(r'^#{1,3} ', part):
            current_heading = part.strip()
        else:
            current_text += part

            # If current_text exceeds chunk_size, split it
            while len(current_text) > chunk_size:
                chunk_text = current_text[:chunk_size]
                chunks.append({
                    "text":        chunk_text.strip(),
                    "source":      source,
                    "heading":     current_heading,
                    "chunk_index": len(chunks),
                })
                current_text = current_text[chunk_size - overlap:]

    # Add remaining text
    if current_text.strip():
        chunks.append({
            "text":        current_text.strip(),
            "source":      source,
            "heading":     current_heading,
            "chunk_index": len(chunks),
        })

    return chunks


async def ingest_runbooks() -> dict:
    """
    Main ingestion pipeline.

    1. Read all .md files from runbooks/
    2. Chunk each file
    3. Embed all chunks via OpenRouter
    4. Upsert to Qdrant runbooks collection

    Returns:
        Summary dict.
    """
    cfg       = get_config()
    rag_cfg   = cfg.get("rag", {})
    chunk_size = rag_cfg.get("chunk_size", 500)
    overlap    = rag_cfg.get("chunk_overlap", 50)
    dimensions = get_embedding_dimensions()

    qdrant = AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None)

    # ── Ensure collection exists ──────────────────────────────────────
    collections = {c.name for c in (await qdrant.get_collections()).collections}
    if COLLECTION_NAME not in collections:
        await qdrant.create_collection(
            collection_name = COLLECTION_NAME,
            vectors_config  = VectorParams(size=dimensions, distance=Distance.COSINE),
        )
        logger.info("Created Qdrant collection '{}'", COLLECTION_NAME)

    # ── Read and chunk all runbooks ───────────────────────────────────
    if not RUNBOOKS_DIR.exists():
        raise FileNotFoundError(f"runbooks/ directory not found at {RUNBOOKS_DIR.absolute()}")

    md_files = sorted(RUNBOOKS_DIR.glob("*.md"))
    if not md_files:
        raise FileNotFoundError("No .md files found in runbooks/ directory")

    logger.info("Found {} runbook files: {}", len(md_files), [f.name for f in md_files])

    all_chunks = []
    for md_file in md_files:
        text   = md_file.read_text(encoding="utf-8")
        chunks = _chunk_markdown(text, source=md_file.name, chunk_size=chunk_size, overlap=overlap)
        all_chunks.extend(chunks)
        logger.info("  {} → {} chunks", md_file.name, len(chunks))

    logger.info("Total chunks to embed: {}", len(all_chunks))

    # ── Embed all chunks ──────────────────────────────────────────────
    texts    = [c["text"] for c in all_chunks]
    vectors  = await get_embeddings_batch(texts)

    # ── Upsert to Qdrant ──────────────────────────────────────────────
    points = [
        PointStruct(
            id      = i,
            vector  = vectors[i],
            payload = all_chunks[i],
        )
        for i in range(len(all_chunks))
    ]

    batch_size = 50
    for i in range(0, len(points), batch_size):
        batch = points[i : i + batch_size]
        await qdrant.upsert(collection_name=COLLECTION_NAME, points=batch)
        logger.info("Upserted {}/{} chunks", min(i + batch_size, len(points)), len(points))

    summary = {
        "runbooks_ingested": len(md_files),
        "total_chunks":      len(all_chunks),
        "collection":        COLLECTION_NAME,
    }
    logger.info("Runbook ingestion complete: {}", summary)
    return summary


async def main():
    logger.info("=" * 60)
    logger.info("RUNBOOK INGESTION STARTING")
    logger.info("=" * 60)
    summary = await ingest_runbooks()
    logger.info("=" * 60)
    logger.info("DONE: {} runbooks → {} chunks", summary["runbooks_ingested"], summary["total_chunks"])
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())