"""DTOs and pure helpers for site identification.

The complaint -> network-element join is the hardest problem in this product (PRD section
8), so the output is deliberately *explainable*: every signal that contributed is listed in
the evidence with its own weight, and the runner-up cells come back as candidates. A
reviewer must be able to read a `SiteGuess` and say "yes, that is why".

Nothing here does I/O and nothing here reads a clock.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

#: `method` values. Stable strings: they are stored on the decision and shown in the UI.
METHOD_SOURCE_PROVIDED = "source_provided"
METHOD_SERVING_CELL = "recent_serving_cell"
METHOD_STATED_LOCATION = "stated_location"
METHOD_TOPOLOGY = "topology"
METHOD_REGION_FALLBACK = "region_fallback"
METHOD_UNRESOLVED = "unresolved"

#: Why a human is wanted. Also stable.
REASON_NO_CANDIDATE = "no_resolvable_cell"
REASON_LOW_CONFIDENCE = "below_confidence_threshold"
REASON_AMBIGUOUS_LOCATION = "ambiguous_stated_location"
REASON_ROAMING = "roaming_subscriber"
REASON_STALE_SERVING_CELL = "stale_serving_cell"
REASON_NO_SERVING_CELL = "no_serving_cell"
REASON_UNKNOWN_CELL = "cell_not_in_directory"
REASON_REGION_ONLY = "region_only_match"

#: Tokens that carry no locating information. Kept short on purpose: an over-eager stop
#: list is how "Nairobi West" silently becomes "Nairobi".
STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "at",
        "in",
        "on",
        "near",
        "around",
        "by",
        "my",
        "i",
        "am",
        "is",
        "here",
        "area",
        "town",
        "road",
        "rd",
        "street",
        "st",
        "estate",
        "ya",
        "na",
        "kwa",
        "hapa",
    },
)

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalise_location(text: str) -> str:
    """Casefold, drop punctuation, collapse whitespace. Cheap and good enough."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", folded)).strip()


def tokenise_location(text: str | None) -> tuple[str, ...]:
    """Locating tokens from free text, stopwords and single characters removed."""
    if not text:
        return ()
    words = normalise_location(text).split()
    return tuple(w for w in words if len(w) > 1 and w not in STOPWORDS)


def token_overlap(query: tuple[str, ...], target: tuple[str, ...]) -> int:
    """How many distinct query tokens appear in the target name."""
    return len(set(query) & set(target))


# ------------------------------------------------------------------------ config


@dataclass(frozen=True, slots=True, kw_only=True)
class SiteIdConfig:
    """Signal weights and decay shape. Tunable without touching the algorithm.

    The weights are modelled, not measured: they encode "a recent serving cell beats a
    place name, and a place name beats a region" and nothing stronger. They are listed in
    the evidence of every decision so a reviewer can argue with them.
    """

    #: A cell id handed over by the source system is trusted: the network told us.
    source_provided_confidence: float = 0.95
    #: Full weight of a fresh serving cell.
    serving_cell_weight: float = 0.92
    #: Weight of a multi-token match on a site name.
    site_name_weight: float = 0.72
    #: Weight of a multi-token match on a cell name.
    cell_name_weight: float = 0.68
    #: A bare region name locates a region, not a cell.
    region_weight: float = 0.34
    #: A single-token match is much weaker than a multi-token one.
    single_token_factor: float = 0.55
    #: Added when topology agrees (the serving cell sits under the named site).
    topology_agreement_bonus: float = 0.12
    #: Subtracted per extra equally-plausible site, down to `min_ambiguity_factor`.
    ambiguity_penalty_per_extra: float = 0.14
    min_ambiguity_factor: float = 0.45
    #: Serving-cell age decay breakpoints.
    fresh_window: timedelta = timedelta(hours=1)
    decay_window: timedelta = timedelta(hours=24)
    stale_window: timedelta = timedelta(days=7)
    #: Decay floor at `decay_window` and at `stale_window`.
    decay_at_24h: float = 0.45
    decay_at_7d: float = 0.15
    #: A stale `last_cell_id` is still worth more than nothing; the home cell is the
    #: fallback of last resort and never on its own enough to skip a human.
    home_cell_weight: float = 0.30
    #: Caps on how much the adapters are asked for. Unbounded fan-out is a DoS on ourselves.
    max_candidates: int = 10
    max_region_cells: int = 200
    max_name_matches: int = 50

    def __post_init__(self) -> None:
        if not 0.0 < self.source_provided_confidence <= 1.0:
            raise ValueError("source_provided_confidence must be in (0, 1]")
        if not self.fresh_window < self.decay_window < self.stale_window:
            raise ValueError("decay windows must increase: fresh < decay < stale")
        if self.max_candidates <= 0:
            raise ValueError("max_candidates must be positive")


