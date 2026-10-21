"""The live-updates endpoint behind the Command Center.

Authentication note worth stating plainly: the browser's native `EventSource` cannot send
an `Authorization` header, so this endpoint also accepts the token as a query parameter.
That is a real trade-off — a URL is more likely to end up in a log or a referrer than a
header — so the token is short-lived, the query form is accepted **only** for this
read-only endpoint, and the access log for it drops the query string. A client that can set
headers (the web app uses a fetch-based reader) should use the header form.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from tazama.api.deps import SettingsDep
from tazama.api.stream import EventStream, get_stream
from tazama.core.auth import verify_token
from tazama.core.errors import Unauthenticated
from tazama.core.logging import get_logger
from tazama.core.security import Principal

router = APIRouter(prefix="/stream", tags=["stream"])
_log = get_logger(__name__)

SSE_MEDIA_TYPE = "text/event-stream"


def _principal_from(request: Request, settings: SettingsDep, token: str | None) -> Principal:
    header = request.headers.get("authorization", "")
    raw = header.split(" ", 1)[1].strip() if header.lower().startswith("bearer ") else (token or "")
    if not raw:
        raise Unauthenticated("Sign in to subscribe to live updates.")
    return verify_token(raw, secret=settings.JWT_SECRET.get_secret_value())


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


@router.get(
    "/incidents",
    summary="Live incident, case and notification updates (SSE)",
    response_class=StreamingResponse,
)
async def incident_stream(
    request: Request,
    settings: SettingsDep,
    token: Annotated[str | None, Query(description="Token, for EventSource clients")] = None,
    limit: Annotated[
        int | None,
        Query(ge=1, le=1000, description="Stop after this many frames (tests and demos)"),
    ] = None,
) -> StreamingResponse:
    principal = _principal_from(request, settings, token)
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
        },
    )
