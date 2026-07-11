"""
observability.py — Langfuse tracing + prompt management.

Provides:
  - get_tracer()     → returns a LangfuseTracer used in every agent node
  - get_langfuse()   → raw Langfuse client for custom spans
  - LangfuseTracer   → thin wrapper so agent code stays clean
  - flush_traces()   → flush pending events on shutdown

Every agent node wraps its work in:
    with tracer.start_span("l1_triage") as span:
        span.set_attribute("ticket_id", ...)

This produces the full trace chain in Langfuse:
  decision_graph → guardrail → router → l1 → l2 → l3
"""

import os
from contextlib import contextmanager
from typing import Any, Generator

from langfuse import Langfuse
from loguru import logger
from dotenv import load_dotenv

from infrastructure.config import get_config

load_dotenv()


# ── Langfuse client singleton ─────────────────────────────────────────────────

_langfuse_client: Langfuse | None = None


def get_langfuse() -> Langfuse | None:
    """
    Returns the Langfuse client singleton.
    Returns None if credentials are missing or observability is disabled.
    """
    global _langfuse_client

    cfg = get_config()
    if not cfg.get("observability", {}).get("enabled", True):
        return None

    if _langfuse_client is None:
        public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
        host       = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")

        if not public_key or not secret_key:
            logger.warning(
                "Langfuse keys not set — observability disabled. "
                "Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in .env"
            )
            return None

        try:
            _langfuse_client = Langfuse(
                public_key = public_key,
                secret_key = secret_key,
                host       = host,
            )
            logger.info("Langfuse client initialised (host={})", host)
        except Exception as exc:
            logger.warning("Langfuse init failed: {} — observability disabled", exc)
            return None

    return _langfuse_client


# ── Span wrapper ──────────────────────────────────────────────────────────────

class _Span:
    """
    Thin wrapper around a Langfuse span.
    All methods are no-ops if Langfuse is not configured — the app
    never crashes because of a missing trace.
    """

    def __init__(self, span: Any | None):
        self._span = span

    def set_attribute(self, key: str, value: Any) -> None:
        """Attach metadata to this span."""
        if self._span is not None:
            try:
                self._span.update(metadata={key: str(value)})
            except Exception:
                pass

    def set_error(self, error: Exception) -> None:
        """Mark this span as errored."""
        if self._span is not None:
            try:
                self._span.update(
                    level          = "ERROR",
                    status_message = str(error),
                )
            except Exception:
                pass

    def end(self) -> None:
        """End this span."""
        if self._span is not None:
            try:
                self._span.end()
            except Exception:
                pass


# ── Tracer ────────────────────────────────────────────────────────────────────

class LangfuseTracer:
    """
    Context-manager-based tracer used in every agent node.

    Usage:
        tracer = get_tracer()

        with tracer.start_span("l1_triage") as span:
            span.set_attribute("ticket_id", state["ticket_id"])
            span.set_attribute("severity", "critical")
            # ... do work ...
            # span auto-ends on context exit
    """

    def __init__(self, trace_name: str = "it_support_swarm"):
        self._lf         = get_langfuse()
        self._trace_name = trace_name
        self._trace_id   = None

        if self._lf is not None:
            try:
                # Create a root trace — all spans nest under this
                trace = self._lf.trace(name=trace_name)
                self._trace_id = trace.id
                logger.debug("Langfuse trace created: id={}", self._trace_id)
            except Exception:
                # .trace() failed — fall back to span-level tracing
                self._trace_id = None

    @contextmanager
    def start_span(self, name: str) -> Generator[_Span, None, None]:
        """
        Context manager that creates a Langfuse span and yields a _Span wrapper.
        Falls back to a no-op span if Langfuse is unavailable.

        Args:
            name: Span name e.g. "l1_triage", "l2_investigator", "l3_resolver"
        """
        raw_span = None

        if self._lf is not None:
            try:
                if self._trace_id is not None:
                    # Preferred: nest span under the root trace
                    raw_span = self._lf.span(
                        name     = name,
                        trace_id = self._trace_id,
                    )
                else:
                    # Fallback: create span as its own trace
                    raw_span = self._lf.span(
                        name       = name,
                        trace_name = self._trace_name,
                    )
            except Exception as exc:
                logger.warning("Failed to create Langfuse span '{}': {}", name, exc)
                raw_span = None

        span = _Span(raw_span)
        try:
            yield span
        except Exception as exc:
            span.set_error(exc)
            raise
        finally:
            span.end()


# ── Singleton factory ─────────────────────────────────────────────────────────

_tracer: LangfuseTracer | None = None


def get_tracer() -> LangfuseTracer:
    """
    Returns the singleton LangfuseTracer.
    Safe to call before Langfuse is configured — falls back to no-op spans.
    """
    global _tracer
    if _tracer is None:
        _tracer = LangfuseTracer(trace_name="it_support_swarm")
    return _tracer


def flush_traces() -> None:
    """
    Flush all pending Langfuse events to the server.
    Call after every request during dev so traces appear immediately.
    Call during app shutdown to avoid losing trace data.
    """
    lf = get_langfuse()
    if lf is not None:
        try:
            lf.flush()
            logger.debug("Langfuse traces flushed.")
        except Exception as exc:
            logger.warning("Langfuse flush failed: {}", exc)