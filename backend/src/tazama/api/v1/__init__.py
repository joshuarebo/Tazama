"""API v1. Routers are mounted here and nowhere else."""

from __future__ import annotations

from fastapi import APIRouter

from tazama.api.v1.routers import (
    audit,
    auth,
    care,
    complaints,
    discovery,
    health,
    incidents,
    insights,
    meta,
    notifications,
    recovery,
    reports,
    stream,
)

router = APIRouter()
router.include_router(health.router)
router.include_router(meta.router, prefix="/api/v1")
router.include_router(auth.router, prefix="/api/v1")
router.include_router(discovery.router, prefix="/api/v1")
router.include_router(stream.router, prefix="/api/v1")
router.include_router(complaints.router, prefix="/api/v1")
router.include_router(care.router, prefix="/api/v1")
router.include_router(incidents.router, prefix="/api/v1")
router.include_router(notifications.router, prefix="/api/v1")
router.include_router(recovery.router, prefix="/api/v1")
router.include_router(audit.router, prefix="/api/v1")
router.include_router(insights.router, prefix="/api/v1")
router.include_router(reports.router, prefix="/api/v1")

__all__ = ["router"]
