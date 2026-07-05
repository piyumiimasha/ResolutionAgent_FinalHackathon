"""
action_server.py — Action MCP Server.

Exposes 5 tools for L3 resolver to execute remediation actions.
All actions are MOCK implementations — they simulate execution and
log to the incident_history table, but don't touch real infrastructure.

Tools:
  1. restart_service     — mock restart a service on a server
  2. scale_service       — mock scale up replicas / increase resource limits
  3. flush_cache         — mock flush Redis / memcached
  4. rotate_credentials  — mock rotate API keys / certificates
  5. run_runbook_step    — execute a specific runbook step (logged only)

Called by: l3_resolver.py
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

app = Server("action-mcp")

def _get_supabase() -> Client:
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def _log_action(sb: Client, ticket_id: str, action: str, target: str, result: str) -> None:
    """Log every action execution to incident_history for audit trail."""
    try:
        sb.table("incident_history").insert({
            "ticket_id":    ticket_id,
            "action":       action,
            "target":       target,
            "result":       result,
            "executed_at":  datetime.now(timezone.utc).isoformat(),
            "is_mock":      True,
        }).execute()
    except Exception as exc:
        logger.warning("Failed to log action to incident_history: {}", exc)


# ── Tool definitions ──────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name        = "restart_service",
            description = "Mock restart a service on a target server. Logs the action and simulates a 30s restart window.",
            inputSchema = {
                "type": "object",
                "properties": {
                    "service_name": {"type": "string", "description": "Service to restart e.g. redis, auth-service"},
                    "server_id":    {"type": "string", "description": "Target server e.g. prod-cache-01"},
                    "ticket_id":    {"type": "string", "description": "Ticket ID for audit trail"},
                    "reason":       {"type": "string", "description": "Reason for restart"},
                },
                "required": ["service_name", "server_id", "ticket_id"],
            },
        ),
        Tool(
            name        = "scale_service",
            description = "Mock scale a service up (more replicas or higher resource limits).",
            inputSchema = {
                "type": "object",
                "properties": {
                    "service_name":  {"type": "string",  "description": "Service to scale"},
                    "server_id":     {"type": "string",  "description": "Target server"},
                    "ticket_id":     {"type": "string",  "description": "Ticket ID for audit trail"},
                    "replicas":      {"type": "integer", "description": "New replica count"},
                    "memory_limit":  {"type": "string",  "description": "New memory limit e.g. 2gb"},
                    "cpu_limit":     {"type": "string",  "description": "New CPU limit e.g. 2.0"},
                },
                "required": ["service_name", "server_id", "ticket_id"],
            },
        ),
        Tool(
            name        = "flush_cache",
            description = "Mock flush a Redis or memcached instance to free memory.",
            inputSchema = {
                "type": "object",
                "properties": {
                    "server_id":   {"type": "string", "description": "Cache server to flush"},
                    "cache_type":  {"type": "string", "description": "redis | memcached"},
                    "ticket_id":   {"type": "string", "description": "Ticket ID for audit trail"},
                    "selective":   {"type": "boolean", "description": "True = flush only expired keys, False = full flush"},
                },
                "required": ["server_id", "ticket_id"],
            },
        ),
        Tool(
            name        = "rotate_credentials",
            description = "Mock rotate credentials — API keys, MFA tokens, or certificates.",
            inputSchema = {
                "type": "object",
                "properties": {
                    "credential_type": {"type": "string",  "description": "mfa_token | api_key | certificate | password"},
                    "target_id":       {"type": "string",  "description": "Employee ID or service ID"},
                    "ticket_id":       {"type": "string",  "description": "Ticket ID for audit trail"},
                    "notify_user":     {"type": "boolean", "description": "Send notification to user after rotation"},
                },
                "required": ["credential_type", "target_id", "ticket_id"],
            },
        ),
        Tool(
            name        = "run_runbook_step",
            description = "Execute a specific step from a runbook. Logs the step and marks it complete.",
            inputSchema = {
                "type": "object",
                "properties": {
                    "runbook_name": {"type": "string", "description": "Runbook file e.g. redis_oom.md"},
                    "step_number":  {"type": "integer","description": "Step number to execute"},
                    "step_command": {"type": "string", "description": "The exact command or action in that step"},
                    "ticket_id":    {"type": "string", "description": "Ticket ID for audit trail"},
                },
                "required": ["runbook_name", "step_number", "step_command", "ticket_id"],
            },
        ),
    ]


# ── Tool handlers ─────────────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    sb        = _get_supabase()
    ticket_id = arguments.get("ticket_id", "UNKNOWN")

    try:
        if name == "restart_service":
            service = arguments["service_name"]
            server  = arguments["server_id"]
            reason  = arguments.get("reason", "L3 automated remediation")

            result_msg = (
                f"[MOCK] Restarted {service} on {server}.\n"
                f"Reason: {reason}\n"
                f"Simulated restart window: 30s\n"
                f"Expected: Service healthy after restart."
            )
            _log_action(sb, ticket_id, "restart_service", f"{service}@{server}", result_msg)
            logger.info("ACTION: restart_service | {} on {} | ticket: {}", service, server, ticket_id)
            return [TextContent(type="text", text=result_msg)]

        elif name == "scale_service":
            service = arguments["service_name"]
            server  = arguments["server_id"]
            changes = []
            if "replicas"     in arguments: changes.append(f"replicas → {arguments['replicas']}")
            if "memory_limit" in arguments: changes.append(f"memory   → {arguments['memory_limit']}")
            if "cpu_limit"    in arguments: changes.append(f"cpu      → {arguments['cpu_limit']}")

            result_msg = (
                f"[MOCK] Scaled {service} on {server}.\n"
                f"Changes: {', '.join(changes) or 'default scale-up'}\n"
                f"Expected: Reduced resource pressure within 60s."
            )
            _log_action(sb, ticket_id, "scale_service", f"{service}@{server}", result_msg)
            logger.info("ACTION: scale_service | {} on {} | ticket: {}", service, server, ticket_id)
            return [TextContent(type="text", text=result_msg)]

        elif name == "flush_cache":
            server     = arguments["server_id"]
            cache_type = arguments.get("cache_type", "redis")
            selective  = arguments.get("selective", True)
            mode       = "selective (expired keys only)" if selective else "full flush"

            result_msg = (
                f"[MOCK] Flushed {cache_type} on {server} ({mode}).\n"
                f"Expected: Memory freed immediately. "
                f"{'Minimal data loss (expired keys only).' if selective else 'WARNING: All cache data cleared.'}"
            )
            _log_action(sb, ticket_id, "flush_cache", f"{cache_type}@{server}", result_msg)
            logger.info("ACTION: flush_cache | {} on {} | ticket: {}", cache_type, server, ticket_id)
            return [TextContent(type="text", text=result_msg)]

        elif name == "rotate_credentials":
            cred_type   = arguments["credential_type"]
            target      = arguments["target_id"]
            notify      = arguments.get("notify_user", True)

            notification_msg = f"Notification sent to {target}." if notify else "No notification sent."
            result_msg = (
                f"[MOCK] Rotated {cred_type} for {target}.\n"
                f"{notification_msg}\n"
                f"Old credentials invalidated. New credentials issued.\n"
                f"Expected: User can authenticate with new credentials immediately."
            )
            _log_action(sb, ticket_id, "rotate_credentials", f"{cred_type}:{target}", result_msg)
            logger.info("ACTION: rotate_credentials | {} for {} | ticket: {}", cred_type, target, ticket_id)
            return [TextContent(type="text", text=result_msg)]

        elif name == "run_runbook_step":
            runbook = arguments["runbook_name"]
            step    = arguments["step_number"]
            command = arguments["step_command"]

            result_msg = (
                f"[MOCK] Executed runbook step.\n"
                f"Runbook : {runbook}\n"
                f"Step    : {step}\n"
                f"Command : {command}\n"
                f"Status  : Completed successfully (mock)."
            )
            _log_action(sb, ticket_id, "run_runbook_step", f"{runbook}:step{step}", result_msg)
            logger.info("ACTION: run_runbook_step | {}:step{} | ticket: {}", runbook, step, ticket_id)
            return [TextContent(type="text", text=result_msg)]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as exc:
        logger.error("Action MCP error [{}]: {}", name, exc)
        return [TextContent(type="text", text=f"Error executing {name}: {exc}")]


# ── Client wrapper used by l3_resolver ───────────────────────────────────────

class ActionMCPClient:
    """
    Thin client so l3_resolver.py can call tools as async methods.
    Parses the L3 resolution plan text and dispatches the right tool.
    """

    def __init__(self):
        self._sb = _get_supabase()

    async def execute(self, action: str, ticket_type: int, severity: str) -> str:
        """
        Parse the action string from L3 and call the right mock action.

        L3 produces lines like:
          "restart_service: redis on prod-cache-01"
          "flush_cache: redis on prod-cache-01"
          "rotate_credentials: mfa_token for EMP-0150"
          "scale_service: auth-service on prod-api-01 replicas=3"

        Args:
            action:      Action string from L3 resolver output.
            ticket_type: Ticket type for context.
            severity:    Severity for logging.

        Returns:
            Result string describing what was executed.
        """
        action_lower = action.lower()

        try:
            if "restart" in action_lower:
                return await self._mock_restart(action)
            elif "flush" in action_lower or "cache" in action_lower:
                return await self._mock_flush(action)
            elif "rotate" in action_lower or "credential" in action_lower or "mfa" in action_lower:
                return await self._mock_rotate(action)
            elif "scale" in action_lower:
                return await self._mock_scale(action)
            else:
                # Generic runbook step
                return f"[MOCK] Executed: {action}"
        except Exception as exc:
            logger.error("ActionMCPClient.execute failed: {}", exc)
            return f"Action execution failed: {exc}. Manual intervention required."

    async def _mock_restart(self, action: str) -> str:
        import re
        server_match  = re.search(r"(prod|dev|staging)-[\w]+-\d+", action)
        service_match = re.search(r"restart[_\s]+([\w-]+)", action.lower())
        server  = server_match.group(0) if server_match else "unknown-server"
        service = service_match.group(1) if service_match else "service"
        return f"[MOCK] Restarted {service} on {server}. Expected: healthy in 30s."

    async def _mock_flush(self, action: str) -> str:
        import re
        server_match = re.search(r"(prod|dev|staging)-[\w]+-\d+", action)
        server = server_match.group(0) if server_match else "unknown-server"
        return f"[MOCK] Flushed cache on {server}. Memory freed."

    async def _mock_rotate(self, action: str) -> str:
        import re
        emp_match = re.search(r"EMP-\d+", action.upper())
        target = emp_match.group(0) if emp_match else "target"
        return f"[MOCK] Credentials rotated for {target}. Notification sent."

    async def _mock_scale(self, action: str) -> str:
        import re
        server_match = re.search(r"(prod|dev|staging)-[\w]+-\d+", action)
        server = server_match.group(0) if server_match else "unknown-server"
        return f"[MOCK] Scaled service on {server}. Resources increased."


# ── Entrypoint ────────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())