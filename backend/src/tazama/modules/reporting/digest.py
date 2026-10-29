"""Render a `Digest` as an Adaptive Card, and prove it is safe to post.

Three rules this module exists to enforce:

1. **No PII, ever.** A Teams channel is forwarded, screenshotted and archived. The card is
   scanned for subscriber-identifier shapes before it is returned, and a hit raises rather
   than redacts — a reporting pipeline that quietly strips a leak teaches nobody.
2. **No active content.** Adaptive Cards render Markdown in `TextBlock`, so every string is
   escaped and length-capped.
3. **Size.** Teams rejects large payloads, so the card is built smallest-first (headline,
   then as many incident rows as fit) and the number dropped is stated on the card itself
   instead of silently truncating the operator's picture.

Pure functions only: no clock, no HTTP, no settings. The sender decides where a card goes;
this module decides what one says.
"""

from __future__ import annotations

import json
import re
from typing import Any
from zoneinfo import ZoneInfo

from tazama.modules.reporting.models import Digest, DigestIncident

#: Teams' documented limit for a Workflows/connector payload is 28 KB. Leaving headroom
#: for the envelope the sender adds keeps us well clear of a rejection we cannot see.
MAX_CARD_BYTES = 24_000

#: Longest string any single field may carry into a card.
MAX_FIELD_CHARS = 300

#: Shapes that must never appear in a card. Kenyan MSISDNs in every form the channels
#: produce, plus the pseudonym token — a token is not a phone number, but it is still a
#: subscriber identifier and it has no business in a management report.
_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("msisdn", re.compile(r"(?<!\d)(?:\+?254|0)\s?7\d{2}\s?\d{3}\s?\d{3}(?!\d)")),
    ("msisdn", re.compile(r"(?<!\d)(?:\+?254|0)\s?1\d{2}\s?\d{3}\s?\d{3}(?!\d)")),
    ("pseudonym-token", re.compile(r"\bSUB-[A-Z2-7]{8,}\b")),
    ("email", re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")),
)

_PRIORITY_COLOUR = {"P1": "attention", "P2": "warning", "P3": "accent", "P4": "default"}


class PiiInCardError(RuntimeError):
    """Raised when a card would carry a subscriber identifier. Never caught in-process."""


def escape_markdown(text: str) -> str:
    """Neutralise Markdown and HTML in a value that came from data, not from us."""
    collapsed = " ".join(str(text).split())
    if len(collapsed) > MAX_FIELD_CHARS:
        collapsed = f"{collapsed[: MAX_FIELD_CHARS - 1]}…"
    # Strip anything that could open a tag, then escape the Markdown actives.
    collapsed = collapsed.replace("<", "&lt;").replace(">", "&gt;")
    for char in ("*", "_", "`", "[", "]", "|", "#"):
        collapsed = collapsed.replace(char, f"\\{char}")
    return collapsed


def assert_no_pii(payload: Any) -> None:
    """Scan a built card for identifier shapes. Raises on the first hit."""
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    for name, pattern in _PII_PATTERNS:
        match = pattern.search(text)
        if match:
            # The offending value is not included in the message: the exception goes to
            # logs, and logs are exactly where it must not land either.
            raise PiiInCardError(
                f"card would carry a {name} at offset {match.start()}; "
                "reporting is blocked until the source is fixed",
            )


def _local(value: Any, tz: str) -> str:
    try:
        return value.astimezone(ZoneInfo(tz)).strftime("%d %b %H:%M")
    except Exception:
        return str(value)


def _incident_line(incident: DigestIncident, tz: str) -> dict[str, Any]:
    cause = (incident.cause or "cause not yet established").replace("_", " ")
    detail = (
        f"{incident.region} · {cause} · {incident.affected_cells} cell(s) · "
        f"~{incident.affected_subs:,} subscribers · {incident.complaint_count} complaint(s) · "
        f"{incident.age_text}"
    )
    if incident.suppressed_reason:
        detail += f" · suppressed: {incident.suppressed_reason.replace('_', ' ')}"
    return {
        "type": "ColumnSet",
        "columns": [
            {
                "type": "Column",
                "width": "auto",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": escape_markdown(incident.priority),
                        "weight": "bolder",
                        "color": _PRIORITY_COLOUR.get(incident.priority, "default"),
                        "wrap": False,
                    },
                ],
            },
            {
                "type": "Column",
                "width": "stretch",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": escape_markdown(incident.title),
                        "weight": "bolder",
                        "wrap": True,
                    },
                    {
                        "type": "TextBlock",
                        "text": escape_markdown(detail),
                        "isSubtle": True,
                        "spacing": "none",
                        "wrap": True,
                    },
                    {
                        "type": "TextBlock",
                        "text": escape_markdown(
                            f"{incident.incident_id} · detected {_local(incident.detected_at, tz)}",
                        ),
                        "isSubtle": True,
                        "spacing": "none",
                        "size": "small",
                        "wrap": True,
                    },
                ],
            },
        ],
    }


