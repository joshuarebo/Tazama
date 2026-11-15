"""Liveness and readiness.

`/healthz` answers "is the process up" and never touches a dependency.
`/readyz` answers "can it serve traffic" and probes DB, LLM, vector backend and outbox
lag. Readiness degrades to 503 so an orchestrator stops sending traffic, while the body
always lists which check failed — that is what the runbooks key off.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from tazama import __version__
from tazama.api.deps import ClockDep, SettingsDep

router = APIRouter(tags=["health"])

CheckStatus = Literal["ok", "degraded", "down", "skipped"]


class Check(BaseModel):
    name: str
    status: CheckStatus
    detail: str = ""
    duration_ms: float = 0.0


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    version: str = __version__
    app_mode: str
    time: str


class Readiness(BaseModel):
    status: Literal["ready", "degraded", "not-ready"]
    version: str = __version__
    checks: list[Check] = Field(default_factory=list)


@router.get("/healthz", response_model=Health, summary="Liveness")
async def healthz(settings: SettingsDep, clock: ClockDep) -> Health:
    return Health(app_mode=settings.APP_MODE, time=clock.now().isoformat())


@router.get("/readyz", response_model=Readiness, summary="Readiness")
async def readyz(settings: SettingsDep, response: Response) -> Readiness:
    from tazama.api.readiness import run_checks  # local import keeps startup cheap

    checks = await run_checks(settings)
    if any(c.status == "down" for c in checks):
        status: Literal["ready", "degraded", "not-ready"] = "not-ready"
        response.status_code = 503
    elif any(c.status == "degraded" for c in checks):
        status = "degraded"
    else:
        status = "ready"
    return Readiness(status=status, checks=checks)


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    include_in_schema=False,
    response_class=Response,
)
async def metrics(settings: SettingsDep) -> Response:
    """The scrape target for Prometheus, and therefore for the Grafana dashboards.

    Unauthenticated on purpose, and safe to be: the payload is counters and latency
    histograms labelled by route *template*, with no identifier, no query text and no
    business value in it. The NetworkPolicy in deploy/openshift restricts who can reach
    the port at all, which is the right place for that control. It is excluded from the
    OpenAPI document because it is an operational endpoint, not part of the API contract.
    """
    if not settings.METRICS_ENABLED:
        return Response(status_code=404)
    from tazama.api.metrics import METRICS

    return Response(
        content=METRICS.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


def as_dict(readiness: Readiness) -> dict[str, Any]:
    """Used by scripts/doctor.py so the CLI and the endpoint cannot drift apart."""
    return readiness.model_dump()
