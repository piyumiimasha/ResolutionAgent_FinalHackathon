"""
rag_service/chain.py — RAG chain wired to L3 resolver.

Combines CRAGRetriever with prompt formatting.
L3 calls retrieve_and_format() to get:
  1. A formatted context string (injected into L3 system prompt)
  2. The source runbook filename (for citation — required by grader)

Also handles the case where no runbook is found — L3 must still
produce a resolution, just without runbook backing.
"""

from loguru import logger
from services.rag_service.retriever import CRAGRetriever


class RAGChain:
    """
    RAG chain used by l3_resolver.py.

    Usage:
        rag = RAGChain()
        context, source = await rag.retrieve_and_format(
            query="Redis OOM on prod-cache-01",
            ticket_type=4,
        )
        # Inject context into L3 system prompt
        # Include source as citation in resolution
    """

    def __init__(self):
        self._retriever = CRAGRetriever()

    async def retrieve_and_format(
        self,
        query:       str,
        ticket_type: int = 1,
    ) -> tuple[str, str | None]:
        """
        Retrieve relevant runbook chunks and format them for the L3 prompt.

        Args:
            query:       L2 findings or original ticket text.
            ticket_type: Ticket type for context.

        Returns:
            Tuple of:
              - formatted_context: String ready to inject into L3 system prompt
              - source:            Runbook filename cited (e.g. "redis_oom.md") or None
        """
        chunks = await self._retriever.retrieve(query=query, ticket_type=ticket_type)

        if not chunks:
            logger.warning("RAGChain: No runbook chunks found for query: {:.60s}", query)
            return (
                "No matching runbook found. Use general IT best practices.",
                None,
            )

        # Get the primary source (highest scoring chunk)
        primary_source = chunks[0].get("source")

        # Format all chunks into a readable context block
        context_parts = []
        seen_sources  = set()

        for chunk in chunks:
            source  = chunk.get("source", "unknown")
            heading = chunk.get("heading", "")
            text    = chunk.get("text", "")
            score   = chunk.get("score", 0)

            # Add source header only once per file
            if source not in seen_sources:
                context_parts.append(f"\n--- Runbook: {source} (relevance: {score:.0%}) ---")
                seen_sources.add(source)

            if heading:
                context_parts.append(f"## {heading}")
            context_parts.append(text)

        formatted_context = "\n".join(context_parts).strip()

        logger.debug(
            "RAGChain: formatted {} chunks from {} runbook(s). Primary: {}",
            len(chunks), len(seen_sources), primary_source,
        )

        return formatted_context, primary_source

    async def get_runbook_list(self) -> list[str]:
        """
        Return list of unique runbook filenames in the collection.
        Useful for debugging and /health endpoint.
        """
        try:
            from qdrant_client import AsyncQdrantClient
            import os
            qdrant = AsyncQdrantClient(
                url     = os.getenv("QDRANT_URL"),
                api_key = os.getenv("QDRANT_API_KEY") or None,
            )
            # Scroll all points and collect unique sources
            results, _ = await qdrant.scroll(
                collection_name = "runbooks",
                with_payload    = True,
                with_vectors    = False,
                limit           = 1000,
            )
            sources = list({r.payload.get("source", "") for r in results if r.payload})
            return sorted(sources)
        except Exception as exc:
            logger.error("RAGChain.get_runbook_list failed: {}", exc)
            return []