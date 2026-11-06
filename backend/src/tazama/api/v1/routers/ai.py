"""The AI surface: diagnose, ask, suggest SQL, and report on itself.

Every route here is a **read**, and that is structural rather than promised:

* The agents hold no tool above tier `propose`, and a `propose` tool returns a proposal
  object and writes nothing.
* `SqlAgentData` — the only database access the agents have — contains no write path.
* `POST /ai/sql` returns **SQL, not results.** The model proposes, a human reads it, and
  `POST /discovery/query` is the only thing that ever executes anything. That separation is
  what makes natural-language querying safe to offer at all: the worst a bad suggestion can
  do is waste a reviewer's time.

They are POSTs rather than GETs because each one spends a budget and writes a decision
record — they are not cacheable and should not be retried by a proxy.

`/ai/status` exists so the console can tell the truth about itself. "AI degraded" is a
first-class state, not an error, and a reviewer should be able to see *why* without reading
logs.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from tazama.ai.agents.diagnosis import CONFIDENCE_FLOOR, DiagnosisAgent
from tazama.ai.agents.knowledge import KnowledgeAssistant
from tazama.ai.agents.tools import build_registry
from tazama.ai.llm.gateway import LlmGateway, get_gateway
from tazama.ai.llm.schemas import SqlSuggestion
from tazama.api.deps import ClockDep, PrincipalDep, SessionDep, SettingsDep, requires
from tazama.core.errors import NotFound
from tazama.core.logging import get_logger
from tazama.infrastructure.db.agent_repo import SqlAgentData
from tazama.modules.discovery import sql_guard

_log = get_logger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])

#: The system prompt for SQL suggestion. Narrow on purpose: it describes the schema and the
#: rules, and says nothing about being helpful. A model told to be helpful about SQL invents
#: columns.
_SQL_SYSTEM = """\
You translate a question about a mobile network's care and performance data into one
read-only SQL query.

Rules you must follow:
- Exactly one SELECT statement. Never INSERT, UPDATE, DELETE, DDL or multiple statements.
- Use only the tables and columns listed for you. Never invent a column name.
- Never select a column that holds an identifier or free text from a customer.
- Always bound the time range, and anchor it on the data (for example
  `(SELECT MAX(received_at) FROM complaint)`) rather than on the current date — this
  database is a fixed snapshot and `now` is not in it.
- Prefer an aggregate over a row dump. Someone is reading the answer, not scrolling it.
- List every table you used in tables_used.
"""


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AskRequest(_Request):
    question: Annotated[str, Field(min_length=3, max_length=500)]
    limit: Annotated[int, Field(ge=1, le=8)] = 4


class SqlRequest(_Request):
    question: Annotated[str, Field(min_length=3, max_length=500)]


class TraceOut(BaseModel):
    """The agent's own account of what it did. The refusals are the interesting part."""

    agent: str
    goal: str
    steps: list[str]
    called: list[str]
    refused: list[str]
    tool_calls: list[dict[str, Any]]
    budget: dict[str, Any]
    stopped_because: str
    degraded: bool
    escalated: bool


class DiagnosisOut(BaseModel):
    incident_id: str
    cause: str | None
    confidence: float
    confidence_floor: float
    needs_human: bool
    method: str
    summary: str
    evidence: list[dict[str, Any]]
    runbook_id: str | None
    trace: TraceOut


class AskOut(BaseModel):
    question: str
    answer: str
    citations: list[dict[str, Any]]
    sufficient_evidence: bool
    declined: bool
    method: str
    trace: TraceOut


class SqlOut(BaseModel):
    """A suggestion and the guard's opinion of it. **Nothing has been executed.**"""

    question: str
    sql: str | None
    explanation: str
    tables_used: list[str]
    confidence: float
    #: What the SQL guard would do with this query, decided without running it.
    guard_verdict: str
    guard_reason: str | None
    executed: bool = False
    degraded: bool
    note: str
    injection_flags: list[str]


class StatusOut(BaseModel):
    configured: bool
    reachable: bool
    degraded: bool
    models: list[str]
    tool_calling: bool
    probe_note: str
    accounting: dict[str, Any]
    budget_remaining: dict[str, int]
    agents: list[dict[str, Any]]


