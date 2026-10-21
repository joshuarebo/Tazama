"""Platform metadata the frontend needs before anything else loads.

Exposes only what a browser may know: mode, feature flags, roles, timezone. Never a
credential, a DSN or a webhook — the response is built from an allowlist, not a dump.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from tazama import __version__
from tazama.api.deps import SettingsDep
from tazama.core.security import PERMISSIONS, ROLES

router = APIRouter(tags=["meta"])


class FeatureFlags(BaseModel):
    classification: bool
    diagnosis_agent: bool
    knowledge_assistant: bool
    reporter: bool
    gnn: bool
    bandit: bool
    social_signals: bool


class Meta(BaseModel):
    version: str = __version__
    app_mode: str
    app_env: str
    display_tz: str
    demo_banner: bool = Field(description="Render the persistent DEMO DATA ribbon")
    ai_available: bool = Field(description="False means every AI panel shows AI degraded")
    vector_backend: str
    auth_mode: str
    teams_dry_run: bool
    map_tile_url: str
    roles: list[str]
    capabilities: dict[str, list[str]]
    features: FeatureFlags


@router.get("/meta", response_model=Meta, summary="Platform metadata and feature flags")
async def meta(settings: SettingsDep) -> Meta:
    return Meta(
        app_mode=settings.APP_MODE,
        app_env=settings.APP_ENV,
        display_tz=settings.DISPLAY_TZ,
        demo_banner=settings.is_demo,
        ai_available=settings.llm_configured,
        vector_backend=settings.VECTOR_BACKEND,
        auth_mode=settings.AUTH_MODE,
        teams_dry_run=settings.TEAMS_DRY_RUN,
        map_tile_url=settings.NEXT_PUBLIC_MAP_TILE_URL,
        roles=list(ROLES),
        capabilities={cap: sorted(roles) for cap, roles in sorted(PERMISSIONS.items())},
        features=FeatureFlags(
            classification=settings.AI_CLASSIFICATION_ENABLED,
            diagnosis_agent=settings.AI_DIAGNOSIS_AGENT_ENABLED,
            knowledge_assistant=settings.AI_KNOWLEDGE_ASSISTANT_ENABLED,
            reporter=settings.AI_REPORTER_ENABLED,
            gnn=settings.AI_GNN_ENABLED,
            bandit=settings.AI_BANDIT_ENABLED,
            social_signals=settings.SOCIAL_SIGNALS_ENABLED,
        ),
    )
