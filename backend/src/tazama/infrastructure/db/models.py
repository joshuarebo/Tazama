"""The canonical app schema (PRD section 7).

Conventions, applied everywhere:
  * Table and column names are uppercase-free snake_case and <= 30 characters, so the same
    DDL is valid on Oracle.
  * Every timestamp is `UtcDateTime` - stored UTC, returned timezone-aware.
  * JSON lives in `JsonText` (CLOB on Oracle, with an `IS JSON` check added by migration).
  * Subscriber identity is a pseudonym token; there is **no raw MSISDN column anywhere**
    (ADR 0008). `msisdn_format` records only which *shape* the input used, for testing.
  * Ground truth lives in its own table (`ground_truth`) so the pipeline cannot read the
    answer it is supposed to derive. Leakage tests assert nothing but evals touches it.
  * No relationships are declared: joins are explicit in repositories, which keeps lazy
    loading (and N+1 queries) impossible by construction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tazama.infrastructure.db.base import Base
from tazama.infrastructure.db.types import Embedding, JsonText, UtcDateTime

#: JSON payloads are documents, so the element type is deliberately open.
# Plain aliases rather than 	ype statements: SQLAlchemy resolves these annotations at
# runtime, so they must be ordinary objects.
JsonDict = dict[str, Any]
JsonList = list[Any]

ID = String(40)
SHORT = String(40)
NAME = String(120)
CODE = String(32)


# ===================================================================== network


class Site(Base):
    __tablename__ = "site"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    name: Mapped[str] = mapped_column(NAME)
    region: Mapped[str] = mapped_column(CODE, index=True)
    county: Mapped[str] = mapped_column(CODE)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    site_type: Mapped[str] = mapped_column(CODE)  # macro | micro | small_cell
    power_backup: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_node_id: Mapped[str | None] = mapped_column(ID, index=True)
    commissioned_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    __table_args__ = (
        CheckConstraint("lat between -90 and 90", name="lat_range"),
        CheckConstraint("lon between -180 and 180", name="lon_range"),
        Index("ix_site_region_type", "region", "site_type"),
    )


class Cell(Base):
    __tablename__ = "cell"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    site_id: Mapped[str] = mapped_column(ID, ForeignKey("site.id"), index=True)
    name: Mapped[str] = mapped_column(NAME)
    tech: Mapped[str] = mapped_column(CODE, index=True)  # 2G | 3G | 4G | 5G
    band: Mapped[str] = mapped_column(CODE)
    azimuth_deg: Mapped[int] = mapped_column(Integer, default=0)
    subscribers_est: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(CODE, default="active")
    region: Mapped[str] = mapped_column(CODE, index=True)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    __table_args__ = (Index("ix_cell_region_tech", "region", "tech"),)


class TopologyNode(Base):
    """Core, aggregation and transmission-link nodes. Sites and cells have their own
    tables but also appear here, so topology queries have one vocabulary."""

    __tablename__ = "topology_node"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    kind: Mapped[str] = mapped_column(CODE, index=True)
    name: Mapped[str] = mapped_column(NAME)
    region: Mapped[str | None] = mapped_column(CODE, index=True)
    capacity_mbps: Mapped[int | None] = mapped_column(Integer)
    redundant: Mapped[bool] = mapped_column(Boolean, default=False)


class TopologyEdge(Base):
    """Directed parent -> child edge. Recursive queries walk this (PRD section 5)."""

    __tablename__ = "topology_edge"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_id: Mapped[str] = mapped_column(ID, index=True)
    child_id: Mapped[str] = mapped_column(ID, index=True)
    parent_kind: Mapped[str] = mapped_column(CODE)
    child_kind: Mapped[str] = mapped_column(CODE)
    relation: Mapped[str] = mapped_column(CODE, default="feeds")

    __table_args__ = (UniqueConstraint("parent_id", "child_id", name="edge"),)


class Alarm(Base):
    __tablename__ = "alarm"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    node_id: Mapped[str] = mapped_column(ID, index=True)
    node_kind: Mapped[str] = mapped_column(CODE)
    cell_id: Mapped[str | None] = mapped_column(ID, index=True)
    site_id: Mapped[str | None] = mapped_column(ID, index=True)
    alarm_type: Mapped[str] = mapped_column(CODE, index=True)
    severity: Mapped[str] = mapped_column(CODE, index=True)
    raised_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    cleared_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    is_flapping: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(CODE, default="nms_mock")
    external_ref: Mapped[str | None] = mapped_column(SHORT)
    details: Mapped[JsonDict | None] = mapped_column(JsonText)

    __table_args__ = (Index("ix_alarm_node_raised", "node_id", "raised_at"),)


class KpiCell(Base):
    """Cell KPIs in wide form, one row per cell per bucket.

    Wide beats long here: it is one row per (cell, bucket) instead of ten, the ML feature
    builder reads it directly, and 30 days of hourly plus 7 days of 15-minute data stays
    inside a size that seeds and queries quickly. `granularity` distinguishes the two
    resolutions required by the PRD.
    """

    __tablename__ = "kpi_cell"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cell_id: Mapped[str] = mapped_column(ID, index=True)
    site_id: Mapped[str] = mapped_column(ID, index=True)
    region: Mapped[str] = mapped_column(CODE)
    bucket_start: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    granularity: Mapped[str] = mapped_column(String(4))  # 15m | 1h

    availability_pct: Mapped[float | None] = mapped_column(Float)
    rrc_setup_success_pct: Mapped[float | None] = mapped_column(Float)
    erab_setup_success_pct: Mapped[float | None] = mapped_column(Float)
    drop_rate_pct: Mapped[float | None] = mapped_column(Float)
    prb_utilisation_pct: Mapped[float | None] = mapped_column(Float)
    dl_throughput_mbps: Mapped[float | None] = mapped_column(Float)
    ul_throughput_mbps: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    active_users: Mapped[float | None] = mapped_column(Float)
    sinr_db: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        UniqueConstraint("cell_id", "bucket_start", "granularity", name="bucket"),
        # Time-bounded, partition-friendly: every query filters granularity + time first.
        Index("ix_kpi_gran_bucket", "granularity", "bucket_start"),
        Index("ix_kpi_cell_gran_bkt", "cell_id", "granularity", "bucket_start"),
    )


class MaintenanceWindow(Base):
    __tablename__ = "maintenance_window"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    node_id: Mapped[str] = mapped_column(ID, index=True)
    node_kind: Mapped[str] = mapped_column(CODE)
    region: Mapped[str | None] = mapped_column(CODE, index=True)
    starts_at: Mapped[datetime] = mapped_column(UtcDateTime)
    ends_at: Mapped[datetime] = mapped_column(UtcDateTime)
    description: Mapped[str | None] = mapped_column(Text)
    external_ref: Mapped[str | None] = mapped_column(SHORT)
    approved_by: Mapped[str | None] = mapped_column(SHORT)

    # Index names are given explicitly wherever the generated name would exceed Oracle's
    # 30-character identifier limit.
    __table_args__ = (
        Index("ix_mw_starts", "starts_at"),
        Index("ix_mw_ends", "ends_at"),
        Index("ix_mw_node_window", "node_id", "starts_at", "ends_at"),
    )


# ================================================================= customers


class Subscriber(Base):
    """Pseudonymised subscriber. The token is the identity (ADR 0008)."""

    __tablename__ = "subscriber"

    id: Mapped[str] = mapped_column(ID, primary_key=True)  # SUB-... pseudonym token
    region: Mapped[str] = mapped_column(CODE, index=True)
    home_cell_id: Mapped[str | None] = mapped_column(ID, index=True)
    last_cell_id: Mapped[str | None] = mapped_column(ID, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    segment: Mapped[str] = mapped_column(CODE, default="consumer")
    is_enterprise: Mapped[bool] = mapped_column(Boolean, default=False)
    enterprise_ref: Mapped[str | None] = mapped_column(SHORT, index=True)
    sla_tier: Mapped[str | None] = mapped_column(CODE)
    opt_out: Mapped[bool] = mapped_column(Boolean, default=False)
    is_roaming: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Which input shape this subscriber's number arrived in
    #: (+2547.., 07.., 2547.., 7..). Kept so normalisation is exercised by real
    #: seeded variety, never the number itself.
    #: Kept so normalisation is exercised by real seeded variety, never the number itself.
    msisdn_format: Mapped[str | None] = mapped_column(CODE)
    created_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class Complaint(Base):
    __tablename__ = "complaint"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    source_ts: Mapped[datetime | None] = mapped_column(UtcDateTime)
    ingested_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    channel: Mapped[str] = mapped_column(CODE, index=True)
    language: Mapped[str] = mapped_column(CODE)
    raw_text: Mapped[str] = mapped_column(Text)
    subscriber_id: Mapped[str | None] = mapped_column(ID, index=True)
    stated_location: Mapped[str | None] = mapped_column(NAME)
    region: Mapped[str | None] = mapped_column(CODE, index=True)
    cell_id: Mapped[str | None] = mapped_column(ID, index=True)
    site_id: Mapped[str | None] = mapped_column(ID, index=True)
    site_confidence: Mapped[float | None] = mapped_column(Float)
    category: Mapped[str | None] = mapped_column(CODE, index=True)
    is_network: Mapped[bool | None] = mapped_column(Boolean)
    sentiment: Mapped[str | None] = mapped_column(CODE)
    urgency: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(CODE, default="received", index=True)
    incident_id: Mapped[str | None] = mapped_column(ID, index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(64), index=True)
    external_ref: Mapped[str | None] = mapped_column(SHORT)
    classifier_version: Mapped[str | None] = mapped_column(CODE)

    __table_args__ = (
        Index("ix_cmp_cell_received", "cell_id", "received_at"),
        Index("ix_cmp_region_received", "region", "received_at"),
    )


class CareCall(Base):
    __tablename__ = "care_call"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    duration_s: Mapped[int] = mapped_column(Integer, default=0)
    subscriber_id: Mapped[str | None] = mapped_column(ID, index=True)
    reason_code: Mapped[str] = mapped_column(CODE, index=True)
    reason_text: Mapped[str | None] = mapped_column(NAME)
    channel: Mapped[str] = mapped_column(CODE, default="call_centre")
    agent_ref: Mapped[str | None] = mapped_column(CODE)
    region: Mapped[str | None] = mapped_column(CODE, index=True)
    cell_id: Mapped[str | None] = mapped_column(ID, index=True)
    is_repeat: Mapped[bool] = mapped_column(Boolean, default=False)
    complaint_id: Mapped[str | None] = mapped_column(ID, index=True)
    avoidable: Mapped[bool | None] = mapped_column(Boolean)

    __table_args__ = (Index("ix_call_region_started", "region", "started_at"),)


# ================================================================= incidents


class Incident(Base):
    __tablename__ = "incident"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    title: Mapped[str] = mapped_column(NAME)
    status: Mapped[str] = mapped_column(CODE, index=True)
    priority: Mapped[str] = mapped_column(String(4), index=True)
    cause: Mapped[str | None] = mapped_column(CODE)
    cause_confidence: Mapped[float | None] = mapped_column(Float)
    region: Mapped[str] = mapped_column(CODE, index=True)
    primary_node_id: Mapped[str | None] = mapped_column(ID, index=True)
    primary_node_kind: Mapped[str | None] = mapped_column(CODE)
    detected_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    affected_cells: Mapped[int] = mapped_column(Integer, default=0)
    affected_sites: Mapped[int] = mapped_column(Integer, default=0)
    affected_subs: Mapped[int] = mapped_column(Integer, default=0)
    enterprise_accounts: Mapped[int] = mapped_column(Integer, default=0)
    complaint_count: Mapped[int] = mapped_column(Integer, default=0)
    sla_risk: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[str | None] = mapped_column(Text)
    suppressed_reason: Mapped[str | None] = mapped_column(NAME)
    detector: Mapped[str] = mapped_column(CODE, default="correlation")
    rule_version: Mapped[str | None] = mapped_column(CODE)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
    #: Optimistic locking: a concurrent update with a stale version is rejected.
    version: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (Index("ix_inc_status_detected", "status", "detected_at"),)


class IncidentLink(Base):
    """What an incident was built from: complaints, alarms, KPI breaches, care calls."""

    __tablename__ = "incident_link"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(ID, index=True)
    entity_type: Mapped[str] = mapped_column(CODE, index=True)
    entity_id: Mapped[str] = mapped_column(ID, index=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    rationale: Mapped[str | None] = mapped_column(NAME)
    linked_at: Mapped[datetime] = mapped_column(UtcDateTime)

    __table_args__ = (UniqueConstraint("incident_id", "entity_type", "entity_id", name="link"),)


class Diagnosis(Base):
    __tablename__ = "diagnosis"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    incident_id: Mapped[str] = mapped_column(ID, index=True)
    probable_cause: Mapped[str] = mapped_column(CODE)
    confidence: Mapped[float] = mapped_column(Float)
    evidence: Mapped[JsonList | None] = mapped_column(JsonText)
    runbook_id: Mapped[str | None] = mapped_column(ID)
    explanation: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(CODE)  # diagnosis_agent | rules | human
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    model_name: Mapped[str | None] = mapped_column(CODE)
    prompt_version: Mapped[str | None] = mapped_column(CODE)
    tokens_used: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)


class CaseRecord(Base):
    """The workflow state machine instance for a complaint or an incident."""

    __tablename__ = "case_record"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    incident_id: Mapped[str | None] = mapped_column(ID, index=True)
    complaint_id: Mapped[str | None] = mapped_column(ID, index=True)
    state: Mapped[str] = mapped_column(CODE, index=True)
    priority: Mapped[str] = mapped_column(String(4), index=True)
    priority_score: Mapped[float | None] = mapped_column(Float)
    assigned_team: Mapped[str | None] = mapped_column(CODE, index=True)
    assigned_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    routing_reason: Mapped[str | None] = mapped_column(NAME)
    decision_inputs: Mapped[JsonDict | None] = mapped_column(JsonText)
    rule_version: Mapped[str | None] = mapped_column(CODE)
    model_version: Mapped[str | None] = mapped_column(CODE)
    sla_due_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Runbook(Base):
    __tablename__ = "runbook"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    title: Mapped[str] = mapped_column(NAME)
    cause: Mapped[str] = mapped_column(CODE, index=True)
    risk_tier: Mapped[str] = mapped_column(CODE)  # low | medium | high
    steps: Mapped[JsonList] = mapped_column(JsonText)
    rollback: Mapped[JsonList | None] = mapped_column(JsonText)
    expected_effect: Mapped[str | None] = mapped_column(Text)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    owner: Mapped[str | None] = mapped_column(CODE)
    updated_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class RecoveryAction(Base):
    """A proposed (never executed) recovery, with its approval trail."""

    __tablename__ = "recovery_action"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    incident_id: Mapped[str] = mapped_column(ID)
    runbook_id: Mapped[str] = mapped_column(ID, index=True)
    status: Mapped[str] = mapped_column(CODE, index=True)
    risk_tier: Mapped[str] = mapped_column(CODE)
    simulated_outcome: Mapped[JsonDict | None] = mapped_column(JsonText)
    proposed_by: Mapped[str] = mapped_column(CODE)
    proposed_at: Mapped[datetime] = mapped_column(UtcDateTime)
    approved_by: Mapped[str | None] = mapped_column(SHORT)
    approved_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    rejected_reason: Mapped[str | None] = mapped_column(NAME)
    rollback_of: Mapped[str | None] = mapped_column(ID)

    __table_args__ = (Index("ix_recov_incident", "incident_id"),)


class Verification(Base):
    __tablename__ = "verification"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    incident_id: Mapped[str] = mapped_column(ID, index=True)
    checked_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    kpi_recovered: Mapped[bool] = mapped_column(Boolean)
    alarms_cleared: Mapped[bool] = mapped_column(Boolean)
    quiet_period_ok: Mapped[bool] = mapped_column(Boolean)
    complaints_after: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, index=True)
    detail: Mapped[JsonDict | None] = mapped_column(JsonText)


class Notification(Base):
    __tablename__ = "notification"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    incident_id: Mapped[str | None] = mapped_column(ID, index=True)
    subscriber_id: Mapped[str | None] = mapped_column(ID, index=True)
    template_id: Mapped[str] = mapped_column(CODE, index=True)
    template_version: Mapped[str | None] = mapped_column(CODE)
    slots: Mapped[JsonDict | None] = mapped_column(JsonText)
    rendered_text: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(CODE, default="en")
    channel: Mapped[str] = mapped_column(CODE, default="sms")
    status: Mapped[str] = mapped_column(CODE, index=True)
    proposed_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    approved_by: Mapped[str | None] = mapped_column(SHORT)
    approved_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    suppressed_reason: Mapped[str | None] = mapped_column(NAME)
    dedupe_key: Mapped[str | None] = mapped_column(String(64), index=True)
    gateway_mode: Mapped[str] = mapped_column(CODE, default="mock")


# =========================================================== ml and knowledge


class KnowledgeDoc(Base):
    __tablename__ = "knowledge_doc"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    title: Mapped[str] = mapped_column(NAME)
    body: Mapped[str] = mapped_column(Text)
    doc_type: Mapped[str] = mapped_column(CODE, index=True)  # sop | runbook | faq | note
    source: Mapped[str | None] = mapped_column(NAME)
    owner: Mapped[str] = mapped_column(CODE)
    valid_from: Mapped[datetime | None] = mapped_column(UtcDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)
    tags: Mapped[JsonList | None] = mapped_column(JsonText)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[JsonList | None] = mapped_column(Embedding)
    embedding_model: Mapped[str | None] = mapped_column(CODE)
    updated_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class Prediction(Base):
    __tablename__ = "prediction"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_name: Mapped[str] = mapped_column(CODE, index=True)
    model_version: Mapped[str] = mapped_column(CODE)
    entity_type: Mapped[str] = mapped_column(CODE)
    entity_id: Mapped[str] = mapped_column(ID, index=True)
    target: Mapped[str] = mapped_column(CODE)
    horizon_minutes: Mapped[int | None] = mapped_column(Integer)
    predicted_for: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)
    value: Mapped[float] = mapped_column(Float)
    lower: Mapped[float | None] = mapped_column(Float)
    upper: Mapped[float | None] = mapped_column(Float)
    features: Mapped[JsonDict | None] = mapped_column(JsonText)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class ModelRegistry(Base):
    __tablename__ = "model_registry"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    name: Mapped[str] = mapped_column(CODE, index=True)
    version: Mapped[str] = mapped_column(CODE)
    algorithm: Mapped[str] = mapped_column(CODE)
    trained_at: Mapped[datetime] = mapped_column(UtcDateTime)
    metrics: Mapped[JsonDict | None] = mapped_column(JsonText)
    params: Mapped[JsonDict | None] = mapped_column(JsonText)
    feature_list: Mapped[JsonList | None] = mapped_column(JsonText)
    data_hash: Mapped[str | None] = mapped_column(String(64))
    artifact_path: Mapped[str | None] = mapped_column(NAME)
    card_path: Mapped[str | None] = mapped_column(NAME)
    beats_baseline: Mapped[bool | None] = mapped_column(Boolean)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    __table_args__ = (UniqueConstraint("name", "version", name="model_ver"),)


class EvalRun(Base):
    __tablename__ = "eval_run"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    suite: Mapped[str] = mapped_column(CODE, index=True)
    llm_mode: Mapped[str] = mapped_column(CODE)  # fake | live
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    metrics: Mapped[JsonDict | None] = mapped_column(JsonText)
    thresholds: Mapped[JsonDict | None] = mapped_column(JsonText)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    prompt_versions: Mapped[JsonDict | None] = mapped_column(JsonText)
    notes: Mapped[str | None] = mapped_column(Text)


# ================================================== platform / audit / events


class AuditLog(Base):
    """Append-only. Nothing in the application updates or deletes a row here."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    actor: Mapped[str] = mapped_column(SHORT, index=True)
    actor_role: Mapped[str | None] = mapped_column(CODE)
    action: Mapped[str] = mapped_column(CODE, index=True)
    entity_type: Mapped[str | None] = mapped_column(CODE)
    entity_id: Mapped[str | None] = mapped_column(ID, index=True)
    outcome: Mapped[str] = mapped_column(CODE)
    inputs: Mapped[JsonDict | None] = mapped_column(JsonText)
    rule_version: Mapped[str | None] = mapped_column(CODE)
    model_version: Mapped[str | None] = mapped_column(CODE)
    prompt_version: Mapped[str | None] = mapped_column(CODE)
    confidence: Mapped[float | None] = mapped_column(Float)
    evidence: Mapped[JsonList | None] = mapped_column(JsonText)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    client_ip: Mapped[str | None] = mapped_column(CODE)


