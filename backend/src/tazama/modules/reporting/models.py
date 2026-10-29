"""What a digest is, before anything knows it will become a Teams card.

Keeping the digest a plain structure — not a card — is what lets the same content be
rendered to Adaptive Card JSON, to the preview panel in the console, and (later) to e-mail,
without three versions of the truth drifting apart. The card builder in `digest.py` is a
pure function of these objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class DigestIncident:
    """One incident as a digest line.

    Deliberately narrow: an id, what is wrong, how big it is, and how long it has run.
    No subscriber identifiers of any kind — a Teams channel is a wider audience than the
    console, and a digest is forwarded far more often than it is read in place.
    """

    incident_id: str
    title: str
    priority: str
    status: str
    cause: str | None
    region: str
    affected_subs: int
    affected_cells: int
    complaint_count: int
    detected_at: datetime
    duration_minutes: float | None
    suppressed_reason: str | None = None

    @property
    def age_text(self) -> str:
        if self.duration_minutes is None:
            return "ongoing"
        hours, minutes = divmod(int(self.duration_minutes), 60)
        return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


@dataclass(frozen=True, slots=True)
class DigestSection:
    """A named group of lines, with the heading the card will use."""

    heading: str
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Digest:
    """A complete report, ready to render.

    `report_id` is deterministic — kind plus the window — so a retried send is recognised
    as the same report and never posted twice. That is the whole idempotency story for
    reporting, and it lives here rather than in the sender because the sender should not be
    deciding what counts as "the same report".
    """

    report_id: str
    kind: str
    title: str
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    display_tz: str
    headline: tuple[tuple[str, str], ...]
    incidents: tuple[DigestIncident, ...]
    sections: tuple[DigestSection, ...] = ()
    caveats: tuple[str, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)
    #: True when the digest describes synthetic data, which the card states on its face.
    is_demo: bool = True