def serving_cell_decay(age: timedelta, config: SiteIdConfig) -> float:
    """How much a serving-cell observation is still worth, by age.

    Full weight under an hour, a straight line down to `decay_at_24h` by a day and to
    `decay_at_7d` by a week, zero after that: a cell a subscriber used ten days ago says
    nothing about where they are standing now.
    """
    if age < timedelta(0):
        # An observation from the future is clock skew; treat it as fresh rather than
        # inventing a negative age.
        return 1.0
    if age <= config.fresh_window:
        return 1.0
    if age <= config.decay_window:
        span = (config.decay_window - config.fresh_window).total_seconds()
        travelled = (age - config.fresh_window).total_seconds() / span
        return 1.0 - travelled * (1.0 - config.decay_at_24h)
    if age <= config.stale_window:
        span = (config.stale_window - config.decay_window).total_seconds()
        travelled = (age - config.decay_window).total_seconds() / span
        return config.decay_at_24h - travelled * (config.decay_at_24h - config.decay_at_7d)
    return 0.0


# ------------------------------------------------------------------------ inputs


@dataclass(frozen=True, slots=True, kw_only=True)
class ComplaintLocation:
    """The locating facts of one complaint. Pseudonymised: no raw identifier reaches here.

    `stated_location` is **untrusted text**. It is tokenised and compared against our own
    names; it never becomes part of a query string and never influences control flow
    beyond which rows are matched.
    """

    complaint_id: str
    received_at: datetime
    subscriber_id: str | None = None
    stated_location: str | None = None
    region: str | None = None
    cell_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SubscriberPosition:
    """Where the network last saw a subscriber. Declared locally: modules share no DTOs."""

    id: str
    region: str | None = None
    home_cell_id: str | None = None
    last_cell_id: str | None = None
    last_seen_at: datetime | None = None
    is_roaming: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class CellInfo:
    id: str
    site_id: str
    name: str
    region: str
    lat: float | None = None
    lon: float | None = None
    tech: str | None = None
    status: str = "active"

    @property
    def name_tokens(self) -> tuple[str, ...]:
        return tokenise_location(self.name)


@dataclass(frozen=True, slots=True, kw_only=True)
class SiteInfo:
    id: str
    name: str
    region: str
    lat: float | None = None
    lon: float | None = None
    parent_node_id: str | None = None

    @property
    def name_tokens(self) -> tuple[str, ...]:
        return tokenise_location(self.name)


@dataclass(frozen=True, slots=True, kw_only=True)
class NameMatch:
    """A site, cell or region whose name shares tokens with the stated location.

    The adapter finds *possible* matches cheaply (a bounded, bound-parameter LIKE); the
    scoring happens in the service, from `tokens`, so the ranking rule is unit-testable
    without a database.
    """

    kind: str  # site | cell | region
    id: str
    name: str
    region: str | None = None
    site_id: str | None = None

    @property
    def tokens(self) -> tuple[str, ...]:
        return tokenise_location(self.name)


@dataclass(frozen=True, slots=True, kw_only=True)
class TopologyHop:
    """One step up the topology from a node towards the core."""

    node_id: str
    kind: str
    name: str = ""
    depth: int = 1


# ------------------------------------------------------------------------ output


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalEvidence:
    """A reference plus the weight it contributed. Same shape the audit module stores."""

    kind: str
    ref: str
    note: str = ""
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref, "note": self.note, "weight": self.weight}


@dataclass(frozen=True, slots=True, kw_only=True)
class Candidate:
    """A cell that could be the answer, with the score and the signal that proposed it."""

    cell_id: str
    score: float
    source: str
    site_id: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "site_id": self.site_id,
            "score": round(self.score, 4),
            "source": self.source,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SiteGuess:
    """The answer. `confidence` is 0..1; `method` names the signal that dominated.

    `needs_human=True` means the pipeline must stop here: nothing downstream may treat a
    low-confidence cell as fact (PRD section 12 — AI recommends, rules decide, humans
    approve).
    """

    complaint_id: str
    cell_id: str | None
    site_id: str | None
    confidence: float
    method: str
    needs_human: bool = False
    evidence: tuple[SignalEvidence, ...] = ()
    candidates: tuple[Candidate, ...] = ()
    reasons: tuple[str, ...] = ()
    region: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")

    @property
    def resolved(self) -> bool:
        """A usable answer: a cell, and a confidence a human has not been asked about."""
        return self.cell_id is not None and not self.needs_human

    def to_payload(self) -> dict[str, Any]:
        return {
            "complaint_id": self.complaint_id,
            "cell_id": self.cell_id,
            "site_id": self.site_id,
            "region": self.region,
            "confidence": round(self.confidence, 4),
            "method": self.method,
            "needs_human": self.needs_human,
            "reasons": list(self.reasons),
            "candidates": [c.to_dict() for c in self.candidates],
        }
