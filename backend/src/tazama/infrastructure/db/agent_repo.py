"""The database behind the agents' tools.

`SqlAgentData` satisfies the `AgentData` protocol declared in `ai/agents/tools.py`. The
agents live above this layer and may not import it (the layered import contract), so the
composition root wires the two together.

Everything here is a **read**. There is no write path, no mutation and no side effect, which
is what makes the agents' `read` tier structurally true rather than a convention: even if a
model asked for something destructive, there is nothing in this file that could do it.

Two things are deliberately narrower than the equivalent API queries:

* **Complaints are returned as counts and categories, never as rows.** An agent summarising
  an incident has no business reading the text a customer typed, and a count cannot carry a
  phone number.
* **Expired knowledge is excluded.** A stale SOP is worse than no SOP, because an engineer
  acts on it. The seed contains one deliberately-expired document so this is tested rather
  than asserted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tazama.core.clock import Clock, SystemClock
from tazama.infrastructure.db.engine import get_engine, session_factory
from tazama.infrastructure.db.models import (
    Alarm,
    Cell,
    Complaint,
    Incident,
    IncidentLink,
    KnowledgeDoc,
    KpiCell,
    MaintenanceWindow,
    Runbook,
    Site,
    TopologyNode,
)

#: How far either side of an incident the agent may look for overlapping maintenance. The
#: same padding the incident card uses, so the two never disagree about what "overlapping"
#: means.
WINDOW_PADDING = timedelta(hours=6)

#: Fallback window length when an incident has no resolution time. `incident` has no
#: `window_end` column (tech-debt #19), so an unresolved incident's window is reconstructed.
OPEN_INCIDENT_WINDOW = timedelta(hours=6)

#: Longest knowledge excerpt handed to an agent. Long enough to carry a procedure, short
#: enough that four of them do not crowd the instructions out of the prompt.
MAX_DOC_CHARS = 2_000

#: Words too common to narrow a search. Without this, "how do I fix the link" matches every
#: document that contains "the".
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "is",
        "are",
        "was",
        "how",
        "do",
        "i",
        "we",
        "what",
        "when",
        "why",
        "which",
        "that",
        "this",
        "with",
        "it",
        "be",
        "can",
        "should",
        "my",
        "our",
        "if",
        "at",
        "by",
        "from",
        "as",
    },
)


class SqlAgentData:
    """Read-only database access for the agent tool surface.

    Accepts either a session (inside a request or a unit of work) or an engine (a worker or
    a script). Same convention as the other adapters.
    """

    __slots__ = ("_clock", "_engine", "_session")

    def __init__(
        self,
        session: Session | None = None,
        *,
        engine: Engine | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._session = session
        self._engine = engine
        self._clock = clock or SystemClock()

    # ------------------------------------------------------------------ plumbing

    def _rows(self, statement: Select[Any]) -> list[Any]:
        """Run a statement on the caller's session, or on a fresh one.

        A `Session` in both branches, not a `Connection`: `select(MappedClass)` on a
        connection returns columns rather than the mapped object, and that difference only
        shows up on the engine-only path — where it is least likely to be noticed.
        """
        if self._session is not None:
            return list(self._session.execute(statement).scalars().all())
        with session_factory(self._engine or get_engine())() as session:
            return list(session.execute(statement).scalars().all())

    def _scalar(self, statement: Select[Any]) -> Any:
        if self._session is not None:
            return self._session.execute(statement).scalar_one_or_none()
        with session_factory(self._engine or get_engine())() as session:
            return session.execute(statement).scalar_one_or_none()

    def _tuples(self, statement: Select[Any]) -> list[Any]:
        if self._session is not None:
            return list(self._session.execute(statement).all())
        with session_factory(self._engine or get_engine())() as session:
            return list(session.execute(statement).all())

    def _links(self, incident_id: str, entity_type: str) -> list[str]:
        return [
            str(value)
            for value in self._rows(
                select(IncidentLink.entity_id)
                .where(IncidentLink.incident_id == incident_id)
                .where(IncidentLink.entity_type == entity_type),
            )
        ]

    def _window(self, incident: Incident) -> tuple[datetime, datetime]:
        start = _utc(incident.started_at or incident.detected_at)
        end = _utc(incident.resolved_at) or (start + OPEN_INCIDENT_WINDOW)
        return start - WINDOW_PADDING, end + WINDOW_PADDING

    # ------------------------------------------------------------------ the port

    def incident(self, incident_id: str) -> dict[str, Any] | None:
        rows = self._rows(select(Incident).where(Incident.id == incident_id))
        if not rows:
            return None
        row: Incident = rows[0]
        return {
            "id": row.id,
            "title": row.title,
            "status": row.status,
            "priority": row.priority,
            "cause": row.cause,
            "region": row.region,
            "primary_node_id": row.primary_node_id,
            "primary_node_kind": row.primary_node_kind,
            "detected_at": _iso(row.detected_at),
            "started_at": _iso(row.started_at),
            "resolved_at": _iso(row.resolved_at),
            "affected_cells": row.affected_cells,
            "affected_sites": row.affected_sites,
            "affected_subs": row.affected_subs,
            "enterprise_accounts": row.enterprise_accounts,
            "complaint_count": row.complaint_count,
            "sla_risk": row.sla_risk,
            "suppressed_reason": row.suppressed_reason,
        }

    def alarms(self, incident_id: str) -> tuple[dict[str, Any], ...]:
        alarm_ids = self._links(incident_id, "alarm")
        if not alarm_ids:
            return ()
        rows = self._rows(
            select(Alarm).where(Alarm.id.in_(alarm_ids)).order_by(Alarm.raised_at),
        )
        return tuple(
            {
                "id": row.id,
                "node_id": row.node_id,
                "node_kind": row.node_kind,
                "alarm_type": row.alarm_type,
                "severity": row.severity,
                "raised_at": _iso(row.raised_at),
                "cleared_at": _iso(row.cleared_at),
                "is_flapping": bool(row.is_flapping),
                "cell_id": row.cell_id,
                "site_id": row.site_id,
            }
            for row in rows
        )

    def kpi_window(self, cell_id: str, hours: int) -> tuple[dict[str, Any], ...]:
        """Hourly samples, newest last.

        Capped at the requested hours by the tool schema (1 to 168), so a model cannot ask for
        the whole history and bury its own instructions under it.
        """
        latest = self._scalar(
            select(func.max(KpiCell.bucket_start)).where(KpiCell.cell_id == cell_id),
        )
        if latest is None:
            return ()
        start = _utc(latest) - timedelta(hours=hours)
        rows = self._rows(
            select(KpiCell)
            .where(KpiCell.cell_id == cell_id)
            .where(KpiCell.granularity == "1h")
            .where(KpiCell.bucket_start >= start)
            .order_by(KpiCell.bucket_start),
        )
        return tuple(
            {
                "at": _iso(row.bucket_start),
                "availability_pct": row.availability_pct,
                "rrc_setup_success_pct": row.rrc_setup_success_pct,
                "erab_setup_success_pct": row.erab_setup_success_pct,
                "drop_rate_pct": row.drop_rate_pct,
                "prb_utilisation_pct": row.prb_utilisation_pct,
                "dl_throughput_mbps": row.dl_throughput_mbps,
                "latency_ms": row.latency_ms,
                "active_users": row.active_users,
            }
            for row in rows
        )

    def complaint_summary(self, incident_id: str) -> dict[str, Any]:
        """Counts and categories. **Never complaint text, never a subscriber.**

        An agent reasoning about an incident needs to know that forty people complained and
        that most said "no service". It does not need to read what any of them wrote, and a
        count cannot carry an identifier out to a model endpoint.
        """
        complaint_ids = self._links(incident_id, "complaint")
        if not complaint_ids:
            return {"total": 0, "by_category": {}, "by_channel": {}, "languages": {}}

        def tally(column: Any) -> dict[str, int]:
            rows = self._tuples(
                select(column, func.count())
                .where(Complaint.id.in_(complaint_ids))
                .group_by(column),
            )
            return {str(key): int(count) for key, count in rows if key is not None}

        return {
            "total": len(complaint_ids),
            "by_category": tally(Complaint.category),
            "by_channel": tally(Complaint.channel),
            "languages": tally(Complaint.language),
        }

    def topology(self, incident_id: str) -> tuple[dict[str, Any], ...]:
        """Upstream nodes the affected cells share, with how many each explains.

        `affected_cells` on each node is what makes a transmission fault diagnosable: one
        node explaining nine cells is a different story from nine unrelated cells.
        """
        cell_ids = self._links(incident_id, "cell")
        if not cell_ids:
            return ()

        sites = self._tuples(
            select(Cell.site_id, func.count()).where(Cell.id.in_(cell_ids)).group_by(Cell.site_id),
        )
        nodes: list[dict[str, Any]] = []

        for site_id, count in sites:
            if site_id is None:
                continue
            site_rows = self._rows(select(Site).where(Site.id == site_id))
            name = site_rows[0].name if site_rows else str(site_id)
            nodes.append(
                {
                    "node_id": str(site_id),
                    "kind": "site",
                    "name": name,
                    "affected_cells": int(count),
                },
            )

        # Plus anything the correlation engine itself attached as a topology link — that is
        # where an aggregation or transmission node comes from.
        for node_id in self._links(incident_id, "topology"):
            rows = self._rows(select(TopologyNode).where(TopologyNode.id == node_id))
            kind = rows[0].kind if rows else "unknown"
            name = rows[0].name if rows else node_id
            nodes.append(
                {
                    "node_id": node_id,
                    "kind": kind,
                    "name": name,
                    # A node the engine attached explains every affected cell by definition;
                    # that is why it was attached.
                    "affected_cells": len(cell_ids),
                },
            )
        return tuple(nodes)

    def maintenance(self, incident_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._rows(select(Incident).where(Incident.id == incident_id))
        if not rows:
            return ()
        start, end = self._window(rows[0])
        windows = self._rows(
            select(MaintenanceWindow)
            .where(MaintenanceWindow.ends_at >= start)
            .where(MaintenanceWindow.starts_at < end)
            .limit(10),
        )
        return tuple(
            {
                "id": row.id,
                "starts_at": _iso(row.starts_at),
                "ends_at": _iso(row.ends_at),
                "reason": row.description,
                "node_id": row.node_id,
                "node_kind": row.node_kind,
            }
            for row in windows
        )

    def similar_incidents(self, incident_id: str, limit: int) -> tuple[dict[str, Any], ...]:
        rows = self._rows(select(Incident).where(Incident.id == incident_id))
        if not rows:
            return ()
        row = rows[0]
        # Same cause, or the same region when no cause has been established yet — a
        # region match is weaker prior art and the agent is told so by the ordering.
        condition = Incident.cause == row.cause if row.cause else Incident.region == row.region
        others = self._rows(
            select(Incident)
            .where(Incident.id != incident_id)
            .where(condition)
            .order_by(Incident.detected_at.desc())
            .limit(max(1, min(limit, 10))),
        )
        return tuple(
            {
                "id": other.id,
                "title": other.title,
                "cause": other.cause,
                "region": other.region,
                "status": other.status,
                "detected_at": _iso(other.detected_at),
                "affected_subs": other.affected_subs,
                "duration_minutes": (
                    None
                    if other.resolved_at is None
                    else round(
                        (_utc(other.resolved_at) - _utc(other.detected_at)).total_seconds() / 60,
                        1,
                    )
                ),
            }
            for other in others
        )

    def runbooks(self) -> tuple[dict[str, Any], ...]:
        """The approved catalogue.

        `causes` is a **list** even though the column is a single value: the agent matches a
        diagnosed cause against it, and a runbook that later applies to two causes should not
        require changing the agent.
        """
        rows = self._rows(select(Runbook).order_by(Runbook.id))
        return tuple(
            {
                "id": row.id,
                "name": row.title,
                "causes": [row.cause],
                "risk_tier": row.risk_tier,
                "steps": list(row.steps or []),
                "expected_effect": row.expected_effect,
            }
            for row in rows
        )

    def search_knowledge(self, query: str, limit: int) -> tuple[dict[str, Any], ...]:
        """Keyword search over the SOP corpus, newest-valid first.

        Deliberately simple (tech-debt #32): the corpus is a few dozen documents and the
        thing that makes an answer safe is citation integrity, not retrieval sophistication.
        What is *not* simple is the expiry filter — a stale SOP is worse than no SOP, because
        an engineer acts on it.
        """
        terms = [
            term
            for term in "".join(c if c.isalnum() else " " for c in query.lower()).split()
            if len(term) > 2 and term not in _STOPWORDS
        ]
        now = self._clock.now()

        statement = select(KnowledgeDoc).where(
            or_(KnowledgeDoc.expires_at.is_(None), KnowledgeDoc.expires_at > now),
        )
        if terms:
            statement = statement.where(
                or_(
                    *[
                        or_(
                            func.lower(KnowledgeDoc.title).like(f"%{term}%"),
                            func.lower(KnowledgeDoc.body).like(f"%{term}%"),
                        )
                        for term in terms
                    ],
                ),
            )
        rows = self._rows(statement.order_by(KnowledgeDoc.id).limit(max(1, min(limit, 8)) * 4))

        # Rank in Python: how many distinct query terms the document matches. A SQL
        # relevance score would need a full-text index the demo database does not have, and
        # the corpus is small enough that this is exact rather than approximate.
        def score(doc: KnowledgeDoc) -> tuple[int, str]:
            haystack = f"{doc.title} {doc.body}".lower()
            return (-sum(1 for term in terms if term in haystack), doc.id)

        ranked = sorted(rows, key=score)[: max(1, min(limit, 8))]
        return tuple(
            {
                "doc_id": doc.id,
                "title": doc.title,
                "chunk": doc.chunk_index,
                "doc_type": doc.doc_type,
                "owner": doc.owner,
                "expires_at": _iso(doc.expires_at),
                "text": (doc.body or "")[:MAX_DOC_CHARS],
            }
            for doc in ranked
        )


def _utc(value: datetime | None) -> Any:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    aware = _utc(value)
    return None if aware is None else aware.isoformat()
