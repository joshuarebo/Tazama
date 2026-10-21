"""API v1. Routers are mounted here and nowhere else."""

from __future__ import annotations

from fastapi import APIRouter

from tazama.api.v1.routers import auth, health, meta

router = APIRouter()
router.include_router(health.router)
router.include_router(meta.router, prefix="/api/v1")
router.include_router(auth.router, prefix="/api/v1")

__all__ = ["router"]
