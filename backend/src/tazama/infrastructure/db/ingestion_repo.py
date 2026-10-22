"""SQLAlchemy adapters for the ingestion ports.

All three take an optional `Session` so they join the caller's transaction: the complaint
row, the subscriber refresh and the outbox event must commit together or not at all (ADR
0003). Without a session each call opens its own transaction, which is the right choice for
a one-off API write that must be recorded even if later work fails.

Every statement is built from our own metadata with bound values. Nothing here
interpolates a string into SQL, nothing selects `*`, and every read is bounded.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tazama.core.clock import ensure_utc
from tazama.core.ids import deterministic_id
from tazama.core.logging import get_logger
from tazama.infrastructure.db.engine import get_engine, session_factory
from tazama.infrastructure.db.models import CareCall, Complaint, Subscriber
from tazama.modules.ingestion.models import (
    CareCallRecord,
    ComplaintRecord,
    StoredCareCall,
    StoredComplaint,
    SubscriberSnapshot,
    idempotency_dedupe_key,
)

_log = get_logger(__name__)

T = TypeVar("T")


class _SessionBound:
    """Shared plumbing: run on the caller's session, or open one per call."""

    __slots__ = ("_engine", "_session")

    def __init__(self, session: Session | None = None, *, engine: Engine | None = None) -> None:
        self._session = session
        # Resolved lazily: a caller that supplies a session must not need an engine at all.
        self._engine = engine

    def _read(self, work: Callable[[Session], T]) -> T:
        if self._session is not None:
            return work(self._session)
        with session_factory(self._engine or get_engine())() as session:
            return work(session)

    def _write(self, work: Callable[[Session], T]) -> T:
        if self._session is not None:
            return work(self._session)
        with session_factory(self._engine or get_engine())() as session, session.begin():
            return work(session)


class SqlComplaintRepository(_SessionBound):
    """Reads and writes `complaint`."""

    def insert(self, record: ComplaintRecord) -> str:
        def work(session: Session) -> str:
            session.execute(insert(Complaint).values(**record.to_row()))
            return record.id

        return self._write(work)

    def find_by_dedupe_key(self, dedupe_key: str) -> StoredComplaint | None:
        return self._read(lambda session: self._by_dedupe_key(session, dedupe_key))

    def find_by_idempotency_key(self, idempotency_key: str) -> StoredComplaint | None:
        """The idempotency key is namespaced into the same column; the rule lives in the
        module, so the adapter applies it rather than reimplementing it."""
        if not idempotency_key.strip():
            return None
        return self.find_by_dedupe_key(idempotency_dedupe_key(idempotency_key))

    @staticmethod
    def _by_dedupe_key(session: Session, dedupe_key: str) -> StoredComplaint | None:
        stmt = (
            select(
                Complaint.id,
                Complaint.subscriber_id,
                Complaint.channel,
                Complaint.raw_text,
                Complaint.received_at,
                Complaint.source_ts,
                Complaint.stated_location,
                Complaint.region,
                Complaint.cell_id,
                Complaint.dedupe_key,
            )
            .where(Complaint.dedupe_key == dedupe_key)
            # The key encodes its own time bucket, so one row is expected; ordering and the
            # limit keep a historical collision from making the result depend on row order.
            .order_by(Complaint.received_at.desc(), Complaint.id)
            .limit(1)
        )
        row = session.execute(stmt).first()
        if row is None:
            return None
        return StoredComplaint(
            id=row.id,
            subscriber_id=row.subscriber_id,
            channel=row.channel,
            raw_text=row.raw_text,
            received_at=_utc(row.received_at),
            source_ts=_utc_or_none(row.source_ts),
            stated_location=row.stated_location,
            region=row.region,
            cell_id=row.cell_id,
            dedupe_key=row.dedupe_key,
        )


