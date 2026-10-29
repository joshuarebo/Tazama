"""The tools an agent may call, and the ports behind them.

Every tool here is **read-only against the database** except `propose_notification` and
`propose_runbook`, which are tier `propose`: they return a proposal object and write
nothing. There is deliberately no tool that sends, executes or mutates, and
`execute_runbook` is registered as tier `never` precisely so that an agent reaching for it
is recorded rather than silently unable to find it (CLAUDE.md §7, PRD §11).

The data access goes through a narrow port, not the repositories. `ai/` sits above
`modules/` and below `infrastructure/` in the dependency order, so the agents describe what
they need and the composition root supplies it. That also makes the whole tool surface
trivially fakeable in evals without a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from tazama.ai.agents.harness import ToolRegistry, ToolSpec


class _Args(BaseModel):
    """Tool arguments are strict: an unknown field is a hallucination, not a hint."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


IncidentId = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.:-]+$")]
CellId = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.:-]+$")]


class IncidentArgs(_Args):
    incident_id: IncidentId


class KpiArgs(_Args):
    cell_id: CellId
    hours: Annotated[int, Field(ge=1, le=168)] = 24


class SimilarArgs(_Args):
    incident_id: IncidentId
    limit: Annotated[int, Field(ge=1, le=10)] = 5


class SearchArgs(_Args):
    query: Annotated[str, Field(min_length=2, max_length=300)]
    limit: Annotated[int, Field(ge=1, le=8)] = 4


class RunbookArgs(_Args):
    incident_id: IncidentId
    runbook_id: Annotated[str, Field(min_length=1, max_length=64)]
    justification: Annotated[str, Field(min_length=10, max_length=600)]


class NotificationArgs(_Args):
    incident_id: IncidentId
    template_id: Annotated[str, Field(min_length=1, max_length=64)]
    slots: dict[str, Annotated[str, Field(max_length=120)]] = Field(default_factory=dict)


class ExecuteArgs(_Args):
    runbook_id: Annotated[str, Field(min_length=1, max_length=64)]


@runtime_checkable
class AgentData(Protocol):
    """Everything the agents are allowed to read.

    Narrow on purpose. If an agent needs something new, it is added here deliberately,
    reviewed, and tiered — rather than arriving as a convenient import somewhere.
    """

    def incident(self, incident_id: str) -> dict[str, Any] | None: ...

    def alarms(self, incident_id: str) -> tuple[dict[str, Any], ...]: ...

    def kpi_window(self, cell_id: str, hours: int) -> tuple[dict[str, Any], ...]: ...

    def complaint_summary(self, incident_id: str) -> dict[str, Any]: ...

    def topology(self, incident_id: str) -> tuple[dict[str, Any], ...]: ...

    def maintenance(self, incident_id: str) -> tuple[dict[str, Any], ...]: ...

    def similar_incidents(self, incident_id: str, limit: int) -> tuple[dict[str, Any], ...]: ...

    def runbooks(self) -> tuple[dict[str, Any], ...]: ...

    def search_knowledge(self, query: str, limit: int) -> tuple[dict[str, Any], ...]: ...


@dataclass(frozen=True, slots=True)
class RunbookProposal:
    """A recommendation. Nothing has run; nothing will until a human approves."""

    incident_id: str
    runbook_id: str
    justification: str
    requires_approval: bool = True
    proposed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "runbook_proposal",
            "incident_id": self.incident_id,
            "runbook_id": self.runbook_id,
            "justification": self.justification,
            "requires_approval": True,
            "executed": False,
        }


@dataclass(frozen=True, slots=True)
class NotificationProposal:
    """Template id plus slots. No message text — the template owns the words."""

    incident_id: str
    template_id: str
    slots: dict[str, str]
    requires_approval: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "notification_proposal",
            "incident_id": self.incident_id,
            "template_id": self.template_id,
            "slots": dict(self.slots),
            "requires_approval": True,
            "sent": False,
        }


#: What each agent is granted. Written here rather than derived, so the answer to "what can
#: the KnowledgeAssistant touch?" is a list someone can read, not a rule someone must
#: evaluate. Note that no agent is granted a `never` tool — that tier exists to catch the
#: request, not to be handed out.
GRANTS: dict[str, tuple[str, ...]] = {
    "DiagnosisAgent": (
        "get_incident",
        "get_alarms",
        "get_kpi_window",
        "get_complaint_summary",
        "get_topology",
        "get_maintenance",
        "find_similar_incidents",
        "list_runbooks",
        "propose_runbook",
    ),
    "KnowledgeAssistant": (
        "search_knowledge",
        "get_incident",
        "list_runbooks",
    ),
    "Reporter": (
        "get_incident",
        "get_complaint_summary",
        "find_similar_incidents",
    ),
}

