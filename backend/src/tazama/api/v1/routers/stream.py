"""The live-updates endpoint behind the Command Center.

The browser's native `EventSource` cannot set an `Authorization` header, so something has
to travel in the URL — and a URL leaks: proxy logs, browser history, referrers. A
general-purpose JWT in a query string is therefore the wrong answer.

Instead, a caller that already holds a token exchanges it (over a normal authenticated
`POST`) for a **single-use ticket** that expires in 30 seconds and is bound to that
principal. The ticket is consumed the first time it is redeemed, so a leaked URL is
worthless by the time anyone reads the log. Clients that can set headers — the web app uses
a fetch-based reader — skip tickets entirely and send the bearer token.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from tazama.api.deps import PrincipalDep, SettingsDep
from tazama.api.stream import (
    TICKET_TTL_SECONDS,
    EventStream,
    TicketStore,
    get_stream,
    get_ticket_store,
)
from tazama.core.auth import verify_token
from tazama.core.errors import Unauthenticated
from tazama.core.logging import get_logger
from tazama.core.security import Principal

router = APIRouter(prefix="/stream", tags=["stream"])
_log = get_logger(__name__)

SSE_MEDIA_TYPE = "text/event-stream"


class TicketResponse(BaseModel):
    ticket: str
    expires_in: int
    url: str


def _principal_from(
    request: Request,
    settings: SettingsDep,
    ticket: str | None,
    tickets: TicketStore,
) -> Principal:
    """Resolve the caller from a bearer header, or from a single-use ticket."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        raw = header.split(" ", 1)[1].strip()
        if raw:
            return verify_token(raw, secret=settings.JWT_SECRET.get_secret_value())

    redeemed = tickets.consume(ticket or "")
    if redeemed is None:
        # One message for missing, unknown, expired and already-used: nothing to probe.
        raise Unauthenticated("Sign in to subscribe to live updates.")
    return Principal(subject=redeemed.subject, role=redeemed.role, display_name=redeemed.subject)


async def _frames(
    stream: EventStream,
    request: Request,
    *,
    limit: int | None = None,
) -> AsyncIterator[bytes]:
    """Render frames until the client disconnects, or until `limit` frames are sent.

    `limit` exists because an endless stream is awkward to test and to demonstrate: with
    it, a test or a `curl` can take the handshake and stop, instead of hanging. Production
    clients omit it.
    """
    sent = 0
    async for event in stream.subscribe():
        if await request.is_disconnected():
            break
        yield event.render().encode("utf-8")
        sent += 1
        if limit is not None and sent >= limit:
            break


@router.post(
    "/ticket",
    response_model=TicketResponse,
    summary="Exchange a bearer token for a single-use, 30-second SSE ticket",
)
async def issue_ticket(principal: PrincipalDep) -> TicketResponse:
    principal.require("incident:read")
    store = get_ticket_store()
    ticket = store.issue(subject=principal.subject, role=principal.role)
    _log.info("sse_ticket_issued", extra={"actor": principal.subject})
    return TicketResponse(
        ticket=ticket.value,
        expires_in=int(TICKET_TTL_SECONDS),
        url=f"/api/v1/stream/incidents?ticket={ticket.value}",
    )


@router.get(
    "/incidents",
    summary="Live incident, case and notification updates (SSE)",
    response_class=StreamingResponse,
)
async def incident_stream(
    request: Request,
    settings: SettingsDep,
    ticket: Annotated[
        str | None,
        Query(min_length=16, max_length=128, description="Single-use ticket from /stream/ticket"),
    ] = None,
    limit: Annotated[
        int | None,
        Query(ge=1, le=1000, description="Stop after this many frames (tests and demos)"),
    ] = None,
) -> StreamingResponse:
    principal = _principal_from(request, settings, ticket, get_ticket_store())
    principal.require("incident:read")
    _log.info("sse_subscribe", extra={"actor": principal.subject, "role": principal.role})

    return StreamingResponse(
        _frames(get_stream(), request, limit=limit),
        media_type=SSE_MEDIA_TYPE,
        headers={
            "Cache-Control": "no-store, no-transform",
            # Tells nginx and friends not to buffer the stream into uselessness.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            # A ticket should never travel onward in a Referer header.
            "Referrer-Policy": "no-referrer",
        },
    )
