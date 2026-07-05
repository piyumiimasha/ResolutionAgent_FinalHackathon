"""
api/routers/health.py — Health check endpoint.

GET /health — returns status of all backing services.
Used by AWS ECS health checks and load balancer.
"""

from fastapi import APIRouter
from loguru import logger

from api.schemas import HealthResponse, ServiceStatus
from src.infastructure.db.supabase_client import check_connection as supabase_health
from src.infastructure.db.qdrant_client import check_connection as qdrant_health
from src.services.azure.blob_service_client import check_connection as azure_health

router = APIRouter()


@router.get("", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """
    Health check endpoint. Returns status of all backing services.
    Returns HTTP 200 even if some services are degraded —
    only returns 503 if all services are down.
    """
    services = []

    # Supabase
    try:
        sb_ok = await supabase_health()
        services.append(ServiceStatus(
            name   = "supabase",
            status = "ok" if sb_ok else "down",
            detail = "tickets, memory, episodes" if sb_ok else "connection failed",
        ))
    except Exception as exc:
        services.append(ServiceStatus(name="supabase", status="down", detail=str(exc)))

    # Qdrant
    try:
        q_ok = await qdrant_health()
        services.append(ServiceStatus(
            name   = "qdrant",
            status = "ok" if q_ok else "down",
            detail = "procedural memory, CAG, runbooks" if q_ok else "connection failed",
        ))
    except Exception as exc:
        services.append(ServiceStatus(name="qdrant", status="down", detail=str(exc)))

    # Azure Blob
    try:
        az_ok = await azure_health()
        services.append(ServiceStatus(
            name   = "azure_blob",
            status = "ok" if az_ok else "degraded",
            detail = "transcript storage" if az_ok else "connection failed (non-critical)",
        ))
    except Exception as exc:
        services.append(ServiceStatus(name="azure_blob", status="degraded", detail=str(exc)))

    # Overall status
    critical_services = [s for s in services if s.name in ("supabase", "qdrant")]
    all_critical_down = all(s.status == "down" for s in critical_services)
    any_degraded      = any(s.status in ("down", "degraded") for s in services)

    if all_critical_down:
        overall = "unhealthy"
    elif any_degraded:
        overall = "degraded"
    else:
        overall = "healthy"

    return HealthResponse(status=overall, services=services)