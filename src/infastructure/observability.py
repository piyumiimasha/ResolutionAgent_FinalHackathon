"""
observability.py — Langfuse tracing + prompt management.

Provides:
  - get_tracer()     → returns a LangfuseTracer context manager used in every agent node
  - get_langfuse()   → raw Langfuse client for custom spans
  - LangfuseTracer   → thin wrapper so agent code stays clean

Every agent node wraps its work in:
    with tracer.start_span("l1_triage") as span:
        span.set_attribute("ticket_id", ...)

This produces the full trace chain in Langfuse:
  decision_graph → guardrail → router → swarm → l1 → l2 → l3 → write_back
"""

import os
from contextlib import contextmanager
from typing import Any, Generator

from langfuse import Langfuse
from langfuse.callback import CallbackHandler
from loguru import logger
from dotenv import load_dotenv

from config import get_config

load_dotenv()


# ── Langfuse client singleton ─────────────────────────────────────────────────

_langfuse_client: Langfuse | None = None


def get_langfuse() -> Langfuse | None:
    """
    Returns the Langfuse client, or None if observability is disabled
    or credentials are missing.
    """
    global _langfuse_client
    cfg = get_config()

    if not cfg.get("observability", {}).get("enabled", True):
        return None

    if _langfuse_client is None:
        public_key  = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        secret_key  = os.getenv("LANGFUSE_SECRET_KEY", "")
        host        = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")

        if not public_key or not secret_key:
            logger.warning(
                "Langfuse keys not set — observability disabled. "
                "Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in .env"
            )
            return None

        _langfuse_client = Langfuse(
            public_key = public_key,
            secret_key = secret_key,
            host       = host,
        )
        logger.info("Langfuse client initialised (host={})", host)

    return _langfuse_client


def get_langfuse_callback() -> CallbackHandler | None:
    """
    Returns a LangChain-compatible Langfuse CallbackHandler.
    Pass this to LLM calls to auto-trace them:
        llm.ainvoke(messages, config={"callbacks": [get_langfuse_callback()]})
    """
    lf = get_langfuse()
    if lf is None:
        return None
    return CallbackHandler(
        public_key = os.getenv("LANGFUSE_PUBLIC_KEY"),
        secret_key = os.getenv("LANGFUSE_SECRET_KEY"),
        host       = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
    )


# ── Span wrapper ──────────────────────────────────────────────────────────────

class _Span:
    """
    Thin wrapper around a Langfuse span.
    Falls back to no-op if Langfuse is not configured.
    """

    def __init__(self, span: Any | None):
        self._span = span

    def set_attribute(self, key: str, value: Any) -> None:
        if self._span is not None:
            try:
                self._span.update(metadata={key: value})
            except Exception:
                pass    # never let observability crash the main flow

    def set_error(self, error: Exception) -> None:
        if self._span is not None:
            try:
                self._span.update(
                    level      = "ERROR",
                    status_message = str(error),
                )
            except Exception:
                pass

    def end(self) -> None:
        if self._span is not None:
            try:
                self._span.end()
            except Exception:
                pass


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
        self._trace      = None

        if self._lf is not None:
            try:
                self._trace = self._lf.trace(name=trace_name)
            except Exception as exc:
                logger.warning("Failed to create Langfuse trace: {}", exc)

    @contextmanager
    def start_span(self, name: str) -> Generator[_Span, None, None]:
        """
        Context manager that creates a Langfuse span and yields a _Span wrapper.

        Args:
            name: Span name e.g. "l1_triage", "cag_lookup", "write_back"
        """
        raw_span = None
        if self._trace is not None:
            try:
                raw_span = self._trace.span(name=name)
            except Exception as exc:
                logger.warning("Failed to create Langfuse span '{}': {}", name, exc)

        span = _Span(raw_span)
        try:
            yield span
        except Exception as exc:
            span.set_error(exc)
            raise
        finally:
            span.end()


# ── Singleton tracer factory ──────────────────────────────────────────────────

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
    Flush pending Langfuse events to the server.
    Call during app shutdown to avoid losing trace data.
    """
    lf = get_langfuse()
    if lf is not None:
        try:
            lf.flush()
            logger.debug("Langfuse traces flushed.")
        except Exception as exc:
            logger.warning("Langfuse flush failed: {}", exc)