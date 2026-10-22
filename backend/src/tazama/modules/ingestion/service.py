"""Ingestion: turn an inbound message into one stored, deduplicated, pseudonymised row.

This module does exactly one job and resists doing any other. It does **not** resolve a
cell, classify the text, detect a category, or correlate anything: those are separate
modules reached through the event it publishes. Keeping ingestion this small is what makes
replay safe — re-running it can only re-derive the same row and the same event.

Order of operations, never reordered:
    1. validate the envelope (timezone-aware, not absurdly in the future)
    2. normalise the MSISDN and pseudonymise it immediately
    3. normalise the text for a fingerprint; measure and clip it for storage
    4. resolve idempotency, then dedupe
    5. write the row, refresh the subscriber, publish the event, record the decision —
       all in the caller's transaction, which is the point of the outbox

The complaint text is **untrusted data** throughout. It is hashed, measured and stored.
Nothing in this file branches on its content, searches it for keywords, or passes it to
anything that could treat it as an instruction.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from tazama.core.clock import Clock, ensure_utc
from tazama.core.errors import IdempotencyConflict, ValidationFailed
from tazama.core.ids import deterministic_id, get_correlation_id
from tazama.core.logging import get_logger
from tazama.core.policy import Policies, get_policies
from tazama.core.security import Pseudonymiser
from tazama.domain.events import AggregateType, ComplaintReceived
from tazama.domain.state_machine import CaseState
from tazama.domain.value_objects import Msisdn
from tazama.modules.ingestion.models import (
    REASON_CLOCK_SKEW,
    REASON_EMPTY_TEXT,
    REASON_LATE_EVENT,
    REASON_MISSING_MSISDN,
    REASON_TRUNCATED_TEXT,
    REASON_UNPARSEABLE_MSISDN,
    UNDETERMINED_LANGUAGE,
    CareCallRecord,
    CareCallResult,
    ComplaintRecord,
    InboundCareCall,
    InboundComplaint,
    IngestEvidence,
    IngestionConfig,
    IngestResult,
    StoredComplaint,
    body_hash,
    care_call_body_hash,
    care_call_dedupe_key,
    clip_field,
    clip_text,
    complaint_dedupe_key,
    idempotency_dedupe_key,
    is_empty_text,
    msisdn_input_shape,
    text_fingerprint,
    window_bucket,
)
from tazama.modules.ingestion.ports import (
    CareCallRepository,
    ComplaintRepository,
    DecisionLog,
    EventPublisher,
    SubscriberRepository,
)

_log = get_logger(__name__)

ACTION_INGEST_COMPLAINT = "ingest_complaint"
ACTION_INGEST_CARE_CALL = "ingest_care_call"

OUTCOME_STORED = "stored"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_IDEMPOTENT_REPLAY = "idempotent_replay"
OUTCOME_CONFLICT = "idempotency_conflict"

# Column widths from the schema. Clipping here keeps a 120-character landmark from
# becoming a 500 on a care agent's screen.
_TEXT_FIELD = 120
_CODE_FIELD = 32
_ID_FIELD = 40


class IngestionService:
    """Stores inbound complaints and care calls. Nothing else."""

    def __init__(
        self,
        *,
        complaints: ComplaintRepository,
        care_calls: CareCallRepository,
        subscribers: SubscriberRepository,
        publisher: EventPublisher,
        decisions: DecisionLog,
        pseudonymiser: Pseudonymiser,
        clock: Clock,
        config: IngestionConfig | None = None,
        policies: Policies | None = None,
    ) -> None:
        self._complaints = complaints
        self._care_calls = care_calls
        self._subscribers = subscribers
        self._publisher = publisher
        self._decisions = decisions
        self._pseudonymiser = pseudonymiser
        self._clock = clock
        self._config = config or IngestionConfig()
        self._policies = policies

    # --------------------------------------------------------------- complaints

    def ingest(self, inbound: InboundComplaint) -> IngestResult:
        """Store one complaint, or recognise it as one we already have."""
        received_at = self._validated_instant(inbound.received_at, "received_at")
        source_ts = (
            self._validated_source_ts(inbound.source_ts) if inbound.source_ts is not None else None
        )
        is_late, is_skewed = self._ordering_flags(received_at, source_ts)

        subscriber_id, msisdn_format, identity_reasons = self._identify(inbound.raw_msisdn)
        text, truncated = clip_text(inbound.text, self._config.max_text_chars)

        reasons: list[str] = list(identity_reasons)
        if is_empty_text(inbound.text):
            reasons.append(REASON_EMPTY_TEXT)
        if truncated:
            reasons.append(REASON_TRUNCATED_TEXT)
        if is_late:
            reasons.append(REASON_LATE_EVENT)
        if is_skewed:
            reasons.append(REASON_CLOCK_SKEW)
        # Only an identity or content problem needs a person; late and skewed are recorded
        # facts that the pipeline handles on its own.
        needs_human = bool(
            set(reasons) & {REASON_UNPARSEABLE_MSISDN, REASON_MISSING_MSISDN, REASON_EMPTY_TEXT},
        )

        bucket = window_bucket(received_at, self._config.dedupe_window_minutes)
        computed_key = complaint_dedupe_key(
            subscriber_id=subscriber_id,
            channel=str(inbound.channel),
            fingerprint=text_fingerprint(inbound.text),
            bucket=bucket,
        )
        explicit = (inbound.idempotency_key or "").strip() or None
        dedupe_key = idempotency_dedupe_key(explicit) if explicit else computed_key

        incoming_hash = body_hash(
            subscriber_id=subscriber_id,
            channel=str(inbound.channel),
            text=text,
            received_at=received_at,
            stated_location=inbound.stated_location,
            region=inbound.region,
            cell_id=inbound.cell_id,
        )

        base_inputs: dict[str, Any] = {
            "channel": str(inbound.channel),
            "subscriber_id": subscriber_id,
            "msisdn_format": msisdn_format,
            "received_at": received_at.isoformat(),
            "source_ts": source_ts.isoformat() if source_ts else None,
            "dedupe_window_minutes": self._config.dedupe_window_minutes,
            "dedupe_bucket": bucket.isoformat(),
            "dedupe_key": dedupe_key,
            "idempotency_key_used": explicit is not None,
            "text_length": len(inbound.text),
            "text_truncated": truncated,
            "is_late": is_late,
            "is_skewed": is_skewed,
            "reasons": list(reasons),
        }

        existing = (
            self._complaints.find_by_idempotency_key(explicit)
            if explicit
            else self._complaints.find_by_dedupe_key(dedupe_key)
        )
        if existing is not None:
            return self._existing_complaint_result(
                existing=existing,
                explicit=explicit,
                incoming_hash=incoming_hash,
                dedupe_key=dedupe_key,
                subscriber_id=subscriber_id,
                reasons=tuple(reasons),
                needs_human=needs_human,
                is_late=is_late,
                is_skewed=is_skewed,
                truncated=truncated,
                original_length=len(inbound.text),
                inputs=base_inputs,
            )

        now = self._clock.now()
        complaint_id = deterministic_id("CMP", dedupe_key)
        record = ComplaintRecord(
            id=complaint_id,
            received_at=received_at,
            source_ts=source_ts,
            ingested_at=now,
            channel=str(inbound.channel)[:_CODE_FIELD],
            language=str(inbound.language) if inbound.language else UNDETERMINED_LANGUAGE,
            raw_text=text,
            subscriber_id=subscriber_id,
            stated_location=clip_field(inbound.stated_location, _TEXT_FIELD),
            region=clip_field(inbound.region, _CODE_FIELD),
            cell_id=clip_field(inbound.cell_id, _ID_FIELD),
            status=str(CaseState.RECEIVED),
            dedupe_key=dedupe_key,
            external_ref=clip_field(inbound.external_ref, _ID_FIELD),
        )
        self._complaints.insert(record)

        if subscriber_id is not None:
            # Out-of-order safe: the adapter refuses to move `last_seen_at` backwards.
            self._subscribers.upsert_seen(
                subscriber_id=subscriber_id,
                seen_at=received_at,
                region=record.region,
                cell_id=record.cell_id,
                msisdn_format=msisdn_format,
            )

        published = self._publisher.publish(
            ComplaintReceived(
                aggregate_type=AggregateType.COMPLAINT,
                aggregate_id=complaint_id,
                occurred_at=now,
                dedupe_key=dedupe_key,
                correlation_id=get_correlation_id(),
                payload={
                    # Pseudonymised identifiers and metadata only. The text stays in the
                    # row; a consumer that needs it reads the complaint by id, behind the
                    # prompt guard.
                    "complaint_id": complaint_id,
                    "subscriber_id": subscriber_id,
                    "channel": str(inbound.channel),
                    "language": record.language,
                    "region": record.region,
                    "cell_id": record.cell_id,
                    "stated_location": record.stated_location,
                    "received_at": received_at.isoformat(),
                    "source_ts": source_ts.isoformat() if source_ts else None,
                    "is_late": is_late,
                    "is_skewed": is_skewed,
                    "text_length": len(inbound.text),
                    "text_truncated": truncated,
                    "needs_human": needs_human,
                    "reasons": list(reasons),
                },
            ),
        )

        self._decisions.record(
            action=ACTION_INGEST_COMPLAINT,
            entity_type="complaint",
            entity_id=complaint_id,
            outcome=OUTCOME_STORED,
            inputs=base_inputs,
            evidence=(
                IngestEvidence(kind="complaint", ref=complaint_id, note="row written"),
                IngestEvidence(kind="complaint", ref=dedupe_key, note="dedupe key"),
            ),
            rule_version=self._rule_version(),
        )
        _log.info(
            "complaint_ingested",
            extra={
                "complaint_id": complaint_id,
                "channel": str(inbound.channel),
                "needs_human": needs_human,
            },
        )
        return IngestResult(
            complaint_id=complaint_id,
            dedupe_key=dedupe_key,
            subscriber_id=subscriber_id,
            duplicate=False,
            published=published,
            needs_human=needs_human,
            is_late=is_late,
            is_skewed=is_skewed,
            text_truncated=truncated,
            original_text_length=len(inbound.text),
            idempotency_key_used=explicit is not None,
            reasons=tuple(reasons),
        )

    # --------------------------------------------------------------- care calls

    def ingest_care_call(self, inbound: InboundCareCall) -> CareCallResult:
        """Store one care call. Same pseudonymisation and dedupe discipline."""
        started_at = self._validated_instant(inbound.started_at, "started_at")
        source_ts = (
            self._validated_source_ts(inbound.source_ts) if inbound.source_ts is not None else None
        )
        is_late, is_skewed = self._ordering_flags(started_at, source_ts)
        if inbound.duration_s < 0:
            raise ValidationFailed("duration_s cannot be negative.")
        if not inbound.reason_code.strip():
            raise ValidationFailed("reason_code is required on a care call.")

        subscriber_id, msisdn_format, identity_reasons = self._identify(inbound.raw_msisdn)
        reasons = list(identity_reasons)
        if is_late:
            reasons.append(REASON_LATE_EVENT)
        if is_skewed:
            reasons.append(REASON_CLOCK_SKEW)
        needs_human = bool(
            set(reasons) & {REASON_UNPARSEABLE_MSISDN, REASON_MISSING_MSISDN},
        )

        reason_code = inbound.reason_code.strip()[:_CODE_FIELD]
        bucket = window_bucket(started_at, self._config.dedupe_window_minutes)
        computed_key = care_call_dedupe_key(
            subscriber_id=subscriber_id,
            reason_code=reason_code,
            bucket=bucket,
        )
        explicit = (inbound.idempotency_key or "").strip() or None
        dedupe_key = idempotency_dedupe_key(explicit) if explicit else computed_key

        incoming_hash = care_call_body_hash(
            subscriber_id=subscriber_id,
            reason_code=reason_code,
            reason_text=inbound.reason_text,
            started_at=started_at,
            duration_s=inbound.duration_s,
            region=inbound.region,
            cell_id=inbound.cell_id,
        )

        inputs: dict[str, Any] = {
            "channel": str(inbound.channel),
            "subscriber_id": subscriber_id,
            "msisdn_format": msisdn_format,
            "started_at": started_at.isoformat(),
            "source_ts": source_ts.isoformat() if source_ts else None,
            "reason_code": reason_code,
            "duration_s": inbound.duration_s,
            "dedupe_window_minutes": self._config.dedupe_window_minutes,
            "dedupe_bucket": bucket.isoformat(),
            "dedupe_key": dedupe_key,
            "idempotency_key_used": explicit is not None,
            "is_late": is_late,
            "is_skewed": is_skewed,
            "reasons": list(reasons),
        }

        existing = (
            self._care_calls.find_by_idempotency_key(explicit)
            if explicit
            else self._care_calls.find_by_dedupe_key(dedupe_key)
        )
        if existing is not None:
            if explicit is not None and existing.body_hash != incoming_hash:
                self._refuse(
                    action=ACTION_INGEST_CARE_CALL,
                    entity_type="care_call",
                    entity_id=existing.id,
                    inputs=inputs,
                )
                raise IdempotencyConflict(
                    "This idempotency key was already used for a different care call.",
                )
            self._decisions.record(
                action=ACTION_INGEST_CARE_CALL,
                entity_type="care_call",
                entity_id=existing.id,
                outcome=(OUTCOME_IDEMPOTENT_REPLAY if explicit is not None else OUTCOME_DUPLICATE),
                inputs=inputs,
                evidence=(
                    IngestEvidence(
                        kind="care_call",
                        ref=existing.id,
                        note="already stored under this key",
                    ),
                ),
                rule_version=self._rule_version(),
            )
            return CareCallResult(
                care_call_id=existing.id,
                dedupe_key=dedupe_key,
                subscriber_id=subscriber_id,
                duplicate=True,
                published=False,
                needs_human=needs_human,
                is_late=is_late,
                is_skewed=is_skewed,
                idempotency_key_used=explicit is not None,
                reasons=tuple(reasons),
            )

        call_id = deterministic_id("CALL", dedupe_key)
        record = CareCallRecord(
            id=call_id,
            started_at=started_at,
            duration_s=inbound.duration_s,
            subscriber_id=subscriber_id,
            reason_code=reason_code,
            reason_text=clip_field(inbound.reason_text, _TEXT_FIELD),
            channel=str(inbound.channel)[:_CODE_FIELD],
            agent_ref=clip_field(inbound.agent_ref, _CODE_FIELD),
            region=clip_field(inbound.region, _CODE_FIELD),
            cell_id=clip_field(inbound.cell_id, _ID_FIELD),
            is_repeat=inbound.is_repeat,
        )
        self._care_calls.insert(record)

        if subscriber_id is not None:
            self._subscribers.upsert_seen(
                subscriber_id=subscriber_id,
                seen_at=started_at,
                region=record.region,
                cell_id=record.cell_id,
                msisdn_format=msisdn_format,
            )

        # A care call is a complaint-shaped signal for the same pipeline, so it enters
        # through the same event rather than a second vocabulary.
        published = self._publisher.publish(
            ComplaintReceived(
                aggregate_type=AggregateType.COMPLAINT,
                aggregate_id=call_id,
                occurred_at=self._clock.now(),
                dedupe_key=dedupe_key,
                correlation_id=get_correlation_id(),
                payload={
                    "care_call_id": call_id,
                    "subscriber_id": subscriber_id,
                    "channel": str(inbound.channel),
                    "reason_code": reason_code,
                    "region": record.region,
                    "cell_id": record.cell_id,
                    "received_at": started_at.isoformat(),
                    "source_ts": source_ts.isoformat() if source_ts else None,
                    "duration_s": inbound.duration_s,
                    "is_care_call": True,
                    "is_late": is_late,
                    "is_skewed": is_skewed,
                    "needs_human": needs_human,
                    "reasons": list(reasons),
                },
            ),
        )

        self._decisions.record(
            action=ACTION_INGEST_CARE_CALL,
            entity_type="care_call",
            entity_id=call_id,
            outcome=OUTCOME_STORED,
            inputs=inputs,
            evidence=(IngestEvidence(kind="care_call", ref=call_id, note="row written"),),
            rule_version=self._rule_version(),
        )
        return CareCallResult(
            care_call_id=call_id,
            dedupe_key=dedupe_key,
            subscriber_id=subscriber_id,
            duplicate=False,
            published=published,
            needs_human=needs_human,
            is_late=is_late,
            is_skewed=is_skewed,
            idempotency_key_used=explicit is not None,
            reasons=tuple(reasons),
        )

    # ----------------------------------------------------------------- helpers

    def _existing_complaint_result(
        self,
        *,
        existing: StoredComplaint,
        explicit: str | None,
        incoming_hash: str,
        dedupe_key: str,
        subscriber_id: str | None,
        reasons: tuple[str, ...],
        needs_human: bool,
        is_late: bool,
        is_skewed: bool,
        truncated: bool,
        original_length: int,
        inputs: dict[str, Any],
    ) -> IngestResult:
        """Handle "we already have this": a replay, a duplicate, or a conflict."""
        if explicit is not None and existing.body_hash != incoming_hash:
            self._refuse(
                action=ACTION_INGEST_COMPLAINT,
                entity_type="complaint",
                entity_id=existing.id,
                inputs=inputs,
            )
            raise IdempotencyConflict(
                "This idempotency key was already used for a different complaint.",
            )
        self._decisions.record(
            action=ACTION_INGEST_COMPLAINT,
            entity_type="complaint",
            entity_id=existing.id,
            outcome=OUTCOME_IDEMPOTENT_REPLAY if explicit is not None else OUTCOME_DUPLICATE,
            inputs=inputs,
            evidence=(
                IngestEvidence(
                    kind="complaint",
                    ref=existing.id,
                    note="already stored under this key",
                ),
            ),
            rule_version=self._rule_version(),
        )
        return IngestResult(
            complaint_id=existing.id,
            dedupe_key=dedupe_key,
            subscriber_id=subscriber_id,
            duplicate=True,
            published=False,
            needs_human=needs_human,
            is_late=is_late,
            is_skewed=is_skewed,
            text_truncated=truncated,
            original_text_length=original_length,
            idempotency_key_used=explicit is not None,
            reasons=reasons,
        )

    def _refuse(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        inputs: dict[str, Any],
    ) -> None:
        self._decisions.record(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            outcome=OUTCOME_CONFLICT,
            inputs=inputs,
            evidence=(
                IngestEvidence(
                    kind=entity_type,
                    ref=entity_id,
                    note="idempotency key reused with a different body",
                ),
            ),
            rule_version=self._rule_version(),
        )

    def _identify(self, raw_msisdn: str | None) -> tuple[str | None, str | None, tuple[str, ...]]:
        """Normalise then immediately pseudonymise. The raw value stops here.

        An unparseable number is data, not an error: the complaint is still stored, with no
        subscriber, and flagged for a human. Guessing at a landline or a foreign number
        would attach a real customer's history to the wrong person.
        """
        if raw_msisdn is None or not raw_msisdn.strip():
            return None, None, (REASON_MISSING_MSISDN,)
        shape = msisdn_input_shape(raw_msisdn)
        normalised = Msisdn.try_parse(raw_msisdn)
        if normalised is None:
            # Deliberately logs the shape and nothing else — the number is never logged.
            _log.warning("msisdn_unparseable", extra={"msisdn_format": shape})
            return None, shape, (REASON_UNPARSEABLE_MSISDN,)
        return self._pseudonymiser.token(normalised.value), shape, ()

    def _validated_instant(self, value: datetime, field_name: str) -> datetime:
        """Reject naive datetimes and impossible futures before anything else happens."""
        if value.tzinfo is None:
            raise ValidationFailed(
                f"{field_name} must be timezone-aware: TAZAMA stores UTC and will not "
                "guess a timezone.",
            )
        moment = ensure_utc(value)
        limit = timedelta(hours=self._config.max_future_skew_hours)
        if moment - self._clock.now() > limit:
            raise ValidationFailed(
                f"{field_name} is more than {self._config.max_future_skew_hours}h in the "
                "future, which is clock skew at the source rather than a real event.",
            )
        return moment

    def _validated_source_ts(self, value: datetime) -> datetime:
        """`source_ts` is validated for shape but never bounded: it may be arbitrarily old."""
        if value.tzinfo is None:
            raise ValidationFailed(
                "source_ts must be timezone-aware: TAZAMA stores UTC and will not guess a "
                "timezone.",
            )
        return ensure_utc(value)

    def _ordering_flags(
        self, received_at: datetime, source_ts: datetime | None
    ) -> tuple[bool, bool]:
        """Classify the gap between the source's clock and ours, and change nothing.

        `source_ts` never drives the dedupe window: a backlog flushing an hour of SMS would
        otherwise land every message in one bucket and collapse a real storm into one row.
        """
        if source_ts is None:
            return False, False
        late_by = received_at - source_ts
        is_late = late_by > timedelta(minutes=self._config.late_threshold_minutes)
        is_skewed = source_ts > received_at
        return is_late, is_skewed

    def _rule_version(self) -> str:
        if self._policies is None:
            self._policies = get_policies()
        return self._policies.rule_version
