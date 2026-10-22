"""DTOs and pure helpers for the ingestion module.

Nothing here does I/O. The two inbound shapes (`InboundComplaint`, `InboundCareCall`) are
what a channel adapter hands over; the two record shapes (`ComplaintRecord`,
`CareCallRecord`) are what a repository writes; the two result shapes are what the caller
gets back.

Two rules shape everything in this file:
  * **No raw MSISDN leaves the boundary.** `InboundComplaint.raw_msisdn` is the last place
    a raw number exists. Records carry a pseudonym token and, at most, the *shape* the
    input used (`msisdn_format`) so normalisation stays testable without the number.
  * **Dedupe is deterministic.** The same fact, submitted twice inside the same window,
    must produce the same key on any machine and after any replay, so the keys are derived
    from normalised content rather than from a sequence or a wall clock.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tazama.core.clock import ensure_utc
from tazama.core.ids import deterministic_id
from tazama.domain.value_objects import Channel, Language

#: Longest complaint text we store. Anything beyond this is truncated for storage and the
#: original length is recorded, so "very long complaint text" is a data point, not a crash.
MAX_TEXT_CHARS = 8000

#: Default dedupe bucket. The duplicate-storm scenario depends on this collapsing repeat
#: submissions of the same sentence by the same subscriber.
DEFAULT_DEDUPE_WINDOW_MINUTES = 10

#: `source_ts` this far before `received_at` means the event arrived late (queue backlog,
#: store-and-forward SMS, an offline app syncing). Late is normal; it is recorded, not fixed.
DEFAULT_LATE_THRESHOLD_MINUTES = 15

#: `received_at` further ahead of now than this is a broken clock, not a prediction.
DEFAULT_MAX_FUTURE_SKEW_HOURS = 24

#: Language is NOT NULL in the schema but ingestion does not classify: it stores
#: "undetermined" (ISO 639-2 `und`) and enrichment overwrites it later.
UNDETERMINED_LANGUAGE = "und"

#: Flags returned on a result so the caller knows *why* a human is wanted. Stable strings:
#: they end up in audit rows and in the review queue UI.
REASON_UNPARSEABLE_MSISDN = "unparseable_msisdn"
REASON_MISSING_MSISDN = "missing_msisdn"
REASON_EMPTY_TEXT = "empty_text"
REASON_TRUNCATED_TEXT = "truncated_text"
REASON_LATE_EVENT = "late_event"
REASON_CLOCK_SKEW = "clock_skew"

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


# ------------------------------------------------------------------ normalisation


def normalise_text(text: str) -> str:
    """Canonical form used for the dedupe fingerprint only.

    Casefold, drop punctuation, collapse whitespace. This is what makes
    "Hakuna network!!" and "hakuna network" the same complaint. The *stored* text is never
    normalised — an analyst must see what the customer actually wrote.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    stripped = _PUNCTUATION.sub(" ", folded)
    return _WHITESPACE.sub(" ", stripped).strip()


def text_fingerprint(text: str) -> str:
    """Short, stable digest of the normalised text. Not a secret; just a bucket label."""
    return hashlib.sha256(normalise_text(text).encode("utf-8")).hexdigest()[:32]


def is_empty_text(text: str) -> bool:
    """Whether the text carries no content at all (whitespace, ".", "-", emoji-only)."""
    return not normalise_text(text)


def window_bucket(moment: datetime, minutes: int) -> datetime:
    """Floor an instant to a dedupe bucket of `minutes`.

    Flooring on the epoch rather than on wall-clock fields keeps the arithmetic correct for
    any window size (including ones that do not divide an hour) and makes the bucketing a
    pure function of the instant, which is what the property test pins down.
    """
    if minutes <= 0:
        raise ValueError(f"dedupe window must be positive, got {minutes}")
    span = minutes * 60
    epoch = int(ensure_utc(moment).timestamp())
    return datetime.fromtimestamp(epoch - (epoch % span), tz=UTC)


def msisdn_input_shape(raw: str | None) -> str | None:
    """Which *shape* the input used — never the number itself.

    Stored in `subscriber.msisdn_format` so the seeded variety of formats
    (+2547.., 07.., 2547.., 7..) exercises normalisation without retaining an identifier.
    """
    if raw is None:
        return None
    text = unicodedata.normalize("NFKC", raw).strip()
    # Every non-digit is dropped below, so no-break spaces need no special case.
    digits = re.sub(r"[^\d+]", "", text)
    if digits.startswith("+254") or digits.startswith("00254"):
        return "plus_254"
    if digits.startswith("254"):
        return "cc_254"
    if digits.startswith("0"):
        return "national_0"
    if digits:
        return "bare_9"
    return "unknown"


def _clip(value: str | None, limit: int) -> str | None:
    """Clamp to the column width. Silent truncation beats a 500 on a care agent's screen."""
    if value is None:
        return None
    return value[:limit]


# --------------------------------------------------------------------- dedupe keys


