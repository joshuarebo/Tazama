"""SQLAlchemy adapters for the notification ports.

All SQL is built from our own mapped columns with bound parameters — nothing is
interpolated. The counting queries exist here rather than in the service because the rate
limits must hold across processes and restarts: in-process state would reset on a deploy
and let a storm through.

A session can be injected so a proposal commits in the same transaction as the outbox event
it causes (the outbox pattern); without one, each call is its own transaction.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tazama.infrastructure.db.engine import get_engine, session_factory
from tazama.infrastructure.db.models import Incident, IncidentLink, Notification, Subscriber
from tazama.modules.notification.models import (
    AudienceMember,
    NotificationProposal,
    NotificationStatus,
    SuppressionReason,
)

#: Statuses that count towards a rate limit. A suppressed or rejected proposal never
#: reached a customer, so it must not consume anyone's daily allowance.
COUNTED_STATUSES: tuple[str, ...] = (
    str(NotificationStatus.PROPOSED),
    str(NotificationStatus.APPROVED),
    str(NotificationStatus.SENT),
)

MAX_AUDIENCE = 5_000


class SqlNotificationRepository:
    """Reads and writes `notification`."""

    __slots__ = ("_engine", "_session")

    def __init__(self, engine: Engine | None = None, *, session: Session | None = None) -> None:
        self._engine = engine or get_engine()
        self._session = session

    # ------------------------------------------------------------- writes

    def save(self, proposal: NotificationProposal) -> NotificationProposal:
        """Insert or update by primary key: a replayed event must not create a second row.

        Written as select-then-write rather than a dialect `ON CONFLICT`/`MERGE`, because
        the same statement has to run on SQLite (tests) and Oracle (deployment), and a
        dialect-specific upsert in a repository is how those two drift apart.
        """
        row = proposal.to_row()
        exists = (
            self._count(
                select(func.count())
                .select_from(Notification)
                .where(Notification.id == proposal.id),
            )
            > 0
        )
        if exists:
            updatable = {key: value for key, value in row.items() if key != "id"}
            self._execute(
                update(Notification).where(Notification.id == proposal.id).values(**updatable),
            )
        else:
            self._execute(insert(Notification).values(**row))
        return proposal

    # -------------------------------------------------------------- reads

    def get(self, notification_id: str) -> NotificationProposal | None:
        row = self._scalar_one(
            select(Notification).where(Notification.id == notification_id),
        )
        return None if row is None else _to_proposal(row)

    def for_incident(self, incident_id: str) -> tuple[NotificationProposal, ...]:
        rows = self._scalars(
            select(Notification)
            .where(Notification.incident_id == incident_id)
            .order_by(Notification.proposed_at, Notification.id),
        )
        return tuple(_to_proposal(row) for row in rows)

    def count_for_subscriber_in_incident(self, subscriber_id: str, incident_id: str) -> int:
        return self._count(
            select(func.count())
            .select_from(Notification)
            .where(Notification.subscriber_id == subscriber_id)
            .where(Notification.incident_id == incident_id)
            .where(Notification.status.in_(COUNTED_STATUSES)),
        )

    def count_for_subscriber_since(self, subscriber_id: str, since: datetime) -> int:
        return self._count(
            select(func.count())
            .select_from(Notification)
            .where(Notification.subscriber_id == subscriber_id)
            # Time-bounded and index-friendly: `proposed_at` is indexed.
            .where(Notification.proposed_at >= since)
            .where(Notification.status.in_(COUNTED_STATUSES)),
        )

    def find_recent_duplicate(
        self,
        *,
        subscriber_id: str,
        template_id: str,
        incident_id: str,
        since: datetime,
    ) -> NotificationProposal | None:
        row = self._scalar_one(
            select(Notification)
            .where(Notification.subscriber_id == subscriber_id)
            .where(Notification.template_id == template_id)
            .where(Notification.incident_id == incident_id)
            .where(Notification.proposed_at >= since)
            .where(Notification.status.in_(COUNTED_STATUSES))
            .order_by(Notification.proposed_at.desc())
            .limit(1),
        )
        return None if row is None else _to_proposal(row)

    def count_proposed_since(self, since: datetime) -> int:
        """System-wide proposal count for the storm guard."""
        return self._count(
            select(func.count())
            .select_from(Notification)
            .where(Notification.proposed_at >= since)
            .where(Notification.status.in_(COUNTED_STATUSES)),
        )

    # ------------------------------------------------------------ plumbing

    def _execute(self, statement: Any) -> None:
        if self._session is not None:
            self._session.execute(statement)
            return
        with session_factory(self._engine)() as session, session.begin():
            session.execute(statement)

    def _scalar_one(self, statement: Any) -> Notification | None:
        if self._session is not None:
            return self._session.execute(statement).scalars().first()
        with session_factory(self._engine)() as session:
            return session.execute(statement).scalars().first()

    def _scalars(self, statement: Any) -> list[Notification]:
        if self._session is not None:
            return list(self._session.execute(statement).scalars().all())
        with session_factory(self._engine)() as session:
            return list(session.execute(statement).scalars().all())

    def _count(self, statement: Any) -> int:
        if self._session is not None:
            return int(self._session.execute(statement).scalar_one())
        with session_factory(self._engine)() as session:
            return int(session.execute(statement).scalar_one())


class SqlSubscriberAudience:
    """Reads `subscriber` for the notifiable audience of an incident.

    The audience is pseudonym tokens by construction: `subscriber.id` *is* the token and
    there is no MSISDN column anywhere in the schema (ADR 0008).

    `for_incident` resolves through the incident's affected cells. Phase 2 has no
    incident→cell table of its own in this adapter, so the cells are passed in by the
    composition root; absent them, it falls back to the region recorded on the incident.
    """

    __slots__ = ("_engine", "_session")

    def __init__(self, engine: Engine | None = None, *, session: Session | None = None) -> None:
        self._engine = engine or get_engine()
        self._session = session

    def for_incident(self, incident_id: str, *, limit: int = 1000) -> tuple[AudienceMember, ...]:
        bounded = max(1, min(limit, MAX_AUDIENCE))
        cells = select(IncidentLink.entity_id).where(
            IncidentLink.incident_id == incident_id,
            IncidentLink.entity_type == "cell",
        )
        statement = (
            select(Subscriber)
            .where(Subscriber.last_cell_id.in_(cells) | Subscriber.home_cell_id.in_(cells))
            .order_by(Subscriber.id)
            .limit(bounded)
        )
        rows = self._scalars(statement)
        if not rows:
            # No cell links recorded: fall back to the incident's region, which is always
            # set. Broader, and the per-subscriber gates still apply to every member.
            region = self._scalar(
                select(Incident.region).where(Incident.id == incident_id),
            )
            if region is None:
                return ()
            rows = self._scalars(
                select(Subscriber)
                .where(Subscriber.region == region)
                .order_by(Subscriber.id)
                .limit(bounded),
            )
        return tuple(_to_member(row) for row in rows)

    def get(self, subscriber_id: str) -> AudienceMember | None:
        row = self._scalar_subscriber(
            select(Subscriber).where(Subscriber.id == subscriber_id),
        )
        return None if row is None else _to_member(row)

    def is_opted_out(self, subscriber_id: str) -> bool:
        """Unknown subscriber reads as opted out: fail closed on a customer-facing send."""
        value = self._scalar(
            select(Subscriber.opt_out).where(Subscriber.id == subscriber_id),
        )
        return True if value is None else bool(value)

    def _scalars(self, statement: Any) -> list[Subscriber]:
        if self._session is not None:
            return list(self._session.execute(statement).scalars().all())
        with session_factory(self._engine)() as session:
            return list(session.execute(statement).scalars().all())

    def _scalar_subscriber(self, statement: Any) -> Subscriber | None:
        if self._session is not None:
            return self._session.execute(statement).scalars().first()
        with session_factory(self._engine)() as session:
            return session.execute(statement).scalars().first()

    def _scalar(self, statement: Any) -> Any:
        if self._session is not None:
            return self._session.execute(statement).scalars().first()
        with session_factory(self._engine)() as session:
            return session.execute(statement).scalars().first()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _to_proposal(row: Notification) -> NotificationProposal:
    slots: Mapping[str, str] = {str(key): str(value) for key, value in (row.slots or {}).items()}
    proposed_at = _aware(row.proposed_at)
    assert proposed_at is not None  # NOT NULL in the schema
    return NotificationProposal(
        id=row.id,
        incident_id=row.incident_id or "",
        subscriber_id=row.subscriber_id or "",
        template_id=row.template_id,
        template_version=row.template_version or "",
        language=row.language,
        channel=row.channel,
        slots=slots,
        rendered_text=row.rendered_text or "",
        status=NotificationStatus(row.status),
        proposed_at=proposed_at,
        # `proposed_by` is not a column: the proposer lives on the audit row, which is the
        # append-only record approval checks are reconciled against. Reconstructing it as
        # the approver would be wrong, so it is left empty on a round-trip.
        proposed_by="",
        dedupe_key=row.dedupe_key or "",
        gateway_mode=row.gateway_mode,
        approved_by=row.approved_by,
        approved_at=_aware(row.approved_at),
        # `rejected_reason` has no column either: the reason an approver gave is part of
        # the append-only decision record, not of the message's own row.
        sent_at=_aware(row.sent_at),
        suppressed_reason=(
            SuppressionReason(row.suppressed_reason)
            if row.suppressed_reason in {str(r) for r in SuppressionReason}
            else None
        ),
    )


def _to_member(row: Subscriber) -> AudienceMember:
    return AudienceMember(
        subscriber_id=row.id,
        # Language preference is not a column in Phase 2; English is the safe default and
        # the service falls back to it whenever a template lacks the member's language.
        language="en",
        opt_out=bool(row.opt_out),
        region=row.region,
        cell_id=row.last_cell_id or row.home_cell_id,
        sla_tier=row.sla_tier,
    )
