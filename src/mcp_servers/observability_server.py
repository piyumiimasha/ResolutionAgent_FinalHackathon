"""
observability_server.py — Observability MCP Server.

Wraps the `server_metrics` and `services` Supabase tables.
Exposes 5 tools for L2 investigator to query live infrastructure state.

Tools:
  1. get_server_metrics   — CPU, memory, disk for a specific server
  2. get_service_status   — health status of a named service
  3. list_unhealthy       — all servers/services currently in bad state
  4. get_incident_history — past incidents for a server or service
  5. query_metrics_range  — metrics over a time window (spike detection)

Supabase tables used:
  server_metrics:
    server_id TEXT, cpu_percent FLOAT, memory_percent FLOAT,
    disk_percent FLOAT, network_in_mbps FLOAT, network_out_mbps FLOAT,
    status TEXT, region TEXT, recorded_at TIMESTAMPTZ

  services:
    service_id TEXT, service_name TEXT, server_id TEXT,
    status TEXT (healthy/degraded/down), error_rate_percent FLOAT,
    p99_latency_ms INTEGER, last_checked TIMESTAMPTZ

  incident_history:
    id SERIAL, server_id TEXT, service_id TEXT, ticket_type INTEGER,
    description TEXT, root_cause TEXT, resolution TEXT,
    occurred_at TIMESTAMPTZ, resolved_at TIMESTAMPTZ
"""

import os
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from supabase import create_client, Client
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

app = Server("observability-mcp")

def _get_supabase() -> Client:
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


# ── Tool definitions ──────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name        = "get_server_metrics",
            description = "Get the latest CPU, memory, and disk metrics for a specific server.",
            inputSchema = {
                "type": "object",
                "properties": {
                    "server_id": {"type": "string", "description": "Server ID e.g. prod-cache-01"},
                },
                "required": ["server_id"],
            },
        ),
        Tool(
            name        = "get_service_status",
            description = "Get the current health status, error rate, and latency for a service.",
            inputSchema = {
                "type": "object",
                "properties": {
                    "service_name": {"type": "string", "description": "Service name e.g. auth-service, checkout-api"},
                },
                "required": ["service_name"],
            },
        ),
        Tool(
            name        = "list_unhealthy",
            description = "List all servers or services currently in degraded or down state.",
            inputSchema = {
                "type": "object",
                "properties": {
                    "resource_type": {
                        "type": "string",
                        "description": "servers | services | both",
                        "enum": ["servers", "services", "both"],
                    },
                },
            },
        ),
        Tool(
            name        = "get_incident_history",
            description = "Retrieve past incident records for a server or service.",
            inputSchema = {
                "type": "object",
                "properties": {
                    "server_id":    {"type": "string",  "description": "Filter by server ID"},
                    "service_id":   {"type": "string",  "description": "Filter by service ID"},
                    "ticket_type":  {"type": "integer", "description": "Filter by ticket type 1-4"},
                    "limit":        {"type": "integer", "description": "Max results (default 5)"},
                },
            },
        ),
        Tool(
            name        = "query_metrics_range",
            description = (
                "Query server metrics over a time window to detect spikes. "
                "Returns min, max, and average for CPU, memory, and disk."
            ),
            inputSchema = {
                "type": "object",
                "properties": {
                    "server_id":   {"type": "string", "description": "Server ID to query"},
                    "hours_back":  {"type": "integer", "description": "How many hours back to look (default 1)"},
                },
                "required": ["server_id"],
            },
        ),
    ]