def complaint_dedupe_key(
    *,
    subscriber_id: str | None,
    channel: str,
    fingerprint: str,
    bucket: datetime,
) -> str:
    """Deterministic key for "this subscriber said this, on this channel, in this bucket".

    An anonymous submission (unparseable or absent MSISDN) buckets under `anon`, which
    deliberately does *not* collapse different anonymous callers together beyond the text
    and channel — the alternative, treating them as one subscriber, would hide a real storm.
    """
    return deterministic_id(
        "DDK",
        subscriber_id or "anon",
        channel,
        fingerprint,
        bucket.isoformat(),
    )


def care_call_dedupe_key(
    *,
    subscriber_id: str | None,
    reason_code: str,
    bucket: datetime,
) -> str:
    """Same discipline for care calls: subscriber, reason and bucket."""
    return deterministic_id(
        "DDK",
        "care",
        subscriber_id or "anon",
        reason_code,
        bucket.isoformat(),
    )


def idempotency_dedupe_key(idempotency_key: str) -> str:
    """Storage key for an explicit idempotency key (PRD section 8: keys on every write).

    The schema has no dedicated idempotency column, so the key is folded into the same
    `dedupe_key` slot under its own namespace. The namespace is what keeps a caller-supplied
    key from ever colliding with a computed one.
    """
    if not idempotency_key.strip():
        raise ValueError("idempotency key must not be blank")
    return deterministic_id("IDK", idempotency_key.strip())


def body_hash(
    *,
    subscriber_id: str | None,
    channel: str,
    text: str,
    received_at: datetime,
    stated_location: str | None,
    region: str | None,
    cell_id: str | None,
) -> str:
    """Digest of the fields that make a complaint *this* complaint.

    Replaying an idempotency key with a different body must be a conflict, so the body has
    to be comparable after the fact. Every input here is a column we store, which means the
    hash can be recomputed from the stored row — no extra column, no drift.
    """
    parts = (
        subscriber_id or "",
        channel,
        normalise_text(text),
        ensure_utc(received_at).isoformat(),
        (stated_location or "").strip().casefold(),
        (region or "").strip().casefold(),
        (cell_id or "").strip().casefold(),
    )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


def care_call_body_hash(
    *,
    subscriber_id: str | None,
    reason_code: str,
    reason_text: str | None,
    started_at: datetime,
    duration_s: int,
    region: str | None,
    cell_id: str | None,
) -> str:
    parts = (
        subscriber_id or "",
        reason_code,
        normalise_text(reason_text or ""),
        ensure_utc(started_at).isoformat(),
        str(duration_s),
        (region or "").strip().casefold(),
        (cell_id or "").strip().casefold(),
    )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


# ------------------------------------------------------------------------ config


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestionConfig:
    """Knobs the service reads. Defaults match the PRD; the composition root may override."""

    dedupe_window_minutes: int = DEFAULT_DEDUPE_WINDOW_MINUTES
    max_text_chars: int = MAX_TEXT_CHARS
    late_threshold_minutes: int = DEFAULT_LATE_THRESHOLD_MINUTES
    max_future_skew_hours: int = DEFAULT_MAX_FUTURE_SKEW_HOURS

    def __post_init__(self) -> None:
        if self.dedupe_window_minutes <= 0:
            raise ValueError("dedupe_window_minutes must be positive")
        if self.max_text_chars <= 0:
            raise ValueError("max_text_chars must be positive")
        if self.max_future_skew_hours <= 0:
            raise ValueError("max_future_skew_hours must be positive")


# ------------------------------------------------------------------------ inbound


@dataclass(frozen=True, slots=True, kw_only=True)
class InboundComplaint:
    """One inbound complaint, exactly as a channel adapter received it.

    `text` is **untrusted data**. Nothing in this module parses it for meaning, matches it
    against commands, or lets it influence control flow: it is hashed for dedupe, measured
    for length, and stored verbatim. Classification happens later, behind the prompt guard.
    """

    raw_msisdn: str | None
    text: str
    channel: Channel
    received_at: datetime
    source_ts: datetime | None = None
    stated_location: str | None = None
    external_ref: str | None = None
    language: Language | None = None
    cell_id: str | None = None
    region: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class InboundCareCall:
    """One inbound care call. Same pseudonymisation and dedupe discipline as a complaint."""

    raw_msisdn: str | None
    reason_code: str
    started_at: datetime
    duration_s: int = 0
    reason_text: str | None = None
    channel: Channel = Channel.CALL_CENTRE
    source_ts: datetime | None = None
    agent_ref: str | None = None
    region: str | None = None
    cell_id: str | None = None
    is_repeat: bool = False
    idempotency_key: str | None = None


# ------------------------------------------------------------------------ records


