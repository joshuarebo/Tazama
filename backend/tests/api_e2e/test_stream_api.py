"""The SSE endpoint: authentication by header or single-use ticket, framing, headers.

Every test passes `limit`, so the stream ends on its own rather than leaving the test
waiting on an endless generator.
"""

from __future__ import annotations

import pytest


def test_stream_requires_authentication(client) -> None:
    response = client.get("/api/v1/stream/incidents?limit=1")
    assert response.status_code == 401
    assert response.json()["type"].endswith("/unauthenticated")


def test_a_general_purpose_token_is_not_accepted_in_the_url(client, settings) -> None:
    """The JWT grants the whole API, so it must never be the thing that travels in a URL.

    A JWT is longer than a ticket, so the parameter bounds reject it before the handler
    looks at it (422); either way it does not open the stream.
    """
    from tazama.core.auth import issue_token
    from tazama.core.clock import SystemClock
    from tazama.core.security import Principal

    token = issue_token(
        Principal(subject="noc", role="noc_engineer"),
        secret=settings.JWT_SECRET.get_secret_value(),
        clock=SystemClock(),
    )
    response = client.get(f"/api/v1/stream/incidents?limit=1&ticket={token}")
    assert response.status_code in (401, 422)
    assert "event: hello" not in response.text


@pytest.mark.parametrize("ticket", ["", "short", "x" * 200])
def test_malformed_tickets_are_refused(client, ticket: str) -> None:
    response = client.get(f"/api/v1/stream/incidents?limit=1&ticket={ticket}")
    assert response.status_code in (401, 422)


def test_header_token_opens_the_stream(client, auth_headers) -> None:
    response = client.get(
        "/api/v1/stream/incidents?limit=1",
        headers=auth_headers("noc_engineer"),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"].startswith("no-store")
    # Without this, a buffering reverse proxy makes the stream useless.
    assert response.headers["x-accel-buffering"] == "no"
    # A ticket must not travel onward in a Referer header.
    assert response.headers["referrer-policy"] == "no-referrer"

    body = response.text
    assert "event: hello" in body
    assert "heartbeat_s" in body
    assert body.endswith("\n\n")


def test_ticket_flow_end_to_end(client, auth_headers) -> None:
    issued = client.post("/api/v1/stream/ticket", headers=auth_headers("noc_engineer"))
    assert issued.status_code == 200
    body = issued.json()
    assert body["expires_in"] <= 60
    assert body["url"].startswith("/api/v1/stream/incidents?ticket=")

    # No Authorization header this time: the ticket is the credential.
    streamed = client.get(f"/api/v1/stream/incidents?limit=1&ticket={body['ticket']}")
    assert streamed.status_code == 200
    assert "event: hello" in streamed.text


def test_a_ticket_cannot_be_reused(client, auth_headers) -> None:
    ticket = client.post("/api/v1/stream/ticket", headers=auth_headers("manager")).json()["ticket"]
    assert client.get(f"/api/v1/stream/incidents?limit=1&ticket={ticket}").status_code == 200
    assert client.get(f"/api/v1/stream/incidents?limit=1&ticket={ticket}").status_code == 401


def test_issuing_a_ticket_requires_authentication(client) -> None:
    assert client.post("/api/v1/stream/ticket").status_code == 401


@pytest.mark.parametrize("role", ["care_agent", "noc_engineer", "analyst", "manager", "admin"])
def test_every_role_that_reads_incidents_can_subscribe(client, auth_headers, role: str) -> None:
    response = client.get("/api/v1/stream/incidents?limit=1", headers=auth_headers(role))
    assert response.status_code == 200


def test_limit_is_bounded(client, auth_headers) -> None:
    response = client.get("/api/v1/stream/incidents?limit=99999", headers=auth_headers("admin"))
    assert response.status_code == 422


def test_stream_endpoints_are_documented(client) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/stream/incidents" in paths
    assert "/api/v1/stream/ticket" in paths