def build_card(digest: Digest, *, console_url: str | None = None) -> dict[str, Any]:
    """Build the Adaptive Card payload for a digest.

    Returns the card itself (schema 1.5), not the Workflows envelope: the sender owns the
    transport shape, and keeping them apart is what let the connector-to-Workflows change
    be a sender-only edit.
    """
    tz = digest.display_tz
    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": escape_markdown(digest.title),
            "size": "large",
            "weight": "bolder",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": escape_markdown(
                f"{_local(digest.window_start, tz)} to {_local(digest.window_end, tz)} ({tz})",
            ),
            "isSubtle": True,
            "spacing": "none",
            "wrap": True,
        },
    ]

    if digest.is_demo:
        # Stated on the card, not only in the console: a forwarded screenshot of synthetic
        # numbers presented as real is the single most likely way this product misleads.
        body.append(
            {
                "type": "TextBlock",
                "text": "**DEMO DATA** — synthetic network, synthetic customers.",
                "color": "warning",
                "weight": "bolder",
                "wrap": True,
            },
        )

    if digest.headline:
        body.append(
            {
                "type": "FactSet",
                "facts": [
                    {"title": escape_markdown(label), "value": escape_markdown(value)}
                    for label, value in digest.headline
                ],
            },
        )

    # Incidents are added one at a time so the size guard can stop at a whole row rather
    # than truncating a card mid-structure.
    skeleton = _card([*body, {"type": "TextBlock", "text": "…", "wrap": True}], digest)
    used = len(json.dumps(skeleton))
    shown = 0
    rows: list[dict[str, Any]] = []
    for incident in digest.incidents:
        row = _incident_line(incident, tz)
        size = len(json.dumps(row))
        if used + size > MAX_CARD_BYTES:
            break
        rows.append(row)
        used += size
        shown += 1

    if rows:
        body.append({"type": "TextBlock", "text": "Incidents", "weight": "bolder", "wrap": True})
        body.extend(rows)
    else:
        body.append(
            {
                "type": "TextBlock",
                "text": "No live incidents in this window.",
                "wrap": True,
            },
        )

    dropped = len(digest.incidents) - shown
    if dropped > 0:
        body.append(
            {
                "type": "TextBlock",
                "text": escape_markdown(
                    f"{dropped} further incident(s) not shown — the card hit its size limit. "
                    "Open the console for the full list.",
                ),
                "isSubtle": True,
                "wrap": True,
            },
        )

    for section in digest.sections:
        body.append(
            {
                "type": "TextBlock",
                "text": escape_markdown(section.heading),
                "weight": "bolder",
                "separator": True,
                "wrap": True,
            },
        )
        body.extend(
            {
                "type": "TextBlock",
                "text": escape_markdown(f"• {line}"),
                "spacing": "none",
                "wrap": True,
            }
            for line in section.lines
        )

    if digest.caveats:
        body.append(
            {
                "type": "TextBlock",
                "text": escape_markdown("Caveats: " + " ".join(digest.caveats)),
                "isSubtle": True,
                "size": "small",
                "separator": True,
                "wrap": True,
            },
        )

    body.append(
        {
            "type": "TextBlock",
            "text": escape_markdown(
                f"report {digest.report_id} · generated {_local(digest.generated_at, tz)}",
            ),
            "isSubtle": True,
            "size": "small",
            "wrap": True,
        },
    )

    card = _card(body, digest)
    if console_url:
        card["actions"] = [
            {"type": "Action.OpenUrl", "title": "Open the console", "url": console_url},
        ]

    assert_no_pii(card)
    return card


def _card(body: list[dict[str, Any]], digest: Digest) -> dict[str, Any]:
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "msteams": {"width": "Full"},
        "metadata": {"webUrl": None},
        "body": body,
        "speak": escape_markdown(digest.title),
    }


def card_size_bytes(card: dict[str, Any]) -> int:
    return len(json.dumps(card))