@dataclass(frozen=True, slots=True, kw_only=True)
class ComplaintRecord:
    """The `complaint` row to write. Column names, widths and nullability match the schema."""

    id: str
    received_at: datetime
    source_ts: datetime | None
    ingested_at: datetime
    channel: str
    language: str
    raw_text: str
    subscriber_id: str | None
    stated_location: str | None
    region: str | None
    cell_id: str | None
    status: str
    dedupe_key: str
    external_ref: str | None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "received_at": self.received_at,
            "source_ts": self.source_ts,
            "ingested_at": self.ingested_at,
            "channel": self.channel,
            "language": self.language,
            "raw_text": self.raw_text,
            "subscriber_id": self.subscriber_id,
            "stated_location": self.stated_location,
            "region": self.region,
            "cell_id": self.cell_id,
            "status": self.status,
            "dedupe_key": self.dedupe_key,
            "external_ref": self.external_ref,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CareCallRecord:
    """The `care_call` row to write.

    `care_call` has no `dedupe_key` column, so the dedupe key *is* the primary key: the id
    is derived from it. Re-inserting the same fact therefore collides on the primary key
    rather than silently duplicating.
    """

    id: str
    started_at: datetime
    duration_s: int
    subscriber_id: str | None
    reason_code: str
    reason_text: str | None
    channel: str
    agent_ref: str | None
    region: str | None
    cell_id: str | None
    is_repeat: bool

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "started_at": self.started_at,
            "duration_s": self.duration_s,
            "subscriber_id": self.subscriber_id,
            "reason_code": self.reason_code,
            "reason_text": self.reason_text,
            "channel": self.channel,
            "agent_ref": self.agent_ref,
            "region": self.region,
            "cell_id": self.cell_id,
            "is_repeat": self.is_repeat,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredComplaint:
    """What a repository returns for an existing complaint.

    It carries exactly the columns `body_hash` needs, so an idempotency conflict can be
    detected by recomputing the hash from the stored row instead of trusting a second
    implementation in the adapter.
    """

    id: str
    subscriber_id: str | None
    channel: str
    raw_text: str
    received_at: datetime
    source_ts: datetime | None
    stated_location: str | None
    region: str | None
    cell_id: str | None
    dedupe_key: str | None

    @property
    def body_hash(self) -> str:
        return body_hash(
            subscriber_id=self.subscriber_id,
            channel=self.channel,
            text=self.raw_text,
            received_at=self.received_at,
            stated_location=self.stated_location,
            region=self.region,
            cell_id=self.cell_id,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredCareCall:
    id: str
    subscriber_id: str | None
    reason_code: str
    reason_text: str | None
    started_at: datetime
    duration_s: int
    region: str | None
    cell_id: str | None

    @property
    def body_hash(self) -> str:
        return care_call_body_hash(
            subscriber_id=self.subscriber_id,
            reason_code=self.reason_code,
            reason_text=self.reason_text,
            started_at=self.started_at,
            duration_s=self.duration_s,
            region=self.region,
            cell_id=self.cell_id,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SubscriberSnapshot:
    """The slice of `subscriber` ingestion reads and refreshes."""

    id: str
    region: str | None = None
    home_cell_id: str | None = None
    last_cell_id: str | None = None
    last_seen_at: datetime | None = None
    segment: str = "consumer"
    is_roaming: bool = False
    opt_out: bool = False


# ------------------------------------------------------------------------ results


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestResult:
    """What one ingest did. Never contains a raw MSISDN and never contains the text."""

    complaint_id: str
    dedupe_key: str
    subscriber_id: str | None
    duplicate: bool = False
    published: bool = False
    needs_human: bool = False
    is_late: bool = False
    is_skewed: bool = False
    text_truncated: bool = False
    original_text_length: int = 0
    idempotency_key_used: bool = False
    reasons: tuple[str, ...] = ()

    @property
    def stored(self) -> bool:
        """False only for a duplicate: nothing new was written."""
        return not self.duplicate


@dataclass(frozen=True, slots=True, kw_only=True)
class CareCallResult:
    care_call_id: str
    dedupe_key: str
    subscriber_id: str | None
    duplicate: bool = False
    published: bool = False
    needs_human: bool = False
    is_late: bool = False
    is_skewed: bool = False
    idempotency_key_used: bool = False
    reasons: tuple[str, ...] = ()


# ------------------------------------------------------------------------ evidence


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestEvidence:
    """A reference, not a copy — the same shape the audit module stores.

    Declared locally because modules may not import one another; the composition root's
    decision-log adapter accepts any object with these fields.
    """

    kind: str
    ref: str
    note: str = ""
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref, "note": self.note, "weight": self.weight}


@dataclass(frozen=True, slots=True, kw_only=True)
class _Prepared:
    """Internal: the validated, pseudonymised view of an inbound message."""

    subscriber_id: str | None
    msisdn_format: str | None
    text: str
    original_text_length: int
    truncated: bool
    is_late: bool
    is_skewed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def clip_text(text: str, limit: int) -> tuple[str, bool]:
    """Truncate for storage, reporting whether anything was cut."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def clip_field(value: str | None, limit: int) -> str | None:
    return _clip(value, limit)


def dedupe_reasons(*groups: Sequence[str]) -> tuple[str, ...]:
    """Union of reason flags, order-preserving and duplicate-free."""
    seen: dict[str, None] = {}
    for group in groups:
        for reason in group:
            seen.setdefault(reason, None)
    return tuple(seen)