def _trace(trace: Any) -> TraceOut:
    return TraceOut(
        agent=trace.agent,
        goal=trace.goal,
        steps=list(trace.steps),
        called=list(trace.called),
        refused=list(trace.refused),
        tool_calls=[call.to_dict() for call in trace.tool_calls],
        budget=dict(trace.budget),
        stopped_because=trace.stopped_because,
        degraded=trace.degraded,
        escalated=trace.escalated,
    )


def _registry(session: SessionDep, clock: ClockDep) -> Any:
    return build_registry(SqlAgentData(session, clock=clock))


@router.post(
    "/diagnose/{incident_id}",
    response_model=DiagnosisOut,
    dependencies=[Depends(requires("ai:diagnose"))],
    summary="Diagnose one incident — rules first, model only where rules cannot decide",
)
async def diagnose(
    incident_id: str,
    session: SessionDep,
    clock: ClockDep,
) -> DiagnosisOut:
    """Run the DiagnosisAgent.

    `method` is the honest part of the response: `rules` means the deterministic path was
    conclusive and no model was consulted, `llm` means the model answered *and its evidence
    checked out*, and `none` means nothing could be concluded. A model answer that cited
    evidence the agent was never shown is discarded before it gets here.
    """
    agent = DiagnosisAgent(registry=_registry(session, clock), gateway=get_gateway())
    outcome = agent.diagnose(incident_id)

    if outcome.cause is None and outcome.trace.stopped_because == "incident not found":
        raise NotFound(f"Incident {incident_id!r} does not exist.")

    return DiagnosisOut(
        incident_id=outcome.incident_id,
        cause=str(outcome.cause) if outcome.cause else None,
        confidence=round(outcome.confidence, 4),
        confidence_floor=CONFIDENCE_FLOOR,
        needs_human=outcome.needs_human,
        method=outcome.method,
        summary=outcome.summary,
        evidence=list(outcome.evidence),
        runbook_id=outcome.runbook_id,
        trace=_trace(outcome.trace),
    )


@router.post(
    "/ask",
    response_model=AskOut,
    dependencies=[Depends(requires("ai:ask"))],
    summary="Ask the operations knowledge base — cites its sources, or declines",
)
async def ask(body: AskRequest, session: SessionDep, clock: ClockDep) -> AskOut:
    """Run the KnowledgeAssistant.

    It will decline. That is the feature: nothing retrieved means no answer, an answer that
    cited a document which was not retrieved is rejected outright, and expired SOPs are
    never returned at all.
    """
    assistant = KnowledgeAssistant(registry=_registry(session, clock), gateway=get_gateway())
    outcome = assistant.ask(body.question, limit=body.limit)
    return AskOut(
        question=outcome.question,
        answer=outcome.answer,
        citations=[dict(citation) for citation in outcome.citations],
        sufficient_evidence=outcome.sufficient,
        declined=outcome.declined,
        method=outcome.method,
        trace=_trace(outcome.trace),
    )


@router.post(
    "/sql",
    response_model=SqlOut,
    dependencies=[Depends(requires("explorer:query"))],
    summary="Suggest a query from a question. Returns SQL, never results.",
)
async def suggest_sql(
    body: SqlRequest,
    settings: SettingsDep,
    principal: PrincipalDep,
) -> SqlOut:
    """Translate a question into SQL and hand it to a human.

    Gated on `explorer:query` rather than `ai:ask`: suggesting a query to someone who may
    not run one is pointless, and it keeps the two surfaces' audiences identical.

    Nothing is executed here, and nothing is written to the query audit — because no query
    ran. The audit entry appears when the human presses Run, attributed to them, which is
    the correct actor for a query a person approved.
    """
    gateway = get_gateway()
    config = sql_guard.config_from_settings(settings)

    result = gateway.complete(
        task="suggest_sql",
        system=_SQL_SYSTEM,
        user=_sql_prompt(config),
        # The question is a person's free text, so it is untrusted: redacted, scanned and
        # delimited like any complaint. Tool access does not depend on it, and neither does
        # the table allowlist.
        untrusted=body.question,
        prompt_version="suggest-sql-v1",
    )

    flags = list(result.injection_flags)
    if not isinstance(result.parsed, SqlSuggestion):
        _log.info("sql_suggestion_unavailable", extra={"note": result.note})
        return SqlOut(
            question=body.question,
            sql=None,
            explanation=(
                "No query could be suggested. Write the SELECT yourself, or pick one of "
                "the examples."
            ),
            tables_used=[],
            confidence=0.0,
            guard_verdict="not-evaluated",
            guard_reason=None,
            degraded=True,
            note=result.note or "the model did not return a usable suggestion",
            injection_flags=flags,
        )

    suggestion = result.parsed
    # The guard's verdict, decided without executing anything. This is the authoritative
    # check; the schema's shape test earlier is only a filter.
    reason = sql_guard.rejection_reason(suggestion.sql, config)
    if reason:
        _log.warning(
            "sql_suggestion_refused",
            extra={"actor": principal.subject, "reason": reason},
        )

    return SqlOut(
        question=body.question,
        sql=suggestion.sql,
        explanation=suggestion.explanation,
        tables_used=list(suggestion.tables_used),
        confidence=suggestion.confidence,
        guard_verdict="would-be-refused" if reason else "would-be-accepted",
        guard_reason=reason,
        degraded=False,
        note="Review the SQL before running it. Nothing has been executed.",
        injection_flags=flags,
    )


