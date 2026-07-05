"""
mcp_config.py — Launch configuration for all 3 MCP servers.

Two modes:
  1. stdio (default) — each server runs as a subprocess, communicates via stdin/stdout.
     Used when agents call MCP servers as child processes.

  2. direct (dev/test) — import client wrappers directly, no subprocess.
     Used in unit tests or when you want to skip MCP overhead locally.

Usage in agents:
    from mcp_servers.mcp_config import get_mcp_mode
    if get_mcp_mode() == "direct":
        from mcp_servers.ticketing_server import TicketingMCPClient
    else:
        # use mcp subprocess client
"""

import os
from pathlib import Path

# ── MCP server process definitions ───────────────────────────────────────────
# These are the configs passed to LangChain MCP client or stdio subprocess

SRC_DIR = Path(__file__).parent.parent  # /src

MCP_SERVERS = {
    "ticketing": {
        "command": "python",
        "args":    [str(SRC_DIR / "mcp_servers" / "ticketing_server.py")],
        "env":     {
            "SUPABASE_URL": os.getenv("SUPABASE_URL", ""),
            "SUPABASE_KEY": os.getenv("SUPABASE_KEY", ""),
        },
        "description": "CRUD on tickets table",
        "tools": [
            "create_ticket",
            "get_ticket",
            "update_ticket",
            "list_tickets",
            "close_ticket",
        ],
    },
    "observability": {
        "command": "python",
        "args":    [str(SRC_DIR / "mcp_servers" / "observability_server.py")],
        "env":     {
            "SUPABASE_URL": os.getenv("SUPABASE_URL", ""),
            "SUPABASE_KEY": os.getenv("SUPABASE_KEY", ""),
        },
        "description": "Query server_metrics, services, incident_history",
        "tools": [
            "get_server_metrics",
            "get_service_status",
            "list_unhealthy",
            "get_incident_history",
            "query_metrics_range",
        ],
    },
    "action": {
        "command": "python",
        "args":    [str(SRC_DIR / "mcp_servers" / "action_server.py")],
        "env":     {
            "SUPABASE_URL": os.getenv("SUPABASE_URL", ""),
            "SUPABASE_KEY": os.getenv("SUPABASE_KEY", ""),
        },
        "description": "Mock remediation actions (restart, scale, flush, rotate)",
        "tools": [
            "restart_service",
            "scale_service",
            "flush_cache",
            "rotate_credentials",
            "run_runbook_step",
        ],
    },
}


def get_mcp_mode() -> str:
    """
    Returns "direct" or "stdio" based on MCP_MODE env var.
    Default is "direct" for development convenience.
    """
    return os.getenv("MCP_MODE", "direct")


def get_server_config(server_name: str) -> dict:
    """
    Get the launch config for a specific MCP server.

    Args:
        server_name: "ticketing" | "observability" | "action"

    Returns:
        Config dict with command, args, env, tools.
    """
    if server_name not in MCP_SERVERS:
        raise ValueError(f"Unknown MCP server: {server_name}. Must be one of {list(MCP_SERVERS)}")
    return MCP_SERVERS[server_name]


def list_all_tools() -> dict[str, list[str]]:
    """
    Returns a flat map of server_name → list of tool names.
    Useful for debugging and logging.
    """
    return {name: cfg["tools"] for name, cfg in MCP_SERVERS.items()}