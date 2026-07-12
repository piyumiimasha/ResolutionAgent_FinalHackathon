"""
api/routers/tickets.py — Ticket management endpoints.

GET  /tickets           — list tickets with optional filters
GET  /tickets/{id}      — get a single ticket by ID
GET  /tickets/stats     — ticket counts grouped by type/severity/status
"""

from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from api.schemas import TicketDetail, TicketListItem, TicketListResponse
from src.infrastructure.db.supabase_client import get_supabase

router = APIRouter()


@router.get("", response_model=TicketListResponse)
async def list_tickets(
    status:      str | None = Query(None, description="Filter by status: open | in_progress | resolved | escalated"),
    ticket_type: int | None = Query(None, description="Filter by type: 1=Access 2=Asset 3=Degradation 4=Outage"),
    severity:    str | None = Query(None, description="Filter by severity: low | medium | critical"),
    limit:       int        = Query(20,   ge=1, le=100),
    offset:      int        = Query(0,    ge=0),
) -> TicketListResponse:
    """List tickets with optional filters. Most recent first."""
    try:
        sb    = get_supabase()
        query = sb.table("tickets").select(
            "id, ticket_type, severity, status, title, user_id, created_at, resolved_at",
            count="exact",
        )

        if status:      query = query.eq("status",      status)
        if ticket_type: query = query.eq("ticket_type", ticket_type)
        if severity:    query = query.eq("severity",    severity)

        result = (
            query
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )

        tickets = [TicketListItem(**row) for row in (result.data or [])]
        return TicketListResponse(
            tickets = tickets,
            total   = result.count or 0,
            limit   = limit,
            offset  = offset,
        )

    except Exception as exc:
        logger.error("GET /tickets failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/stats")
async def ticket_stats() -> dict:
    """
    Return ticket counts grouped by type, severity, and status.
    Useful for dashboard/monitoring.
    """
    try:
        sb     = get_supabase()
        result = sb.table("tickets").select("ticket_type, severity, status").execute()
        rows   = result.data or []

        by_type     = {}
        by_severity = {}
        by_status   = {}

        type_labels = {1: "access_identity", 2: "asset_provisioning",
                       3: "service_degradation", 4: "critical_outage"}

        for row in rows:
            t = type_labels.get(row.get("ticket_type"), "unknown")
            s = row.get("severity", "unknown")
            st = row.get("status", "unknown")
            by_type[t]       = by_type.get(t, 0) + 1
            by_severity[s]   = by_severity.get(s, 0) + 1
            by_status[st]    = by_status.get(st, 0) + 1

        return {
            "total":       len(rows),
            "by_type":     by_type,
            "by_severity": by_severity,
            "by_status":   by_status,
        }

    except Exception as exc:
        logger.error("GET /tickets/stats failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/{ticket_id}", response_model=TicketDetail)
async def get_ticket(ticket_id: str) -> TicketDetail:
    """Fetch a single ticket by ID including full resolution text."""
    try:
        sb     = get_supabase()
        result = (
            sb.table("tickets")
            .select("*")
            .eq("id", ticket_id)
            .single()
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
        return TicketDetail(**result.data)

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("GET /tickets/{} failed: {}", ticket_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))