"""
api/run.py — Uvicorn launcher.

Usage:
    python src/api/run.py
    # or via Makefile:
    make demo
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import uvicorn
from src.infastructure.config import get_config


def main():
    cfg     = get_config()
    api_cfg = cfg.get("api", {})

    uvicorn.run(
        "api.main:app",
        host    = api_cfg.get("host",    "0.0.0.0"),
        port    = api_cfg.get("port",    8000),
        reload  = api_cfg.get("reload",  True),
        workers = api_cfg.get("workers", 1),
        log_level = cfg.get("logging", {}).get("level", "info").lower(),
    )


if __name__ == "__main__":
    main()