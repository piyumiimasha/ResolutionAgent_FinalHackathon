"""
api/schemas.py — Pydantic request/response models for all API endpoints.
"""

from typing import Optional, Any
from pydantic import BaseModel, Field
from datetime import datetime


# ── Chat ──────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    """Incoming chat/ticket request."""
    message:     str            = Field(..., description="Incident description or IT support request")
    user_id:     Optional[str]  = Field(None, description="Employee ID e.g. EMP-0150")
    ticket_id:   Optional[str]  = Field(None, description="Existing ticket ID to continue (optional)")

    model_config = {
        "json_schema_extra": {
            "example": {
                "message": "Redis is running out of memory on prod-cache-01",
                "user_id": "EMP-0150",
            }
        }
    }


class ChatResponse(BaseModel):
    """Response returned after processing a ticket through the swarm."""
    ticket_id:               str
    resolution:              Optional[str]  = None
    ticket_type:             Optional[int]  = None
    severity:                Optional[str]  = None
    runbook_used:            Optional[str]  = None
    action_taken:            Optional[str]  = None
    cag_hit:                 bool           = False
    is_safe:                 bool           = True
    guardrail_reason:        Optional[str]  = None
    error:                   Optional[str]  = None
    started_at:              Optional[str]  = None
    resolved_at:             Optional[str]  = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "ticket_id":    "550e8400-e29b-41d4-a716-446655440000",
                "resolution":   "Restarted Redis on prod-cache-01 with maxmemory set to 2gb.",
                "ticket_type":  4,
                "severity":     "critical",
                "runbook_used": "redis_oom.md",
                "action_taken": "[MOCK] Restarted redis on prod-cache-01.",
                "cag_hit":      False,
                "is_safe":      True,
            }
        }
    }


class StreamChunk(BaseModel):
    """Single chunk in a streaming response."""
    event:   str            # "start" | "update" | "done" | "error"
    data:    Optional[str]  = None
    state:   Optional[dict] = None


# ── Tickets ───────────────────────────────────────────────────────────────────

class TicketListItem(BaseModel):
    """Summary row for ticket list endpoint."""
    id:          str
    ticket_type: int
    severity:    str
    status:      str
    title:       Optional[str] = None
    user_id: Optional[str] = None
    created_at:  Optional[str] = None
    resolved_at: Optional[str] = None


class TicketDetail(TicketListItem):
    """Full ticket detail including resolution."""
    description:  Optional[str] = None
    assigned_to:  Optional[str] = None
    resolution:   Optional[str] = None
    runbook_used: Optional[str] = None
    updated_at:   Optional[str] = None


class TicketListResponse(BaseModel):
    """Paginated ticket list."""
    tickets: list[TicketListItem]
    total:   int
    limit:   int
    offset:  int


# ── Health ────────────────────────────────────────────────────────────────────

class ServiceStatus(BaseModel):
    """Status of a single backing service."""
    name:    str
    status:  str    # "ok" | "degraded" | "down"
    detail:  Optional[str] = None


class HealthResponse(BaseModel):
    """Overall health check response."""
    status:   str                   # "healthy" | "degraded" | "unhealthy"
    services: list[ServiceStatus]
    version:  str = "1.0.0"
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())