class OutboxEvent(Base):
    """Transactional outbox (ADR 0003). Written in the same transaction as the change."""

    __tablename__ = "outbox_event"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    event_type: Mapped[str] = mapped_column(CODE, index=True)
    event_version: Mapped[int] = mapped_column(Integer, default=1)
    aggregate_type: Mapped[str] = mapped_column(CODE)
    aggregate_id: Mapped[str] = mapped_column(ID, index=True)
    payload: Mapped[JsonDict] = mapped_column(JsonText)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    available_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    status: Mapped[str] = mapped_column(CODE, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(NAME)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    dedupe_key: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("dedupe_key", name="outbox_dedupe"),
        Index("ix_outbox_status_avail", "status", "available_at"),
        Index("ix_outbox_corr", "correlation_id"),
    )


class ScheduledReport(Base):
    __tablename__ = "scheduled_report"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    kind: Mapped[str] = mapped_column(CODE, index=True)
    schedule_local: Mapped[str] = mapped_column(CODE)  # e.g. "07:30" or "mon 08:00"
    tz: Mapped[str] = mapped_column(CODE, default="Africa/Nairobi")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    next_run_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_status: Mapped[str | None] = mapped_column(CODE)
    last_error: Mapped[str | None] = mapped_column(NAME)
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    recipients: Mapped[str | None] = mapped_column(NAME)  # channel name, never a person

    __table_args__ = (
        Index("ix_sched_next_run", "next_run_at"),
        Index("ix_sched_idem", "idempotency_key"),
    )


