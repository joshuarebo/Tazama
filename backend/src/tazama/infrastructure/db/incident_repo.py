"""SQLAlchemy adapters for the incident store, its links, and the correlation bridge.

Two things here are worth reading before changing anything.

**Optimistic locking is done in SQL, not in Python.** `update` issues
`UPDATE incident SET ... WHERE id = :id AND version = :expected`, and a zero row count is
a `Conflict`. Checking the version with a prior `SELECT` would leave the race wide open.

**The incident's event window.** The `incident` table has `started_at` but no
"degradation ended at" column, and `resolved_at` means something else (when the fault was
verified as fixed). So the persisted event window is read back as
`[started_at, resolved_at or started_at + INCIDENT_WINDOW_SPAN)`: an incident's
observable window is capped at `INCIDENT_WINDOW_SPAN`, and a fault on the same element
after that is genuinely a new incident rather than the same one dragging on. This is
recorded as an assumption in the phase report; a `window_end` column would make it exact.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import Row, Select, delete, func, insert, select, update
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tazama.core.clock import Clock
from tazama.core.errors import Conflict
from tazama.core.logging import get_logger
from tazama.domain.value_objects import IncidentCause, IncidentStatus, Priority, TimeWindow
from tazama.infrastructure.db.engine import get_engine
from tazama.infrastructure.db.models import Incident as IncidentRow
from tazama.infrastructure.db.models import IncidentLink as IncidentLinkRow
from tazama.modules.correlation.models import IncidentCandidate as CorrelationCandidate
from tazama.modules.correlation.models import IncidentRef
from tazama.modules.incidents.models import (
    Incident,
    IncidentCandidate,
    IncidentLink,
    SimilarityQuery,
)
from tazama.modules.incidents.service import IncidentService

_log = get_logger(__name__)

#: How long an incident's event window may run before a later fault on the same element
#: counts as a new incident. See the module docstring.
INCIDENT_WINDOW_SPAN = timedelta(hours=6)

#: Columns read for an incident. Never `SELECT *`: the table grows and the DTO should not.
_INCIDENT_COLUMNS = (
    IncidentRow.id,
    IncidentRow.title,
    IncidentRow.status,
    IncidentRow.priority,
    IncidentRow.cause,
    IncidentRow.cause_confidence,
    IncidentRow.region,
    IncidentRow.primary_node_id,
    IncidentRow.primary_node_kind,
    IncidentRow.detected_at,
    IncidentRow.started_at,
    IncidentRow.resolved_at,
    IncidentRow.closed_at,
    IncidentRow.affected_cells,
    IncidentRow.affected_sites,
    IncidentRow.affected_subs,
    IncidentRow.enterprise_accounts,
    IncidentRow.complaint_count,
    IncidentRow.sla_risk,
    IncidentRow.summary,
    IncidentRow.suppressed_reason,
    IncidentRow.detector,
    IncidentRow.rule_version,
    IncidentRow.created_at,
    IncidentRow.updated_at,
    IncidentRow.version,
)


class _SqlWriteAdapter:
    """Shared plumbing. A session when the caller has one, a connection otherwise."""

    __slots__ = ("_engine", "_session")

    def __init__(self, session: Session | None = None, *, engine: Engine | None = None) -> None:
        self._session = session
        self._engine = engine

    def _rows(self, statement: Select[Any]) -> Sequence[Row[Any]]:
        if self._session is not None:
            return self._session.execute(statement).all()
        with (self._engine or get_engine()).connect() as connection:
            return connection.execute(statement).all()

    def _scalar(self, statement: Select[Any]) -> Any:
        if self._session is not None:
            return self._session.execute(statement).scalar_one_or_none()
        with (self._engine or get_engine()).connect() as connection:
            return connection.execute(statement).scalar_one_or_none()

    def _write(self, statement: Any) -> int:
        """Execute a write and return the affected row count."""
        if self._session is not None:
            result = cast("CursorResult[Any]", self._session.execute(statement))
            self._session.flush()
            return int(result.rowcount or 0)
        with (self._engine or get_engine()).begin() as connection:
            return int(connection.execute(statement).rowcount or 0)


def _incident(row: Row[Any]) -> Incident:
    mapping = row._mapping
    started_at: datetime | None = mapping["started_at"]
    resolved_at: datetime | None = mapping["resolved_at"]
    detected_at: datetime = mapping["detected_at"]
    start = started_at or detected_at
    window_end = resolved_at or (start + INCIDENT_WINDOW_SPAN)
    return Incident(
        id=str(mapping["id"]),
        title=str(mapping["title"]),
        status=IncidentStatus(mapping["status"]),
        priority=Priority(mapping["priority"]),
        cause=IncidentCause(mapping["cause"] or IncidentCause.UNKNOWN.value),
        region=str(mapping["region"]),
        detected_at=detected_at,
        created_at=mapping["created_at"],
        updated_at=mapping["updated_at"],
        started_at=started_at,
        resolved_at=resolved_at,
        closed_at=mapping["closed_at"],
        window_end=window_end,
        cause_confidence=mapping["cause_confidence"],
        primary_node_id=mapping["primary_node_id"],
        primary_node_kind=mapping["primary_node_kind"],
        affected_cells=int(mapping["affected_cells"] or 0),
        affected_sites=int(mapping["affected_sites"] or 0),
        affected_subs=int(mapping["affected_subs"] or 0),
        enterprise_accounts=int(mapping["enterprise_accounts"] or 0),
        complaint_count=int(mapping["complaint_count"] or 0),
        sla_risk=mapping["sla_risk"],
        summary=mapping["summary"],
        suppressed_reason=mapping["suppressed_reason"],
        detector=str(mapping["detector"] or "correlation"),
        rule_version=mapping["rule_version"],
        version=int(mapping["version"] or 1),
    )


def _values(incident: Incident) -> dict[str, Any]:
    """The column values for an incident. An explicit allowlist, never mass assignment."""
    return {
        "title": incident.title[:120],
        "status": str(incident.status),
        "priority": str(incident.priority),
        "cause": str(incident.cause),
        "cause_confidence": incident.cause_confidence,
        "region": incident.region,
        "primary_node_id": incident.primary_node_id,
        "primary_node_kind": incident.primary_node_kind,
        "detected_at": incident.detected_at,
        "started_at": incident.started_at,
        "resolved_at": incident.resolved_at,
        "closed_at": incident.closed_at,
        "affected_cells": incident.affected_cells,
        "affected_sites": incident.affected_sites,
        "affected_subs": incident.affected_subs,
        "enterprise_accounts": incident.enterprise_accounts,
        "complaint_count": incident.complaint_count,
        "sla_risk": incident.sla_risk,
        "summary": incident.summary,
        "suppressed_reason": (
            incident.suppressed_reason[:120] if incident.suppressed_reason else None
        ),
        "detector": incident.detector,
        "rule_version": incident.rule_version,
        "updated_at": incident.updated_at,
        "version": incident.version,
    }


class SqlIncidentStore(_SqlWriteAdapter):
    """Reads and writes `incident`, with optimistic locking enforced in the UPDATE."""

    def get(self, incident_id: str) -> Incident | None:
        rows = self._rows(select(*_INCIDENT_COLUMNS).where(IncidentRow.id == incident_id))
        return _incident(rows[0]) if rows else None

    def insert(self, incident: Incident) -> Incident:
        """Insert, or return the row that beat us to this deterministic id."""
        statement = insert(IncidentRow).values(
            id=incident.id,
            created_at=incident.created_at,
            **_values(incident),
        )
        try:
            self._write(statement)
        except IntegrityError:
            # Two workers detected the same fault. The id is deterministic, so whoever
            # arrived first is right and this is not an error.
            if self._session is not None:
                self._session.rollback()
            existing = self.get(incident.id)
            if existing is None:  # pragma: no cover - the constraint says otherwise
                raise
            _log.debug("incident_insert_raced", extra={"incident_id": incident.id})
            return existing
        return incident

    def update(self, incident: Incident, *, expected_version: int) -> Incident:
        """Persist a change, refusing a stale write."""
        statement = (
            update(IncidentRow)
            .where(IncidentRow.id == incident.id, IncidentRow.version == expected_version)
            .values(**_values(incident))
        )
        if self._write(statement) == 0:
            current = self.get(incident.id)
            raise Conflict(
                f"Incident {incident.id} was modified concurrently: expected version "
                f"{expected_version}, found {current.version if current else 'none'}.",
            )
        return incident

    def find_by_nodes(
        self,
        node_ids: Sequence[str],
        window: TimeWindow,
    ) -> tuple[Incident, ...]:
        """Incidents rooted on any of these elements whose window overlaps `window`."""
        ids = list(dict.fromkeys(node_ids))
        if not ids:
            return ()
        start_floor = window.start - INCIDENT_WINDOW_SPAN
        statement = (
            select(*_INCIDENT_COLUMNS)
            .where(
                IncidentRow.primary_node_id.in_(ids),
                # Time-bounded on an indexed column: the window can only start inside
                # [window.start - span, window.end).
                IncidentRow.detected_at >= start_floor,
                func.coalesce(IncidentRow.started_at, IncidentRow.detected_at) < window.end,
                func.coalesce(IncidentRow.started_at, IncidentRow.detected_at) >= start_floor,
            )
            .order_by(IncidentRow.detected_at.desc(), IncidentRow.id)
            .limit(50)
        )
        return tuple(_incident(row) for row in self._rows(statement))

    def find_similar(self, query: SimilarityQuery) -> tuple[Incident, ...]:
        statement = select(*_INCIDENT_COLUMNS).where(
            IncidentRow.cause == str(query.cause),
            IncidentRow.status != str(IncidentStatus.SUPPRESSED),
        )
        if query.region:
            statement = statement.where(IncidentRow.region == query.region)
        if query.primary_node_kind:
            statement = statement.where(
                IncidentRow.primary_node_kind == query.primary_node_kind,
            )
        if query.exclude_id:
            statement = statement.where(IncidentRow.id != query.exclude_id)
        statement = statement.where(
            IncidentRow.affected_cells >= query.min_affected_cells,
        )
        if query.max_affected_cells is not None:
            statement = statement.where(
                IncidentRow.affected_cells <= query.max_affected_cells,
            )
        statement = statement.order_by(
            IncidentRow.detected_at.desc(),
            IncidentRow.id,
        ).limit(query.limit)
        return tuple(_incident(row) for row in self._rows(statement))

    def open_in_window(self, window: TimeWindow) -> tuple[Incident, ...]:
        """Every incident detected in the window — used by the scenario scorer and the UI."""
        statement = (
            select(*_INCIDENT_COLUMNS)
            .where(
                IncidentRow.detected_at >= window.start - INCIDENT_WINDOW_SPAN,
                IncidentRow.detected_at < window.end + INCIDENT_WINDOW_SPAN,
            )
            .order_by(IncidentRow.detected_at, IncidentRow.id)
        )
        return tuple(_incident(row) for row in self._rows(statement))


class SqlIncidentLinkStore(_SqlWriteAdapter):
    """Reads and writes `incident_link`, idempotent on its unique constraint."""

    def __init__(
        self,
        session: Session | None = None,
        *,
        engine: Engine | None = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(session, engine=engine)
        self._clock = clock

    def link(
        self,
        incident_id: str,
        entity_type: str,
        entity_id: str,
        *,
        weight: float = 1.0,
        rationale: str = "",
    ) -> bool:
        """Returns False when the link already existed. Idempotent by construction."""
        existing = self._scalar(
            select(IncidentLinkRow.id).where(
                IncidentLinkRow.incident_id == incident_id,
                IncidentLinkRow.entity_type == entity_type,
                IncidentLinkRow.entity_id == entity_id,
            ),
        )
        if existing is not None:
            return False
        statement = insert(IncidentLinkRow).values(
            incident_id=incident_id,
            entity_type=entity_type,
            entity_id=entity_id,
            weight=weight,
            rationale=rationale[:120] or None,
            linked_at=self._now(),
        )
        try:
            self._write(statement)
        except IntegrityError:
            # Lost the race to another worker; the link exists, which is all we wanted.
            if self._session is not None:
                self._session.rollback()
            return False
        return True

    def for_incident(self, incident_id: str) -> tuple[IncidentLink, ...]:
        statement = (
            select(
                IncidentLinkRow.incident_id,
                IncidentLinkRow.entity_type,
                IncidentLinkRow.entity_id,
                IncidentLinkRow.weight,
                IncidentLinkRow.rationale,
                IncidentLinkRow.linked_at,
            )
            .where(IncidentLinkRow.incident_id == incident_id)
            .order_by(
                IncidentLinkRow.entity_type,
                IncidentLinkRow.entity_id,
            )
        )
        return tuple(
            IncidentLink(
                incident_id=str(row[0]),
                entity_type=str(row[1]),
                entity_id=str(row[2]),
                weight=float(row[3] or 0.0),
                rationale=str(row[4] or ""),
                linked_at=row[5],
            )
            for row in self._rows(statement)
        )

    def move(self, *, from_incident_id: str, to_incident_id: str) -> int:
        """Re-point links during a merge. Nothing is deleted except a pure duplicate."""
        moved = 0
        for link in self.for_incident(from_incident_id):
            if self.link(
                to_incident_id,
                link.entity_type,
                link.entity_id,
                weight=link.weight,
                rationale=link.rationale or f"moved from {from_incident_id}",
            ):
                moved += 1
        # The source rows would now duplicate the target's. Their information is preserved
        # on the target, and the absorbed incident keeps its own `incident` link.
        self._write(
            delete(IncidentLinkRow).where(
                IncidentLinkRow.incident_id == from_incident_id,
            ),
        )
        return moved

    def cells_for(self, incident_id: str) -> tuple[str, ...]:
        """The impact module's port: which cells this incident was built from."""
        statement = (
            select(IncidentLinkRow.entity_id)
            .where(
                IncidentLinkRow.incident_id == incident_id,
                IncidentLinkRow.entity_type == "cell",
            )
            .order_by(IncidentLinkRow.entity_id)
        )
        return tuple(str(row[0]) for row in self._rows(statement))

    def _now(self) -> datetime:
        if self._clock is None:
            raise ValueError("SqlIncidentLinkStore needs a clock to stamp linked_at")
        return self._clock.now()


