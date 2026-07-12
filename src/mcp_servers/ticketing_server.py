"""
ticketing_server.py — Ticketing MCP Server.

Wraps the `tickets` Supabase table and exposes 5 tools:
  1. create_ticket      — insert a new ticket row
  2. get_ticket         — fetch a ticket by ID
  3. update_ticket      — update status / severity / assigned_to
  4. list_tickets       — list tickets with optional filters
  5. close_ticket       — mark ticket as resolved with resolution text

Called by: l1_triage.py (create/update), l3_resolver.py (close)

Supabase tickets table schema:
  id              TEXT PRIMARY KEY      -- TXN-XXXXX
  ticket_type     INTEGER               -- 1-4
  severity        TEXT                  -- low / medium / critical
  status          TEXT                  -- open / in_progress / resolved / escalated
  title           TEXT
  description     TEXT
  user_id     TEXT
  assigned_to     TEXT
  resolution      TEXT
  created_at      TIMESTAMPTZ
  updated_at      TIMESTAMPTZ
  resolved_at     TIMESTAMPTZ
"""

import os
from datetime import datetime, timezone
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from supabase import create_client, Client
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

TABLE = "tickets"

app = Server("ticketing-mcp")

def _get_supabase() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
    return create_client(url, key)


# ── Tool definitions ──────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name        = "create_ticket",
            description = "Create a new IT support ticket in the system.",
            inputSchema = {
                "type": "object",
                "properties": {
                    "ticket_id":   {"type": "string",  "description": "Unique ticket ID e.g. TXN-00001"},
                    "ticket_type": {"type": "integer", "description": "1=Access, 2=Asset, 3=Degradation, 4=Outage"},
                    "severity":    {"type": "string",  "description": "low | medium | critical"},
                    "title":       {"type": "string",  "description": "Short title for the ticket"},
                    "description": {"type": "string",  "description": "Full incident description"},
                    "user_id": {"type": "string",  "description": "Employee who raised the ticket"},
                },
                "required": ["ticket_id", "ticket_type", "severity", "title", "description"],
            },
        ),
        Tool(
            name        = "get_ticket",
            description = "Fetch a single ticket by its ID.",
            inputSchema = {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string", "description": "Ticket ID to fetch"},
                },
                "required": ["ticket_id"],
            },
        ),
        Tool(
            name        = "update_ticket",
            description = "Update ticket status, severity, or assigned engineer.",
            inputSchema = {
                "type": "object",
                "properties": {
                    "ticket_id":   {"type": "string", "description": "Ticket ID to update"},
                    "status":      {"type": "string", "description": "open | in_progress | resolved | escalated"},
                    "severity":    {"type": "string", "description": "low | medium | critical"},
                    "assigned_to": {"type": "string", "description": "Agent/engineer ID assigned"},
                },
                "required": ["ticket_id"],
            },
        ),
        Tool(
            name        = "list_tickets",
            description = "List tickets with optional filters by status, type, or severity.",
            inputSchema = {
                "type": "object",
                "properties": {
                    "status":      {"type": "string",  "description": "Filter by status"},
                    "ticket_type": {"type": "integer", "description": "Filter by type 1-4"},
                    "severity":    {"type": "string",  "description": "Filter by severity"},
                    "limit":       {"type": "integer", "description": "Max results (default 20)"},
                },
            },
        ),
        Tool(
            name        = "close_ticket",
            description = "Mark a ticket as resolved and record the resolution.",
            inputSchema = {
                "type": "object",
                "properties": {
                    "ticket_id":  {"type": "string", "description": "Ticket ID to close"},
                    "resolution": {"type": "string", "description": "Full resolution text"},
                    "runbook":    {"type": "string", "description": "Runbook filename used (if any)"},
                },
                "required": ["ticket_id", "resolution"],
            },
        ),
    ]


# ── Tool handlers ─────────────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    sb = _get_supabase()
    now = datetime.now(timezone.utc).isoformat()

    try:
        if name == "create_ticket":
            data = {
                "id":          arguments["ticket_id"],
                "ticket_type": arguments["ticket_type"],
                "severity":    arguments["severity"],
                "status":      "open",
                "title":       arguments["title"],
                "description": arguments["description"],
                "user_id": arguments.get("user_id", ""),
                "created_at":  now,
                "updated_at":  now,
            }
            result = sb.table(TABLE).insert(data).execute()
            return [TextContent(type="text", text=f"Ticket created: {arguments['ticket_id']}")]

        elif name == "get_ticket":
            result = sb.table(TABLE).select("*").eq("id", arguments["ticket_id"]).single().execute()
            if result.data:
                return [TextContent(type="text", text=str(result.data))]
            return [TextContent(type="text", text=f"Ticket {arguments['ticket_id']} not found.")]

        elif name == "update_ticket":
            updates = {"updated_at": now}
            for field in ("status", "severity", "assigned_to"):
                if field in arguments:
                    updates[field] = arguments[field]
            sb.table(TABLE).update(updates).eq("id", arguments["ticket_id"]).execute()
            return [TextContent(type="text", text=f"Ticket {arguments['ticket_id']} updated: {updates}")]

        elif name == "list_tickets":
            query = sb.table(TABLE).select("id, ticket_type, severity, status, title, created_at")
            if "status"      in arguments: query = query.eq("status",      arguments["status"])
            if "ticket_type" in arguments: query = query.eq("ticket_type", arguments["ticket_type"])
            if "severity"    in arguments: query = query.eq("severity",    arguments["severity"])
            limit = arguments.get("limit", 20)
            result = query.limit(limit).order("created_at", desc=True).execute()
            return [TextContent(type="text", text=str(result.data))]

        elif name == "close_ticket":
            updates = {
                "status":      "resolved",
                "resolution":  arguments["resolution"],
                "resolved_at": now,
                "updated_at":  now,
            }
            if "runbook" in arguments:
                updates["runbook_used"] = arguments["runbook"]
            sb.table(TABLE).update(updates).eq("id", arguments["ticket_id"]).execute()
            return [TextContent(type="text", text=f"Ticket {arguments['ticket_id']} closed.")]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as exc:
        logger.error("Ticketing MCP error [{}]: {}", name, exc)
        return [TextContent(type="text", text=f"Error: {exc}")]


# ── Client wrapper used by agents ─────────────────────────────────────────────

class TicketingMCPClient:
    """
    Thin client wrapper so l1_triage.py and l3_resolver.py
    can call MCP tools as regular async methods.
    """

    def __init__(self):
        self._sb = _get_supabase()
        self._now = lambda: datetime.now(timezone.utc).isoformat()

    async def create_ticket(self, ticket_id: str, ticket_type: int,
                             severity: str, title: str, description: str,
                             user_id: str = "") -> str:
        data = {
            "id": ticket_id, "ticket_type": ticket_type,
            "severity": severity, "status": "open",
            "title": title, "description": description,
            "user_id": user_id,
            "created_at": self._now(), "updated_at": self._now(),
        }
        self._sb.table(TABLE).insert(data).execute()
        return ticket_id

    async def update_ticket(self, ticket_id: str, **kwargs) -> None:
        kwargs["updated_at"] = self._now()
        self._sb.table(TABLE).update(kwargs).eq("id", ticket_id).execute()

    async def close_ticket(self, ticket_id: str, resolution: str, runbook: str = "") -> None:
        updates = {
            "status": "resolved", "resolution": resolution,
            "resolved_at": self._now(), "updated_at": self._now(),
        }
        if runbook:
            updates["runbook_used"] = runbook
        self._sb.table(TABLE).update(updates).eq("id", ticket_id).execute()


# ── Entrypoint ────────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())