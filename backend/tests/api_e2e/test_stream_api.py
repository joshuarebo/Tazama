"""The SSE endpoint: authentication, framing, and headers that keep proxies honest.

Every test passes `limit`, so the stream ends on its own rather than leaving the test
waiting on an endless generator.
"""

from __future__ import annotations

import pytest


def test_stream_requires_authentication(client) -> None:
    response = client.get("/api/v1/stream/incidents?limit=1")
    assert response.status_code == 401
    assert response.json()["type"].endswith("/unauthenticated")


def test_stream_rejects_a_forged_query_token(client) -> None:
    response = client.get("/api/v1/stream/incidents?limit=1&token=not-a-real-token")
    assert response.status_code == 401


def test_stream_opens_and_frames_correctly(client, auth_headers) -> None:
    response = client.get(
        "/api/v1/stream/incidents?limit=1",
        headers=auth_headers("noc_engineer"),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"].startswith("no-store")
    # Without this, a buffering reverse proxy makes the stream useless.
    assert response.headers["x-accel-buffering"] == "no"

    body = response.text
    assert "event: hello" in body
    assert "heartbeat_s" in body
    assert body.endswith("\n\n")


def test_stream_accepts_a_query_token_for_eventsource(client, settings) -> None:
    """Native EventSource cannot set headers, so the query form must work too."""
    from tazama.core.auth import issue_token
    from tazama.core.clock import SystemClock
    from tazama.core.security import Principal

    token = issue_token(
        Principal(subject="noc", role="noc_engineer"),
        secret=settings.JWT_SECRET.get_secret_value(),
        clock=SystemClock(),
    )
    response = client.get(f"/api/v1/stream/incidents?limit=1&token={token}")
    assert response.status_code == 200
    assert "event: hello" in response.text


@pytest.mark.parametrize("role", ["care_agent", "noc_engineer", "analyst", "manager", "admin"])
def test_every_role_that_reads_incidents_can_subscribe(client, auth_headers, role: str) -> None:
    response = client.get("/api/v1/stream/incidents?limit=1", headers=auth_headers(role))
    assert response.status_code == 200


def test_limit_is_bounded(client, auth_headers) -> None:
    response = client.get("/api/v1/stream/incidents?limit=99999", headers=auth_headers("admin"))
    assert response.status_code == 422


def test_stream_endpoint_is_documented(client) -> None:
    document = client.get("/openapi.json").json()
    assert "/api/v1/stream/incidents" in document["paths"]
