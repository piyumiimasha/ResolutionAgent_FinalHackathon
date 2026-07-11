"""
log.py — Loguru logging setup.

Call configure_logging() once at app startup (in api/main.py).
All other modules just do `from loguru import logger` and use it directly.

Features:
  - Coloured console output in development
  - JSON-structured output in production (for log aggregators)
  - Intercepts standard library logging (so supabase, httpx etc. all go through loguru)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import sys
import logging
from infrastructure.config import get_config


class _InterceptHandler(logging.Handler):
    """
    Redirects standard library log records into loguru.
    So boto3, supabase-py, httpx, uvicorn all appear in the same stream.
    """
    def emit(self, record: logging.LogRecord) -> None:
        from loguru import logger
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = sys._getframe(6), 6
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def configure_logging() -> None:
    """
    Set up loguru for the entire application.
    Call once at startup before anything else logs.
    """
    from loguru import logger
    cfg = get_config()

    log_cfg  = cfg.get("logging", {})
    level    = log_cfg.get("level", "INFO")
    colorize = log_cfg.get("colorize", True)
    fmt      = log_cfg.get(
        "format",
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>",
    )

    # Remove default loguru handler
    logger.remove()

    if cfg.get("environment") == "production":
        # JSON output for production log aggregators (CloudWatch, Datadog etc.)
        logger.add(
            sys.stdout,
            level    = level,
            format   = "{time} {level} {name} {message}",
            colorize = False,
            serialize= True,    # outputs JSON
        )
    else:
        # Human-readable coloured output for development
        logger.add(
            sys.stdout,
            level    = level,
            format   = fmt,
            colorize = colorize,
        )

    # Intercept stdlib logging
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)

    # Suppress noisy third-party loggers
    for noisy in ("httpx", "httpcore", "azure", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger.info("Logging configured | level={} env={}", level, cfg.get("environment"))