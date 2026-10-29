"""The digest card: no PII, no active content, no silent truncation.

A card is the most widely forwarded artefact this product produces, so these are the
tests that matter most about it. The PII check is deliberately adversarial: it tries the
identifier shapes a real leak would take, in each channel's formatting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tazama.modules.reporting.digest import (
    MAX_CARD_BYTES,
    PiiInCardError,
    assert_no_pii,
    build_card,
    card_size_bytes,
    escape_markdown,
)
from tazama.modules.reporting.models import Digest, DigestIncident, DigestSection

T0 = datetime(2026, 3, 17, 7, 30, tzinfo=UTC)


def _incident(index: int, **overrides: object) -> DigestIncident:
    defaults: dict[str, object] = {
        "incident_id": f"INC-{index:04d}",
        "title": f"Congestion on Westlands {index}",
        "priority": "P2",
        "status": "detected",
        "cause": "congestion",
        "region": "Nairobi",
        "affected_subs": 1200 + index,
        "affected_cells": 3,
        "complaint_count": 12,
        "detected_at": T0 - timedelta(hours=2),
        "duration_minutes": 125.0,
    }
    defaults.update(overrides)
    return DigestIncident(**defaults)  # type: ignore[arg-type]


def _digest(**overrides: object) -> Digest:
    defaults: dict[str, object] = {
        "report_id": "RPT-DAILY-20260317T0730",
        "kind": "daily",
        "title": "TAZAMA daily network-care digest",
        "generated_at": T0,
        "window_start": T0 - timedelta(days=1),
        "window_end": T0,
        "display_tz": "Africa/Nairobi",
        "headline": (("Incidents", "3 live, 1 suppressed"),),
        "incidents": (_incident(1), _incident(2)),
    }
    defaults.update(overrides)
    return Digest(**defaults)  # type: ignore[arg-type]


def test_a_card_renders_the_headline_and_every_incident() -> None:
    card = build_card(_digest())
    text = str(card)
    assert "TAZAMA daily network-care digest" in text
    assert "INC-0001" in text and "INC-0002" in text
    assert card["version"] == "1.5"


def test_demo_data_is_stated_on_the_face_of_the_card() -> None:
    card = build_card(_digest(is_demo=True))
    assert "DEMO DATA" in str(card)
    card = build_card(_digest(is_demo=False))
    assert "DEMO DATA" not in str(card)


@pytest.mark.parametrize(
    "leak",
    [
        "0712345678",
        "254712345678",
        "+254712345678",
        "+254 712 345 678",
        "0112345678",
        "SUB-ABCDEFGH23456727",
        "customer@example.com",
    ],
)
def test_an_identifier_in_any_shape_blocks_the_report(leak: str) -> None:
    """A leak raises. It is never quietly redacted — that would hide the source."""
    digest = _digest(incidents=(_incident(1, title=f"Outage affecting {leak}"),))
    with pytest.raises(PiiInCardError):
        build_card(digest)


def test_the_exception_does_not_repeat_the_offending_value() -> None:
    """The message goes to logs, and logs must not become the leak."""
    digest = _digest(incidents=(_incident(1, title="Outage for 0712345678"),))
    with pytest.raises(PiiInCardError) as caught:
        build_card(digest)
    assert "0712345678" not in str(caught.value)


def test_a_cell_or_incident_id_is_not_mistaken_for_an_identifier() -> None:
    """The guard must not fire on the ids a digest legitimately carries."""
    assert_no_pii({"text": "CELL-NRB-001 / INC-0001 / SITE-NRB-001 / 1200 subscribers"})


def test_markdown_and_html_in_data_are_neutralised() -> None:
    escaped = escape_markdown("<script>alert(1)</script> **bold** [link](http://x)")
    assert "<" not in escaped and ">" not in escaped
    assert "**" not in escaped
    assert "\\[link\\]" in escaped


def test_a_long_field_is_capped_rather_than_carried_whole() -> None:
    escaped = escape_markdown("x" * 5_000)
    assert len(escaped) <= 300
    assert escaped.endswith("…")


def test_the_card_stays_under_the_size_limit_and_says_what_it_dropped() -> None:
    digest = _digest(incidents=tuple(_incident(index) for index in range(400)))
    card = build_card(digest)
    assert card_size_bytes(card) <= MAX_CARD_BYTES
    # Truncation is disclosed, so nobody reads a partial card as the whole picture.
    assert "not shown" in str(card)


def test_a_small_card_drops_nothing_and_says_nothing_about_dropping() -> None:
    card = build_card(_digest())
    assert "not shown" not in str(card)


def test_an_empty_window_renders_as_an_answer_not_a_blank() -> None:
    card = build_card(_digest(incidents=()))
    assert "No live incidents in this window." in str(card)


def test_suppression_is_visible_on_the_line() -> None:
    digest = _digest(
        incidents=(_incident(1, suppressed_reason="planned_maintenance"),),
    )
    assert "suppressed: planned maintenance" in str(build_card(digest))


def test_sections_and_caveats_are_rendered() -> None:
    digest = _digest(
        sections=(DigestSection(heading="Needs a human", lines=("2 proposals waiting",)),),
        caveats=("Subscriber counts are estimates.",),
    )
    text = str(build_card(digest))
    assert "Needs a human" in text
    assert "2 proposals waiting" in text
    assert "Subscriber counts are estimates." in text


def test_an_action_url_is_added_only_when_given() -> None:
    assert "actions" not in build_card(_digest())
    card = build_card(_digest(), console_url="http://localhost:3000")
    assert card["actions"][0]["url"] == "http://localhost:3000"


def test_a_bad_timezone_does_not_take_the_report_down() -> None:
    """A misconfigured DISPLAY_TZ degrades the formatting, never the delivery."""
    card = build_card(_digest(display_tz="Mars/Olympus"))
    assert "TAZAMA daily network-care digest" in str(card)


def test_age_text_reads_as_a_duration() -> None:
    assert _incident(1, duration_minutes=None).age_text == "ongoing"
    assert _incident(1, duration_minutes=45.0).age_text == "45m"
    assert _incident(1, duration_minutes=125.0).age_text == "2h 05m"