#: The tier ceiling per agent. The Reporter and the KnowledgeAssistant cannot propose
#: anything at all; only the DiagnosisAgent can, and only a runbook.
CEILINGS: dict[str, str] = {
    "DiagnosisAgent": "propose",
    "KnowledgeAssistant": "read",
    "Reporter": "read",
}


def build_registry(data: AgentData) -> ToolRegistry:
    """Wire the tool surface over one data port."""
    return ToolRegistry(
        [
            ToolSpec(
                name="get_incident",
                description="Facts about one incident: status, priority, region, impact.",
                args=IncidentArgs,
                handler=lambda a: data.incident(a.incident_id),  # type: ignore[attr-defined]
            ),
            ToolSpec(
                name="get_alarms",
                description="Alarms raised on the nodes attached to an incident.",
                args=IncidentArgs,
                handler=lambda a: list(data.alarms(a.incident_id)),  # type: ignore[attr-defined]
            ),
            ToolSpec(
                name="get_kpi_window",
                description="Hourly KPI samples for one cell over the last N hours.",
                args=KpiArgs,
                handler=lambda a: list(
                    data.kpi_window(a.cell_id, a.hours),  # type: ignore[attr-defined]
                ),
            ),
            ToolSpec(
                name="get_complaint_summary",
                description=(
                    "Aggregate complaint counts and categories for an incident. "
                    "Counts only — never individual subscribers."
                ),
                args=IncidentArgs,
                handler=lambda a: data.complaint_summary(a.incident_id),  # type: ignore[attr-defined]
            ),
            ToolSpec(
                name="get_topology",
                description="Upstream nodes shared by the affected cells.",
                args=IncidentArgs,
                handler=lambda a: list(data.topology(a.incident_id)),  # type: ignore[attr-defined]
            ),
            ToolSpec(
                name="get_maintenance",
                description="Planned maintenance windows overlapping the incident.",
                args=IncidentArgs,
                handler=lambda a: list(data.maintenance(a.incident_id)),  # type: ignore[attr-defined]
            ),
            ToolSpec(
                name="find_similar_incidents",
                description="Past incidents with a similar shape, and how they were resolved.",
                args=SimilarArgs,
                handler=lambda a: list(
                    data.similar_incidents(a.incident_id, a.limit),  # type: ignore[attr-defined]
                ),
            ),
            ToolSpec(
                name="list_runbooks",
                description="The approved runbook catalogue with their trigger conditions.",
                args=_Args,
                handler=lambda _a: list(data.runbooks()),
            ),
            ToolSpec(
                name="search_knowledge",
                description="Search the operations knowledge base. Returns cited chunks.",
                args=SearchArgs,
                handler=lambda a: list(
                    data.search_knowledge(a.query, a.limit),  # type: ignore[attr-defined]
                ),
            ),
            ToolSpec(
                name="propose_runbook",
                description=(
                    "Recommend an approved runbook for a human to approve. "
                    "This does not run anything."
                ),
                args=RunbookArgs,
                tier="propose",
                handler=lambda a: RunbookProposal(
                    incident_id=a.incident_id,  # type: ignore[attr-defined]
                    runbook_id=a.runbook_id,  # type: ignore[attr-defined]
                    justification=a.justification,  # type: ignore[attr-defined]
                ).to_dict(),
            ),
            ToolSpec(
                name="propose_notification",
                description=(
                    "Propose a customer notification by template id and slot values. "
                    "Never sends. Never writes message text."
                ),
                args=NotificationArgs,
                tier="propose",
                handler=lambda a: NotificationProposal(
                    incident_id=a.incident_id,  # type: ignore[attr-defined]
                    template_id=a.template_id,  # type: ignore[attr-defined]
                    slots=dict(a.slots),  # type: ignore[attr-defined]
                ).to_dict(),
            ),
            # Registered so that asking is recorded. There is no handler worth writing: it
            # must never run, and a tier-`never` tool is refused before the handler is
            # reached. The lambda exists only to satisfy the dataclass.
            ToolSpec(
                name="execute_runbook",
                description="Not available. Runbook execution requires human approval.",
                args=ExecuteArgs,
                tier="never",
                handler=lambda _a: (_ for _ in ()).throw(
                    AssertionError("a tier-never tool must never be invoked"),
                ),
            ),
        ],
    )