# ── Tool handlers ─────────────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    sb = _get_supabase()

    try:
        if name == "get_server_metrics":
            result = (
                sb.table("server_metrics")
                .select("server_id, cpu_percent, memory_percent, disk_percent, network_in_mbps, network_out_mbps, status, region, recorded_at")
                .eq("server_id", arguments["server_id"])
                .order("recorded_at", desc=True)
                .limit(1)
                .execute()
            )
            if not result.data:
                return [TextContent(type="text", text=f"No metrics found for server: {arguments['server_id']}")]

            m = result.data[0]
            summary = (
                f"Server: {m['server_id']} | Region: {m.get('region', 'N/A')} | Status: {m['status']}\n"
                f"CPU: {m['cpu_percent']}% | Memory: {m['memory_percent']}% | "
                f"Disk: {m['disk_percent']}% | "
                f"Net in: {m.get('network_in_mbps', 0)} Mbps | Net out: {m.get('network_out_mbps', 0)} Mbps\n"
                f"Recorded at: {m['recorded_at']}"
            )
            return [TextContent(type="text", text=summary)]

        elif name == "get_service_status":
            result = (
                sb.table("services")
                .select("service_id, service_name, server_id, status, error_rate_percent, p99_latency_ms, last_checked")
                .eq("service_name", arguments["service_name"])
                .order("last_checked", desc=True)
                .limit(1)
                .execute()
            )
            if not result.data:
                return [TextContent(type="text", text=f"No service found: {arguments['service_name']}")]

            s = result.data[0]
            summary = (
                f"Service: {s['service_name']} (ID: {s['service_id']}) on {s['server_id']}\n"
                f"Status: {s['status']} | Error rate: {s['error_rate_percent']}% | "
                f"P99 latency: {s['p99_latency_ms']}ms\n"
                f"Last checked: {s['last_checked']}"
            )
            return [TextContent(type="text", text=summary)]

        elif name == "list_unhealthy":
            resource_type = arguments.get("resource_type", "both")
            output_parts  = []

            if resource_type in ("servers", "both"):
                servers = (
                    sb.table("server_metrics")
                    .select("server_id, cpu_percent, memory_percent, disk_percent, status, recorded_at")
                    .in_("status", ["degraded", "down", "critical"])
                    .order("recorded_at", desc=True)
                    .limit(10)
                    .execute()
                )
                if servers.data:
                    output_parts.append("UNHEALTHY SERVERS:")
                    for s in servers.data:
                        output_parts.append(
                            f"  {s['server_id']} | {s['status']} | "
                            f"CPU: {s['cpu_percent']}% | Mem: {s['memory_percent']}%"
                        )
                else:
                    output_parts.append("SERVERS: All healthy.")

            if resource_type in ("services", "both"):
                services = (
                    sb.table("services")
                    .select("service_name, server_id, status, error_rate_percent, p99_latency_ms")
                    .in_("status", ["degraded", "down"])
                    .execute()
                )
                if services.data:
                    output_parts.append("UNHEALTHY SERVICES:")
                    for s in services.data:
                        output_parts.append(
                            f"  {s['service_name']} on {s['server_id']} | {s['status']} | "
                            f"Error rate: {s['error_rate_percent']}% | P99: {s['p99_latency_ms']}ms"
                        )
                else:
                    output_parts.append("SERVICES: All healthy.")

            return [TextContent(type="text", text="\n".join(output_parts))]

        elif name == "get_incident_history":
            query = (
                sb.table("incident_history")
                .select("id, server_id, service_id, ticket_type, description, root_cause, resolution, occurred_at, resolved_at")
                .order("occurred_at", desc=True)
            )
            if "server_id"   in arguments: query = query.eq("server_id",   arguments["server_id"])
            if "service_id"  in arguments: query = query.eq("service_id",  arguments["service_id"])
            if "ticket_type" in arguments: query = query.eq("ticket_type", arguments["ticket_type"])
            result = query.limit(arguments.get("limit", 5)).execute()

            if not result.data:
                return [TextContent(type="text", text="No incident history found for given filters.")]

            lines = ["INCIDENT HISTORY:"]
            for inc in result.data:
                lines.append(
                    f"\n[{inc['occurred_at']}] Type {inc['ticket_type']}\n"
                    f"  Description : {inc['description']}\n"
                    f"  Root Cause  : {inc['root_cause']}\n"
                    f"  Resolution  : {inc['resolution']}\n"
                    f"  Resolved at : {inc.get('resolved_at', 'N/A')}"
                )
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "query_metrics_range":
            from datetime import datetime, timezone, timedelta
            hours_back = arguments.get("hours_back", 1)
            since = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()

            result = (
                sb.table("server_metrics")
                .select("cpu_percent, memory_percent, disk_percent, recorded_at")
                .eq("server_id", arguments["server_id"])
                .gte("recorded_at", since)
                .execute()
            )
            if not result.data:
                return [TextContent(type="text", text=f"No metrics found for {arguments['server_id']} in last {hours_back}h")]

            cpus = [r["cpu_percent"] for r in result.data]
            mems = [r["memory_percent"] for r in result.data]
            disks = [r["disk_percent"] for r in result.data]

            summary = (
                f"Metrics for {arguments['server_id']} over last {hours_back}h ({len(result.data)} samples):\n"
                f"CPU    — min: {min(cpus):.1f}%  max: {max(cpus):.1f}%  avg: {sum(cpus)/len(cpus):.1f}%\n"
                f"Memory — min: {min(mems):.1f}%  max: {max(mems):.1f}%  avg: {sum(mems)/len(mems):.1f}%\n"
                f"Disk   — min: {min(disks):.1f}%  max: {max(disks):.1f}%  avg: {sum(disks)/len(disks):.1f}%"
            )
            return [TextContent(type="text", text=summary)]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as exc:
        logger.error("Observability MCP error [{}]: {}", name, exc)
        return [TextContent(type="text", text=f"Error: {exc}")]


# ── Client wrapper used by l2_investigator ────────────────────────────────────

class ObservabilityMCPClient:
    """Thin client so l2_investigator can call tools as async methods."""

    def __init__(self):
        self._sb = _get_supabase()

    async def get_metrics(self, ticket: str, ticket_type: int) -> dict:
        """
        Smart dispatcher — extracts server/service name from ticket text
        and queries the right observability tool.
        """
        import re

        # Try to extract a server name pattern e.g. prod-cache-01, prod-api-02
        server_match = re.search(r"(prod|dev|staging)-[\w]+-\d+", ticket.lower())
        service_keywords = {
            "auth":     "auth-service",
            "checkout": "checkout-api",
            "redis":    "redis",
            "api":      "api-gateway",
            "database": "postgres",
            "db":       "postgres",
        }

        results = {}

        if server_match:
            server_id = server_match.group(0)
            metrics_result = (
                self._sb.table("server_metrics")
                .select("*")
                .eq("server_id", server_id)
                .order("recorded_at", desc=True)
                .limit(1)
                .execute()
            )
            results["server_metrics"] = metrics_result.data[0] if metrics_result.data else {}

        for keyword, service_name in service_keywords.items():
            if keyword in ticket.lower():
                svc_result = (
                    self._sb.table("services")
                    .select("*")
                    .eq("service_name", service_name)
                    .order("last_checked", desc=True)
                    .limit(1)
                    .execute()
                )
                results["service_status"] = svc_result.data[0] if svc_result.data else {}
                break

        # Always include unhealthy services for context
        unhealthy = (
            self._sb.table("services")
            .select("service_name, status, error_rate_percent")
            .in_("status", ["degraded", "down"])
            .execute()
        )
        results["unhealthy_services"] = unhealthy.data or []

        return results


# ── Entrypoint ────────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())