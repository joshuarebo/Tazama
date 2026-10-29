"""Every shape a model is allowed to return.

A model that answers in prose is a model whose output has to be parsed by guesswork, and
guesswork in a pipeline that proposes customer messages is how a wrong message gets sent.
So every call names a schema, the response is validated against it, and an invalid
response gets exactly one repair attempt before the deterministic fallback takes over.

The constraints on these models are the product's safety rules expressed as types:

* `Diagnosis.probable_cause` is an enum, not a string, so a model cannot invent a cause
  the runbooks have never heard of.
* `Diagnosis.recommended_runbook_id` is checked against the runbook catalogue by the
  caller; the schema only guarantees the shape.
* `NotificationFill` carries **slots**, never message text. A model fills an approved
  template; it does not write to customers (PRD §11).
* `ReportDigest.numbers_used` exists so a verifier can check every number in the prose
  against the figures the agent was actually given — the grounding check that stops a
  confident fabricated statistic.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tazama.domain.value_objects import ComplaintCategory, IncidentCause

#: Longest free-text field any model may return. Long enough for a paragraph of summary,
#: short enough that a runaway generation cannot fill a card or a log line.
MAX_TEXT = 1_200


class StrictModel(BaseModel):
    """Reject unknown fields.

    `extra="forbid"` is the point: a model that returns an extra field has misunderstood
    the contract, and accepting it silently means the next prompt change can quietly stop
    working.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EvidenceRef(StrictModel):
    """A pointer to something in the database, never a claim in itself."""

    kind: Literal["alarm", "kpi", "complaint", "topology", "maintenance", "history", "doc"]
    ref: Annotated[str, Field(min_length=1, max_length=120)]
    note: Annotated[str, Field(max_length=300)] = ""


class ClassifiedComplaint(StrictModel):
    """The LLM path for complaint classification, mirroring the rules classifier's output."""

    category: ComplaintCategory
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    language: Annotated[str, Field(max_length=16)] = "en"
    is_network: bool = False
    urgency: Annotated[int, Field(ge=1, le=5)] = 3
    reason: Annotated[str, Field(max_length=MAX_TEXT)] = ""


class Diagnosis(StrictModel):
    """What the DiagnosisAgent is allowed to conclude."""

    probable_cause: IncidentCause
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    evidence: Annotated[list[EvidenceRef], Field(min_length=1, max_length=12)]
    recommended_runbook_id: Annotated[str, Field(max_length=64)] | None = None
    summary: Annotated[str, Field(max_length=MAX_TEXT)] = ""

    @field_validator("evidence")
    @classmethod
    def _evidence_must_be_distinct(cls, value: list[EvidenceRef]) -> list[EvidenceRef]:
        """Repeating one alarm eight times is not eight pieces of evidence.

        Without this a model can manufacture apparent confidence by listing the same row
        over and over, and a reviewer scanning the card would see a long evidence list.
        """
        seen = {(item.kind, item.ref) for item in value}
        if len(seen) != len(value):
            raise ValueError("evidence entries must be distinct")
        return value


class KnowledgeAnswer(StrictModel):
    """The KnowledgeAssistant's answer, which must cite or decline."""

    answer: Annotated[str, Field(max_length=MAX_TEXT)]
    citations: Annotated[list[dict[str, Any]], Field(max_length=10)] = Field(default_factory=list)
    sufficient_evidence: bool = True

    @field_validator("citations")
    @classmethod
    def _citations_name_a_document(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for citation in value:
            if not str(citation.get("doc_id", "")).strip():
                raise ValueError("every citation must name a doc_id")
        return value


class ReportDigest(StrictModel):
    """Prose for a digest, plus the numbers it used so they can be checked."""

    headline: Annotated[str, Field(min_length=1, max_length=200)]
    bullets: Annotated[list[Annotated[str, Field(max_length=300)]], Field(max_length=8)] = Field(
        default_factory=list,
    )
    numbers_used: Annotated[list[float], Field(max_length=40)] = Field(default_factory=list)


class NotificationFill(StrictModel):
    """Template slots. **Not** message text — the template owns the words."""

    template_id: Annotated[str, Field(min_length=1, max_length=64)]
    slots: dict[str, Annotated[str, Field(max_length=120)]] = Field(default_factory=dict)

    @field_validator("slots")
    @classmethod
    def _slots_are_values_not_sentences(
        cls,
        value: dict[str, str],
    ) -> dict[str, str]:
        """A slot holds a place name or a number, not a sentence a model wrote.

        This is the line between "the LLM fills an approved template" and "the LLM writes
        to customers". Without it, a single slot called `message` would quietly become
        free-text generation with a template's signature on it.
        """
        for key, slot in value.items():
            if len(slot.split()) > 8:
                raise ValueError(
                    f"slot {key!r} looks like prose; slots carry values, not sentences",
                )
        return value


#: Which schema each named task returns. The gateway reads this, so a task cannot be
#: called without a schema — "no schema" is not a reachable state.
TASK_SCHEMAS: dict[str, type[StrictModel]] = {
    "classify_complaint": ClassifiedComplaint,
    "diagnose_incident": Diagnosis,
    "knowledge_answer": KnowledgeAnswer,
    "report_digest": ReportDigest,
    "fill_notification": NotificationFill,
}


def schema_for(task: str) -> type[StrictModel]:
    schema = TASK_SCHEMAS.get(task)
    if schema is None:
        raise KeyError(
            f"unknown LLM task {task!r}; add it to TASK_SCHEMAS with the shape it returns",
        )
    return schema
