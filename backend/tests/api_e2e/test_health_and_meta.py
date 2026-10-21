"""Health, readiness and metadata — the endpoints the frontend and the runbooks rely on."""

from __future__ import annotations

import pytest


def test_healthz_never_touches_a_dependency(client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app_mode"] == "demo"
    assert body["time"].endswith("+00:00")  # stored/reported in UTC


def test_readyz_lists_every_probe(client) -> None:
    response = client.get("/readyz")
    # Nothing is "down" in Phase 0: unimplemented subsystems report "skipped".
    assert response.status_code == 200
    body = response.json()
    names = {check["name"] for check in body["checks"]}
    assert names == {"config", "database", "llm", "vector", "outbox"}
    assert body["status"] in {"ready", "degraded"}
    config_check = next(c for c in body["checks"] if c["name"] == "config")
    assert config_check["status"] == "ok"


def test_readyz_reports_degraded_without_an_llm(client) -> None:
    body = client.get("/readyz").json()
    llm = next(c for c in body["checks"] if c["name"] == "llm")
    assert llm["status"] == "degraded"
    assert "deterministic fallbacks" in llm["detail"]


def test_meta_exposes_flags_but_no_secrets(client) -> None:
    response = client.get("/api/v1/meta")
    assert response.status_code == 200
    body = response.json()
    assert body["app_mode"] == "demo"
    assert body["demo_banner"] is True
    assert body["ai_available"] is False
    assert body["display_tz"] == "Africa/Nairobi"
    assert body["teams_dry_run"] is True
    assert set(body["roles"]) == {"care_agent", "noc_engineer", "analyst", "manager", "admin"}
    assert body["capabilities"]["report:send"] == ["admin"]
    assert body["features"]["gnn"] is False

    flat = response.text.lower()
    for forbidden in ("password", "secret", "jwt", "salt", "webhook", "dsn", "api_key"):
        assert forbidden not in flat


def test_openapi_is_published(client) -> None:
    document = client.get("/openapi.json").json()
    assert document["info"]["title"] == "TAZAMA API"
    assert "/api/v1/meta" in document["paths"]
    assert "/healthz" in document["paths"]


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/api/v1/meta"])
def test_public_endpoints_need_no_token(client, path: str) -> None:
    assert client.get(path).status_code in (200, 503)


def test_cors_allowlist_is_honoured(client) -> None:
    allowed = client.get("/api/v1/meta", headers={"Origin": "http://localhost:3000"})
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:3000"
    denied = client.get("/api/v1/meta", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in denied.headers