@router.get(
    "/status",
    response_model=StatusOut,
    dependencies=[Depends(requires("ai:ask"))],
    summary="What the model endpoint is doing, and what the AI has cost",
)
async def status() -> StatusOut:
    """The console's own honesty panel.

    Shows whether a model is configured at all, whether it answered the capability probe,
    and what has been spent. With no endpoint configured this reports `degraded` — which is
    a supported configuration, not a fault: every AI path has a deterministic fallback.
    """
    gateway: LlmGateway = get_gateway()
    capabilities = gateway.capabilities()
    return StatusOut(
        configured=gateway.configured,
        reachable=capabilities.reachable,
        degraded=not (gateway.configured and capabilities.reachable),
        models=list(capabilities.models),
        tool_calling=capabilities.tool_calling,
        probe_note=capabilities.note,
        accounting=gateway.accounting,
        budget_remaining=gateway.budget_remaining,
        agents=[
            {
                "name": "DiagnosisAgent",
                "purpose": "Why is this incident happening, and which runbook applies",
                "ceiling": "propose",
                "fallback": "deterministic rules (7/7 on the labelled scenarios)",
            },
            {
                "name": "KnowledgeAssistant",
                "purpose": "Answer an operational question from the SOP corpus, or decline",
                "ceiling": "read",
                "fallback": "the retrieved excerpt verbatim, with its citation",
            },
            {
                "name": "Reporter",
                "purpose": "Turn figures into prose without inventing a figure",
                "ceiling": "read",
                "fallback": "the figures stated plainly",
            },
        ],
    )


def _sql_prompt(config: sql_guard.GuardConfig) -> str:
    """The schema the model is allowed to use, and the goal at the tail.

    Only the allowlisted tables are described. A model that never hears about a table cannot
    write a query against it — which is not the security boundary (the guard is), but there
    is no reason to advertise.
    """
    tables = ", ".join(sorted(config.allowed_tables))
    return "\n".join(
        [
            "You may read only these tables:",
            f"  {tables}",
            "",
            "Useful columns:",
            "  complaint(id, received_at, channel, language, region, cell_id, site_id,",
            "            category, status, incident_id)",
            "  incident(id, title, status, priority, cause, region, detected_at,",
            "           resolved_at, affected_cells, affected_subs, complaint_count)",
            "  cell(id, site_id, name, tech, band, region, subscribers_est)",
            "  site(id, name, region, county, site_type)",
            "  kpi_cell(cell_id, site_id, region, bucket_start, granularity,",
            "           availability_pct, drop_rate_pct, prb_utilisation_pct,",
            "           dl_throughput_mbps, latency_ms, active_users)",
            "  alarm(id, node_id, alarm_type, severity, raised_at, cleared_at)",
            "  care_call(id, started_at, duration_s, reason_code, region, cell_id)",
            "",
            f"A row cap of {config.max_rows} is applied automatically; do not add LIMIT",
            "unless the question asks for a top-N.",
            "",
            "GOAL: write one SELECT that answers the question in the untrusted block, using",
            "only the tables and columns above, with the time range anchored on the data.",
        ],
    )


__all__ = ["router"]
