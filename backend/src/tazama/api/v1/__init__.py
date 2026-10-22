"""API v1. Routers are mounted here and nowhere else."""

from __future__ import annotations

from fastapi import APIRouter

from tazama.api.v1.routers import (
    audit,
    auth,
    complaints,
    discovery,
    health,
    meta,
    notifications,
    recovery,
    stream,
)

router = APIRouter()
router.include_router(health.router)
router.include_router(meta.router, prefix="/api/v1")
router.include_router(auth.router, prefix="/api/v1")
router.include_router(discovery.router, prefix="/api/v1")
router.include_router(stream.router, prefix="/api/v1")
router.include_router(complaints.router, prefix="/api/v1")
router.include_router(notifications.router, prefix="/api/v1")
router.include_router(recovery.router, prefix="/api/v1")
router.include_router(audit.router, prefix="/api/v1")

__all__ = ["router"]