class CorrelationIncidentBridge:
    """Satisfies correlation's `IncidentRepository` by delegating to `IncidentService`.

    Correlation declares what it needs from the incident aggregate in its own vocabulary;
    this adapter is the only place that knows both. Without it the two modules would have
    to import each other, which the module-boundary contract forbids — and should, because
    correlation must stay testable without a lifecycle.
    """

    __slots__ = ("_incidents",)

    def __init__(self, incidents: IncidentService) -> None:
        self._incidents = incidents

    def open_or_update(self, candidate: CorrelationCandidate) -> IncidentRef:
        stored = self._incidents.open_or_update(_translate(candidate))
        return _ref(stored)

    def find_candidates(
        self,
        node_ids: Sequence[str],
        window: TimeWindow,
    ) -> tuple[IncidentRef, ...]:
        return tuple(_ref(incident) for incident in self._incidents.find_by_nodes(node_ids, window))

    def reopen(self, incident_id: str, *, reason: str) -> IncidentRef:
        return _ref(self._incidents.reopen(incident_id, reason=reason))

    def link(
        self,
        incident_id: str,
        entity_type: str,
        entity_id: str,
        *,
        weight: float = 1.0,
        rationale: str = "",
    ) -> bool:
        return self._incidents.link(
            incident_id,
            entity_type,
            entity_id,
            weight=weight,
            rationale=rationale,
        )