class ReportDelivery(Base):
    """One attempt to deliver a report; failures land in the dead-letter state."""

    __tablename__ = "report_delivery"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    report_id: Mapped[str] = mapped_column(ID, index=True)
    kind: Mapped[str] = mapped_column(CODE)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    card: Mapped[JsonDict | None] = mapped_column(JsonText)
    status: Mapped[str] = mapped_column(CODE, index=True)  # dry_run|sent|failed|dead_letter
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(NAME)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    __table_args__ = (UniqueConstraint("idempotency_key", name="delivery_idem"),)


# ============================================== discovery (explorer + catalog)


class SavedQuery(Base):
    __tablename__ = "saved_query"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    name: Mapped[str] = mapped_column(NAME)
    sql_text: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(NAME)
    created_by: Mapped[str] = mapped_column(SHORT, index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (UniqueConstraint("created_by", "name", name="saved_name"),)


class QueryAudit(Base):
    """Every Data Explorer query, accepted or refused (PRD section 7)."""

    __tablename__ = "query_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    executed_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    actor: Mapped[str] = mapped_column(SHORT, index=True)
    actor_role: Mapped[str | None] = mapped_column(CODE)
    sql_hash: Mapped[str] = mapped_column(String(64), index=True)
    sql_text: Mapped[str] = mapped_column(Text)
    accepted: Mapped[bool] = mapped_column(Boolean, index=True)
    reject_reason: Mapped[str | None] = mapped_column(NAME)
    row_count: Mapped[int | None] = mapped_column(Integer)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    correlation_id: Mapped[str | None] = mapped_column(String(64))


class GroundTruth(Base):
    """Seeded answers, kept apart from the pipeline on purpose.

    Scenario labels say what the seed *built*; the pipeline must derive the same thing
    from observations alone. Only the seed writes here and only evals/scenario tests read
    here - a leakage test asserts that no module imports this table.
    """

    __tablename__ = "ground_truth"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scenario_id: Mapped[str] = mapped_column(CODE, index=True)
    scenario_kind: Mapped[str] = mapped_column(CODE, index=True)
    entity_type: Mapped[str] = mapped_column(CODE, index=True)
    entity_id: Mapped[str] = mapped_column(ID, index=True)
    label: Mapped[str] = mapped_column(CODE)
    region: Mapped[str | None] = mapped_column(CODE)
    starts_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    ends_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    detail: Mapped[JsonDict | None] = mapped_column(JsonText)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class SeedRun(Base):
    """Provenance for a seeded dataset: the same inputs must reproduce the same data."""

    __tablename__ = "seed_run"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    seed: Mapped[int] = mapped_column(Integer)
    days: Mapped[int] = mapped_column(Integer)
    fine_days: Mapped[int] = mapped_column(Integer)
    generator_version: Mapped[str] = mapped_column(CODE)
    anchor_at: Mapped[datetime] = mapped_column(UtcDateTime)
    row_counts: Mapped[JsonDict | None] = mapped_column(JsonText)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


#: Tables the Data Explorer may read. Anything not listed is refused by the SQL guard,
#: even for an admin: the allowlist is the control, not the role.
EXPLORER_ALLOWLIST: frozenset[str] = frozenset(
    {
        "site",
        "cell",
        "topology_node",
        "topology_edge",
        "alarm",
        "kpi_cell",
        "maintenance_window",
        "subscriber",
        "complaint",
        "care_call",
        "incident",
        "incident_link",
        "diagnosis",
        "case_record",
        "runbook",
        "recovery_action",
        "verification",
        "notification",
        "knowledge_doc",
        "prediction",
        "model_registry",
        "eval_run",
        "scheduled_report",
        "report_delivery",
    },
)

#: Columns that must be masked in explorer results and CSV exports unless the caller
#: holds an explicit unmask capability (nobody does in v1).
PII_COLUMNS: frozenset[str] = frozenset(
    {
        "subscriber_id",
        "enterprise_ref",
        "raw_text",
        "reason_text",
        "rendered_text",
        "stated_location",
        "agent_ref",
        "approved_by",
        "actor",
        "client_ip",
        "recipients",
    },
)