class SqlCareCallRepository(_SessionBound):
    """Reads and writes `care_call`.

    `care_call` has no dedupe column, so the dedupe key is folded into the primary key by
    the module (`id = deterministic_id("CALL", dedupe_key)`). Looking a key up is therefore
    a primary-key read, and a duplicate insert collides rather than duplicating.
    """

    def insert(self, record: CareCallRecord) -> str:
        def work(session: Session) -> str:
            session.execute(insert(CareCall).values(**record.to_row()))
            return record.id

        return self._write(work)

    def find_by_dedupe_key(self, dedupe_key: str) -> StoredCareCall | None:
        return self._read(
            lambda session: self._by_id(session, deterministic_id("CALL", dedupe_key)),
        )

    def find_by_idempotency_key(self, idempotency_key: str) -> StoredCareCall | None:
        if not idempotency_key.strip():
            return None
        return self.find_by_dedupe_key(idempotency_dedupe_key(idempotency_key))

    @staticmethod
    def _by_id(session: Session, call_id: str) -> StoredCareCall | None:
        stmt = (
            select(
                CareCall.id,
                CareCall.subscriber_id,
                CareCall.reason_code,
                CareCall.reason_text,
                CareCall.started_at,
                CareCall.duration_s,
                CareCall.region,
                CareCall.cell_id,
            )
            .where(CareCall.id == call_id)
            .limit(1)
        )
        row = session.execute(stmt).first()
        if row is None:
            return None
        return StoredCareCall(
            id=row.id,
            subscriber_id=row.subscriber_id,
            reason_code=row.reason_code,
            reason_text=row.reason_text,
            started_at=_utc(row.started_at),
            duration_s=int(row.duration_s or 0),
            region=row.region,
            cell_id=row.cell_id,
        )


class SqlSubscriberRepository(_SessionBound):
    """Reads and refreshes `subscriber`. There is no raw-identifier path here by design."""

    def get(self, subscriber_id: str) -> SubscriberSnapshot | None:
        def work(session: Session) -> SubscriberSnapshot | None:
            stmt = (
                select(
                    Subscriber.id,
                    Subscriber.region,
                    Subscriber.home_cell_id,
                    Subscriber.last_cell_id,
                    Subscriber.last_seen_at,
                    Subscriber.segment,
                    Subscriber.is_roaming,
                    Subscriber.opt_out,
                )
                .where(Subscriber.id == subscriber_id)
                .limit(1)
            )
            row = session.execute(stmt).first()
            if row is None:
                return None
            return SubscriberSnapshot(
                id=row.id,
                region=row.region,
                home_cell_id=row.home_cell_id,
                last_cell_id=row.last_cell_id,
                last_seen_at=_utc_or_none(row.last_seen_at),
                segment=row.segment or "consumer",
                is_roaming=bool(row.is_roaming),
                opt_out=bool(row.opt_out),
            )

        return self._read(work)

    def upsert_seen(
        self,
        *,
        subscriber_id: str,
        seen_at: datetime,
        region: str | None = None,
        cell_id: str | None = None,
        msisdn_format: str | None = None,
    ) -> None:
        """Insert the subscriber, or move them forward in time — never backwards.

        Late and replayed events are normal here (PRD section 15), and a day-old complaint
        arriving after today's must not overwrite today's serving cell.
        """
        moment = ensure_utc(seen_at)

        def work(session: Session) -> None:
            existing = session.execute(
                select(Subscriber.id, Subscriber.last_seen_at)
                .where(Subscriber.id == subscriber_id)
                .limit(1),
            ).first()
            if existing is None:
                session.execute(
                    insert(Subscriber).values(
                        id=subscriber_id,
                        # `region` is NOT NULL; "unknown" is honest and queryable, and
                        # enrichment replaces it once the cell is resolved.
                        region=region or "unknown",
                        home_cell_id=cell_id,
                        last_cell_id=cell_id,
                        last_seen_at=moment,
                        segment="consumer",
                        is_enterprise=False,
                        opt_out=False,
                        is_roaming=False,
                        msisdn_format=msisdn_format,
                        created_at=moment,
                    ),
                )
                return
            previous = _utc_or_none(existing.last_seen_at)
            if previous is not None and previous > moment:
                return
            values: dict[str, Any] = {"last_seen_at": moment}
            if cell_id:
                values["last_cell_id"] = cell_id
            if region:
                values["region"] = region
            if msisdn_format:
                values["msisdn_format"] = msisdn_format
            session.execute(
                update(Subscriber).where(Subscriber.id == subscriber_id).values(**values),
            )

        self._write(work)


# ------------------------------------------------------------------------ helpers


def _utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes for some drivers; the column is always UTC."""
    return ensure_utc(value) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _utc_or_none(value: datetime | None) -> datetime | None:
    return None if value is None else _utc(value)