def _translate(candidate: CorrelationCandidate) -> IncidentCandidate:
    """Correlation's candidate -> the incidents module's candidate. Same key by design."""
    return IncidentCandidate(
        primary_node_id=candidate.primary_node_id,
        primary_node_kind=candidate.primary_node_kind,
        cause=candidate.cause,
        window=candidate.window,
        region=candidate.region,
        cell_ids=candidate.cell_ids,
        site_ids=candidate.site_ids,
        alarm_ids=candidate.alarm_ids,
        severity=candidate.severity,
        confidence=candidate.confidence,
        complaint_count=candidate.complaint_count,
        title=candidate.title,
        summary=candidate.summary,
        detector=candidate.detector,
        suppressed_reason=candidate.suppressed_reason,
        rule_version=candidate.rule_version or None,
    )


def _ref(incident: Incident) -> IncidentRef:
    return IncidentRef(
        incident_id=incident.id,
        status=str(incident.status),
        primary_node_id=incident.primary_node_id or "",
        primary_node_kind=incident.primary_node_kind or "",
        cause=str(incident.cause),
        window=incident.window,
        version=incident.version,
    )


__all__ = [
    "INCIDENT_WINDOW_SPAN",
    "CorrelationIncidentBridge",
    "SqlIncidentLinkStore",
    "SqlIncidentStore",
]
