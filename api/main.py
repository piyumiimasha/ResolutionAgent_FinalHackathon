"""
api/main.py — FastAPI app factory + lifespan.

Startup sequence:
  1. configure_logging()
  2. check_connection() for Supabase + Qdrant + Azure Blob
  3. ensure_collections() — create Qdrant collections if missing
  4. get_langfuse()       — init Langfuse tracing
  5. Mount routers

Shutdown:
  1. flush_traces() — flush pending Langfuse events
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from src.infastructure.log import configure_logging
from src.infastructure.config import get_config
from src.infastructure.observability import get_tracer, flush_traces
from src.infastructure.db.supabase_client import check_connection as supabase_health
from src.infastructure.db.qdrant_client import check_connection as qdrant_health, ensure_collections
from src.services.azure.blob_service_client  import check_connection as azure_health
from api.routers import chat, tickets, health


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle manager."""

    # ── Startup ───────────────────────────────────────────────────────
    configure_logging()
    cfg = get_config()
    logger.info("=" * 60)
    logger.info("IT Support Swarm starting up (env={})", cfg.get("environment"))
    logger.info("=" * 60)

    # Connection health checks
    sb_ok    = await supabase_health()
    qdrant_ok = await qdrant_health()
    azure_ok  = await azure_health()

    if not sb_ok:
        logger.warning("Supabase connection failed — ticket/memory operations may fail")
    if not qdrant_ok:
        logger.warning("Qdrant connection failed — RAG/CAG/procedural memory unavailable")
    if not azure_ok:
        logger.warning("Azure Blob connection failed — transcript reading unavailable")

    # Ensure Qdrant collections exist
    if qdrant_ok:
        await ensure_collections()

    # Init Langfuse tracer (no-op if keys not set)
    get_tracer()

    logger.info("Startup complete. API ready.")
    logger.info("  Supabase : {}", "OK" if sb_ok    else "DEGRADED")
    logger.info("  Qdrant   : {}", "OK" if qdrant_ok else "DEGRADED")
    logger.info("  Azure    : {}", "OK" if azure_ok  else "DEGRADED")

    yield  # app runs here

    # ── Shutdown ──────────────────────────────────────────────────────
    logger.info("Shutting down — flushing Langfuse traces...")
    flush_traces()
    logger.info("Shutdown complete.")


def create_app() -> FastAPI:
    """
    Build and configure the FastAPI application.

    Returns:
        Configured FastAPI instance.
    """
    cfg = get_config()
    api_cfg = cfg.get("api", {})

    app = FastAPI(
        title       = "IT Support Swarm API",
        description = "Enterprise IT Support & Incident Resolution — powered by LangGraph multi-agent swarm",
        version     = "1.0.0",
        lifespan    = lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────
    origins = api_cfg.get("cors_origins", ["http://localhost:3000"])
    app.add_middleware(
        CORSMiddleware,
        allow_origins     = origins,
        allow_credentials = True,
        allow_methods     = ["*"],
        allow_headers     = ["*"],
    )

    # ── Routers ───────────────────────────────────────────────────────
    app.include_router(health.router,  prefix="/health",  tags=["Health"])
    app.include_router(chat.router,    prefix="/chat",    tags=["Chat"])
    app.include_router(tickets.router, prefix="/tickets", tags=["Tickets"])

    return app


app = create_app()