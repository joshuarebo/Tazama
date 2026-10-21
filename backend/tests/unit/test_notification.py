"""Notification module: templates, quiet hours, opt-out, caps, storm guard, approval.

PRD section 15 edge cases named here:
  * "notification storms (rate limits, quiet hours, opt-out)" — the storm guard, the
    per-incident/daily caps, the dedupe window and the absolute opt-out.
  * "UTC vs Africa/Nairobi" — the quiet-hours boundaries at 20:59/21:00 and 06:29/06:30
    local, and the midnight wrap, all driven from UTC instants.
  * "clock skew" — an approval that ages into quiet hours before it is sent.
  * "prompt injection inside complaints" — slot values carrying template or markup syntax.
  * "Swahili/Sheng/code-switched text" — the `sw` body of every shipped template.
  * "empty or very long complaint text" — empty and over-long slot values.
  * "duplicate and out-of-order events" — deterministic dedupe keys and idempotent saves.
  * "MSISDN format variants" — every Kenyan shape refused in a slot value.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tazama.core.clock import FixedClock
from tazama.core.errors import Conflict, Forbidden, NotFound, PolicyViolation, ValidationFailed
from tazama.core.policy import Policies, load_policies
from tazama.core.security import Principal
from tazama.domain.events import DomainEvent, EventType
from tazama.infrastructure.adapters.sms.mock import MockSmsGateway
from tazama.modules.notification.models import (
    MAX_SLOT_CHARS,
    AudienceMember,
    IncidentBrief,
    NotificationProposal,
    NotificationStatus,
    SuppressionReason,
    Template,
)
from tazama.modules.notification.service import NotificationService
from tazama.modules.notification.templates import (
    FileTemplateRepository,
    InMemoryTemplateRepository,
    clean_slot_value,
    load_templates,
    render,
)

INCIDENT = IncidentBrief(id="INC_TEST_1", region="Nairobi", status="mitigating")
SLOTS = {"service": "data", "area": "Westlands", "eta": "18:00"}

PROPOSER = Principal(subject="care-1", role="care_agent", display_name="Care One")
APPROVER = Principal(subject="mgr-1", role="manager", display_name="Manager One")
NOC = Principal(subject="noc-1", role="noc_engineer", display_name="NOC One")
ANALYST = Principal(subject="an-1", role="analyst")

#: 2026-03-17 14:30 Africa/Nairobi — comfortably outside quiet hours.
DAYTIME = datetime(2026, 3, 17, 11, 30, tzinfo=UTC)


# ------------------------------------------------------------------- fakes


@dataclass(slots=True)
class FakeRepo:
    rows: dict[str, NotificationProposal] = field(default_factory=dict)

    def save(self, proposal: NotificationProposal) -> NotificationProposal:
        self.rows[proposal.id] = proposal
        return proposal

    def get(self, notification_id: str) -> NotificationProposal | None:
        return self.rows.get(notification_id)

    def for_incident(self, incident_id: str) -> tuple[NotificationProposal, ...]:
        return tuple(r for r in self.rows.values() if r.incident_id == incident_id)

    def _counted(self) -> tuple[NotificationProposal, ...]:
        live = {
            NotificationStatus.PROPOSED,
            NotificationStatus.APPROVED,
            NotificationStatus.SENT,
        }
        return tuple(r for r in self.rows.values() if r.status in live)

    def count_for_subscriber_in_incident(self, subscriber_id: str, incident_id: str) -> int:
        return sum(
            1
            for r in self._counted()
            if r.subscriber_id == subscriber_id and r.incident_id == incident_id
        )

    def count_for_subscriber_since(self, subscriber_id: str, since: datetime) -> int:
        return sum(
            1
            for r in self._counted()
            if r.subscriber_id == subscriber_id and r.proposed_at >= since
        )

    def find_recent_duplicate(
        self,
        *,
        subscriber_id: str,
        template_id: str,
        incident_id: str,
        since: datetime,
    ) -> NotificationProposal | None:
        for row in self._counted():
            if (
                row.subscriber_id == subscriber_id
                and row.template_id == template_id
                and row.incident_id == incident_id
                and row.proposed_at >= since
            ):
                return row
        return None

    def count_proposed_since(self, since: datetime) -> int:
        return sum(1 for r in self._counted() if r.proposed_at >= since)


@dataclass(slots=True)
class FakeAudience:
    members: tuple[AudienceMember, ...] = ()

    def for_incident(self, incident_id: str, *, limit: int = 1000) -> tuple[AudienceMember, ...]:
        return self.members[:limit]

    def get(self, subscriber_id: str) -> AudienceMember | None:
        return next((m for m in self.members if m.subscriber_id == subscriber_id), None)

    def is_opted_out(self, subscriber_id: str) -> bool:
        member = self.get(subscriber_id)
        return True if member is None else member.opt_out


@dataclass(slots=True)
class RecordingPublisher:
    events: list[DomainEvent] = field(default_factory=list)

    def publish(self, event: DomainEvent) -> bool:
        self.events.append(event)
        return True

    def types(self) -> list[EventType]:
        return [e.event_type for e in self.events]

    def of(self, event_type: EventType) -> list[DomainEvent]:
        return [e for e in self.events if e.event_type is event_type]


@dataclass(slots=True)
class RecordingDecisions:
    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        outcome: str,
        inputs: dict[str, Any],
        evidence: Any = (),
        confidence: float | None = None,
        rule_version: str | None = None,
    ) -> None:
        self.entries.append(
            {
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "outcome": outcome,
                "inputs": inputs,
                "evidence": tuple(evidence),
                "confidence": confidence,
                "rule_version": rule_version,
            },
        )

    def actions(self) -> list[str]:
        return [e["action"] for e in self.entries]

    def outcomes(self) -> list[str]:
        return [e["outcome"] for e in self.entries]


def token(n: int) -> str:
    """A pseudonym token of the shape `Pseudonymiser` produces."""
    body = f"{n:016d}".translate(str.maketrans("0123456789", "ABCDEFGHIJ"))
    return f"SUB-{body}"


def member(n: int, *, opt_out: bool = False, language: str = "en") -> AudienceMember:
    return AudienceMember(subscriber_id=token(n), opt_out=opt_out, language=language)


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def policies() -> Policies:
    return load_policies()


@pytest.fixture
def templates() -> FileTemplateRepository:
    return FileTemplateRepository()


@dataclass(slots=True)
class Harness:
    service: NotificationService
    repo: FakeRepo
    audience: FakeAudience
    gateway: MockSmsGateway
    publisher: RecordingPublisher
    decisions: RecordingDecisions
    clock: FixedClock


def build(
    *,
    policies: Policies,
    templates: Any,
    members: tuple[AudienceMember, ...] = (),
    now: datetime = DAYTIME,
    gateway: MockSmsGateway | None = None,
) -> Harness:
    repo = FakeRepo()
    audience = FakeAudience(members=members)
    publisher = RecordingPublisher()
    decisions = RecordingDecisions()
    clock = FixedClock(now)
    gw = gateway or MockSmsGateway()
    service = NotificationService(
        repository=repo,
        audience=audience,
        templates=templates,
        gateway=gw,
        publisher=publisher,
        decisions=decisions,
        policies=policies,
        clock=clock,
    )
    return Harness(service, repo, audience, gw, publisher, decisions, clock)


def with_quiet_hours(policies: Policies, **kwargs: Any) -> Policies:
    return replace(policies, notification=replace(policies.notification, **kwargs))


# ============================================================== templates


def test_shipped_templates_load_and_declare_both_languages(templates: FileTemplateRepository):
    ids = {t.id for t in templates.all()}
    assert ids == {"known_issue", "resolved", "maintenance"}
    for template in templates.all():
        assert "en" in template.languages
        assert "sw" in template.languages
        assert template.approval_required is True
        assert template.version


def test_swahili_bodies_are_real_swahili(templates: FileTemplateRepository):
    """Not machine filler: each body uses Swahili words an operator would actually send."""
    known = templates.get("known_issue")
    assert known is not None
    assert "Tunafahamu" in known.languages["sw"]
    assert "Pole kwa usumbufu" in known.languages["sw"]
    resolved = templates.get("resolved")
    assert resolved is not None
    assert "imerekebishwa" in resolved.languages["sw"]
    assert "Asante kwa uvumilivu" in resolved.languages["sw"]
    maintenance = templates.get("maintenance")
    assert maintenance is not None
    assert "Matengenezo yaliyopangwa" in maintenance.languages["sw"]


def test_every_shipped_template_renders_inside_its_sms_budget(templates: FileTemplateRepository):
    slots = {
        "known_issue": SLOTS,
        "resolved": {"service": "data", "area": "Westlands", "resolved_at": "17:45"},
        "maintenance": {
            "area": "Westlands, Nairobi",
            "window": "Sat 22:00-02:00",
            "service": "data and voice",
        },
    }
    for template in templates.all():
        for language in ("en", "sw"):
            text = render(template, language=language, slots=slots[template.id])
            assert len(text) <= template.max_length
            parts = max(1, -(-len(text) // 160))
            assert parts <= template.max_parts <= 2


def test_render_refuses_an_unknown_slot(templates: FileTemplateRepository):
    template = templates.get("known_issue")
    assert template is not None
    with pytest.raises(ValidationFailed, match="no slot"):
        render(template, language="en", slots={**SLOTS, "operator": "me"})


def test_render_refuses_a_missing_required_slot(templates: FileTemplateRepository):
    template = templates.get("known_issue")
    assert template is not None
    with pytest.raises(ValidationFailed, match="requires slot"):
        render(template, language="en", slots={"service": "data", "area": "Westlands"})


def test_render_refuses_an_unavailable_language(templates: FileTemplateRepository):
    template = templates.get("known_issue")
    assert template is not None
    with pytest.raises(ValidationFailed, match="no body for language"):
        render(template, language="fr", slots=SLOTS)


@pytest.mark.parametrize(
    "value",
    [
        "{{eta}}",
        "{eta}",
        "<script>alert(1)</script>",
        "<b>now</b>",
        "${PATH}",
        "%(x)s",
        "back`tick`",
        "[[wiki]]",
    ],
)
def test_slot_value_with_template_or_markup_syntax_is_refused(value: str):
    """PRD section 15: "prompt injection inside complaints" — slots are not a payload."""
    with pytest.raises(ValidationFailed):
        clean_slot_value("area", value)


@pytest.mark.parametrize(
    "value",
    ["+254712345678", "254712345678", "0712345678", "0110123456", "+254 712 345 678"],
)
def test_slot_value_that_looks_like_an_msisdn_is_refused(value: str):
    """PRD section 15: "MSISDN format variants" — none of them belong in a message."""
    with pytest.raises(ValidationFailed, match="subscriber identifier"):
        clean_slot_value("area", value)


def test_empty_and_overlong_slot_values_are_refused():
    """PRD section 15: "empty or very long complaint text"."""
    with pytest.raises(ValidationFailed, match="must not be empty"):
        clean_slot_value("area", "   ")
    with pytest.raises(ValidationFailed, match="over the"):
        clean_slot_value("area", "x" * (MAX_SLOT_CHARS + 1))


def test_slot_value_must_be_a_string_and_single_line():
    with pytest.raises(ValidationFailed, match="must be a string"):
        clean_slot_value("area", 42)
    with pytest.raises(ValidationFailed, match="single line"):
        clean_slot_value("area", "Westlands\nKilimani")


def test_rendering_is_substitution_not_evaluation(templates: FileTemplateRepository):
    """A value naming another slot stays literal text: substitution does not recurse."""
    template = templates.get("known_issue")
    assert template is not None
    text = render(template, language="en", slots={**SLOTS, "area": "eta zone 0:10"})
    assert "eta zone 0:10" in text
    assert text.count("18:00") == 1


def test_template_file_with_three_parts_is_refused(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "id: bad\nversion: '1'\nchannel: sms\nmax_parts: 3\nmax_length: 480\n"
        "required_slots: [a]\nlanguages:\n  en: 'x {{a}}'\n  sw: 'y {{a}}'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="max_parts"):
        load_templates(tmp_path)


def test_template_file_missing_swahili_is_refused(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "id: bad\nversion: '1'\nchannel: sms\nmax_length: 160\nrequired_slots: [a]\n"
        "languages:\n  en: 'x {{a}}'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="missing required language"):
        load_templates(tmp_path)


def test_template_file_with_undeclared_slot_in_body_is_refused(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "id: bad\nversion: '1'\nchannel: sms\nmax_length: 160\nrequired_slots: [a]\n"
        "languages:\n  en: 'x {{a}} {{b}}'\n  sw: 'y {{a}} {{b}}'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="slot mismatch"):
        load_templates(tmp_path)


def test_template_file_opting_out_of_approval_is_refused(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "id: bad\nversion: '1'\nchannel: sms\nmax_length: 160\napproval_required: false\n"
        "required_slots: [a]\nlanguages:\n  en: 'x {{a}}'\n  sw: 'y {{a}}'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="approval_required"):
        load_templates(tmp_path)


def test_template_file_with_an_unsupported_channel_is_refused(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "id: bad\nversion: '1'\nchannel: whatsapp\nmax_length: 160\nrequired_slots: [a]\n"
        "languages:\n  en: 'x {{a}}'\n  sw: 'y {{a}}'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="channel"):
        load_templates(tmp_path)


# ================================================================ propose


def test_propose_creates_one_proposal_per_member(policies, templates):
    h = build(policies=policies, templates=templates, members=(member(1), member(2)))
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    assert len(batch) == 2
    assert {p.status for p in batch.proposals} == {NotificationStatus.PROPOSED}
    assert h.publisher.types().count(EventType.NOTIFICATION_PROPOSED) == 2


def test_propose_requires_the_propose_capability(policies, templates):
    h = build(policies=policies, templates=templates, members=(member(1),))
    with pytest.raises(Forbidden):
        h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=ANALYST)


def test_propose_refuses_an_unknown_template(policies, templates):
    h = build(policies=policies, templates=templates, members=(member(1),))
    with pytest.raises(ValidationFailed, match="Unknown notification template"):
        h.service.propose(INCIDENT, template_id="free_text", slots=SLOTS, principal=PROPOSER)


def test_propose_reads_the_audience_through_the_port_when_none_is_passed(policies, templates):
    h = build(policies=policies, templates=templates, members=(member(1), member(2), member(3)))
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    assert len(batch) == 3


def test_propose_records_a_decision_with_the_policy_version(policies, templates):
    h = build(policies=policies, templates=templates, members=(member(1),))
    h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    entry = h.decisions.entries[0]
    assert entry["action"] == "notification_propose"
    assert entry["rule_version"] == policies.rule_version
    assert entry["inputs"]["template_version"]


def test_proposal_stores_no_pii_beyond_the_pseudonym_token(policies, templates):
    """A stored notification must contain nothing MSISDN-shaped, in any field."""
    import re

    msisdn = re.compile(r"(?<!\d)(?:\+?254|0)(?:7|1)\d{8}(?!\d)")
    h = build(policies=policies, templates=templates, members=(member(1),))
    batch = h.service.propose(
        INCIDENT,
        template_id="known_issue",
        slots={"service": "data", "area": "Westlands", "eta": "18:00"},
        principal=PROPOSER,
    )
    row = batch.proposals[0].to_row()
    blob = " ".join(str(value) for value in row.values())
    assert not msisdn.search(blob)
    assert row["subscriber_id"].startswith("SUB-")
    assert not any(ch.isdigit() for ch in row["subscriber_id"][4:])


def test_proposal_renders_the_members_language(policies, templates):
    h = build(policies=policies, templates=templates, members=(member(1, language="sw"),))
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    assert batch.proposals[0].language == "sw"
    assert "Tunafahamu" in batch.proposals[0].rendered_text


def test_proposal_falls_back_to_english_for_an_unsupported_language(policies, templates):
    h = build(policies=policies, templates=templates, members=(member(1, language="sheng"),))
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    assert batch.proposals[0].language == "en"


def test_dedupe_key_is_deterministic_so_a_replay_is_a_no_op(policies, templates):
    """PRD section 15: "duplicate and out-of-order events"."""
    h1 = build(policies=policies, templates=templates, members=(member(1),))
    h2 = build(policies=policies, templates=templates, members=(member(1),))
    first = h1.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    second = h2.service.propose(
        INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER
    )
    assert first.proposals[0].dedupe_key == second.proposals[0].dedupe_key


def test_an_audience_over_the_per_call_limit_is_refused(policies, templates):
    h = build(policies=policies, templates=templates)
    huge = tuple(member(i) for i in range(5_001))
    with pytest.raises(ValidationFailed, match="per-call"):
        h.service.propose(
            INCIDENT, huge, template_id="known_issue", slots=SLOTS, principal=PROPOSER
        )


# ============================================================== opt-out


def test_an_opted_out_subscriber_is_never_proposed_to(policies, templates):
    h = build(
        policies=policies,
        templates=templates,
        members=(member(1, opt_out=True), member(2)),
    )
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    assert [p.subscriber_id for p in batch.proposals] == [token(2)]
    assert SuppressionReason.OPT_OUT in batch.reasons()


def test_opt_out_suppression_is_recorded_and_published(policies, templates):
    h = build(policies=policies, templates=templates, members=(member(1, opt_out=True),))
    h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    suppressed = h.publisher.of(EventType.NOTIFICATION_SUPPRESSED)
    assert len(suppressed) == 1
    assert suppressed[0].payload["reason"] == str(SuppressionReason.OPT_OUT)
    assert "notification_suppress" in h.decisions.actions()


def test_opt_out_is_consulted_through_the_port_even_when_the_member_says_otherwise(
    policies, templates
):
    """The audience port is the authority: a stale member flag cannot override it."""
    h = build(policies=policies, templates=templates, members=(member(1, opt_out=True),))
    stale = (replace(member(1), opt_out=False),)
    batch = h.service.propose(
        INCIDENT, stale, template_id="known_issue", slots=SLOTS, principal=PROPOSER
    )
    assert len(batch) == 0
    assert batch.reasons() == (SuppressionReason.OPT_OUT,)


# ========================================================== quiet hours


@pytest.mark.parametrize(
    ("utc_hour", "utc_minute", "local", "quiet"),
    [
        (17, 59, "20:59", False),
        (18, 0, "21:00", True),
        (21, 0, "00:00", True),
        (3, 29, "06:29", True),
        (3, 30, "06:30", False),
    ],
)
def test_quiet_hours_boundaries_in_the_policy_timezone(
    policies, templates, utc_hour, utc_minute, local, quiet
):
    """PRD section 15: "UTC vs Africa/Nairobi". Nairobi is UTC+3, so 21:00 local is 18:00Z."""
    now = datetime(2026, 3, 17, utc_hour, utc_minute, tzinfo=UTC)
    h = build(policies=policies, templates=templates, members=(member(1),), now=now)
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    if quiet:
        assert len(batch) == 0, f"{local} local should be inside quiet hours"
        assert batch.reasons() == (SuppressionReason.QUIET_HOURS,)
    else:
        assert len(batch) == 1, f"{local} local should be outside quiet hours"


def test_quiet_hours_wrap_midnight(policies, templates):
    """02:00 local is between 21:00 and 06:30, which only holds if `covers` wraps."""
    now = datetime(2026, 3, 17, 23, 0, tzinfo=UTC)  # 02:00 Africa/Nairobi
    h = build(policies=policies, templates=templates, members=(member(1),), now=now)
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    assert len(batch) == 0
    assert batch.reasons() == (SuppressionReason.QUIET_HOURS,)


def test_quiet_hours_suppression_names_the_window_and_timezone(policies, templates):
    now = datetime(2026, 3, 17, 19, 0, tzinfo=UTC)
    h = build(policies=policies, templates=templates, members=(member(1),), now=now)
    h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    event = h.publisher.of(EventType.NOTIFICATION_SUPPRESSED)[0]
    assert "Africa/Nairobi" in event.payload["detail"]
    assert "21:00" in event.payload["detail"]


def test_quiet_hours_suppress_the_whole_batch(policies, templates):
    now = datetime(2026, 3, 17, 19, 0, tzinfo=UTC)
    members = tuple(member(i) for i in range(1, 6))
    h = build(policies=policies, templates=templates, members=members, now=now)
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    assert len(batch) == 0
    assert len(batch.suppressions) == 5


# ===================================================== caps and dedupe


def test_per_incident_cap_is_enforced(policies, templates):
    h = build(policies=policies, templates=templates, members=(member(1),))
    first = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    assert len(first) == 1
    second = h.service.propose(
        INCIDENT,
        template_id="resolved",
        slots={
            "service": "data",
            "area": "Westlands",
            "resolved_at": "17:45",
        },
        principal=PROPOSER,
    )
    assert len(second) == 0
    assert second.reasons() == (SuppressionReason.PER_INCIDENT_CAP,)


def test_dedupe_window_blocks_the_same_template_for_the_same_incident(policies, templates):
    relaxed = with_quiet_hours(policies, max_per_incident=5)
    h = build(policies=relaxed, templates=templates, members=(member(1),))
    h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    again = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    assert again.reasons() == (SuppressionReason.DEDUPE_WINDOW,)


def test_outside_the_dedupe_window_the_same_template_is_allowed_again(policies, templates):
    relaxed = with_quiet_hours(policies, max_per_incident=5, max_per_subscriber_per_day=5)
    h = build(policies=relaxed, templates=templates, members=(member(1),))
    h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    # A full day later: past the 12-hour dedupe window and back in the Nairobi afternoon,
    # so quiet hours do not mask what this test is asserting.
    assert relaxed.notification.dedupe_window_hours < 24
    h.clock.advance(timedelta(days=1))
    again = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    assert len(again) == 1


def test_daily_cap_is_enforced_across_incidents(policies, templates):
    relaxed = with_quiet_hours(policies, max_per_incident=1, max_per_subscriber_per_day=2)
    h = build(policies=relaxed, templates=templates, members=(member(1),))
    for n in range(1, 4):
        incident = replace(INCIDENT, id=f"INC_{n}")
        batch = h.service.propose(
            incident, template_id="known_issue", slots=SLOTS, principal=PROPOSER
        )
        if n <= 2:
            assert len(batch) == 1, f"incident {n} should be allowed"
        else:
            assert batch.reasons() == (SuppressionReason.DAILY_CAP,)


def test_a_suppressed_proposal_does_not_consume_the_daily_allowance(policies, templates):
    h = build(policies=policies, templates=templates, members=(member(1, opt_out=True),))
    h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    assert h.repo.count_for_subscriber_since(token(1), DAYTIME - timedelta(days=1)) == 0


# =========================================================== storm guard


def test_storm_guard_stops_a_fan_out_that_would_breach_the_ceiling(policies, templates):
    tight = with_quiet_hours(policies, max_per_minute=3)
    members = tuple(member(i) for i in range(1, 6))
    h = build(policies=tight, templates=templates, members=members)
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    assert batch.storm_guard_tripped is True
    assert len(batch) == 0
    assert h.repo.rows == {}


def test_storm_guard_publishes_a_suppression_with_the_breach_action(policies, templates):
    tight = with_quiet_hours(policies, max_per_minute=1)
    h = build(policies=tight, templates=templates, members=(member(1), member(2)))
    h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    event = h.publisher.of(EventType.NOTIFICATION_SUPPRESSED)[0]
    assert event.payload["reason"] == str(SuppressionReason.STORM_GUARD)
    assert event.payload["breach_action"] == "pause_and_alert"
    assert event.payload["suppressed_count"] == 2


def test_storm_guard_records_a_decision(policies, templates):
    tight = with_quiet_hours(policies, max_per_minute=1)
    h = build(policies=tight, templates=templates, members=(member(1), member(2)))
    h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    assert h.decisions.outcomes() == [str(SuppressionReason.STORM_GUARD)]


def test_storm_guard_counts_proposals_already_made_this_minute(policies, templates):
    tight = with_quiet_hours(policies, max_per_minute=2, max_per_incident=5)
    h = build(policies=tight, templates=templates, members=(member(1),))
    assert (
        len(h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER))
        == 1
    )
    second = h.service.propose(
        replace(INCIDENT, id="INC_2"),
        template_id="known_issue",
        slots=SLOTS,
        principal=PROPOSER,
    )
    assert len(second) == 1
    third = h.service.propose(
        replace(INCIDENT, id="INC_3"),
        template_id="known_issue",
        slots=SLOTS,
        principal=PROPOSER,
    )
    assert third.storm_guard_tripped is True


def test_the_default_policy_ceiling_is_not_reachable_by_a_single_call(policies, templates):
    """A storm must be impossible to trigger accidentally: the per-call cap bites first."""
    assert policies.notification.max_per_minute == 200
    h = build(policies=policies, templates=templates)
    members = tuple(member(i) for i in range(201))
    batch = h.service.propose(
        INCIDENT, members, template_id="known_issue", slots=SLOTS, principal=PROPOSER
    )
    assert batch.storm_guard_tripped is True


# ============================================================== approval


def test_approve_requires_the_approve_capability(policies, templates):
    h = build(policies=policies, templates=templates, members=(member(1),))
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    with pytest.raises(Forbidden):
        h.service.approve(batch.proposals[0].id, PROPOSER)


def test_approve_requires_a_role_the_policy_names(policies, templates):
    narrowed = replace(
        policies,
        approvals={
            **policies.approvals,
            "notification_send": replace(
                policies.approval_for("notification_send"), roles=("admin",)
            ),
        },
    )
    h = build(policies=narrowed, templates=templates, members=(member(1),))
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=PROPOSER)
    with pytest.raises(PolicyViolation, match="may not approve"):
        h.service.approve(batch.proposals[0].id, APPROVER)


def test_self_approval_is_refused_when_the_policy_names_two_approvers(policies, templates):
    h = build(policies=policies, templates=templates, members=(member(1),))
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=NOC)
    assert len(policies.approval_for("notification_send").roles) > 1
    with pytest.raises(PolicyViolation, match="proposed yourself"):
        h.service.approve(batch.proposals[0].id, NOC)


def test_a_different_approver_may_approve(policies, templates):
    h = build(policies=policies, templates=templates, members=(member(1),))
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=NOC)
    approved = h.service.approve(batch.proposals[0].id, APPROVER)
    assert approved.status is NotificationStatus.APPROVED
    assert approved.approved_by == APPROVER.subject
    assert h.publisher.of(EventType.NOTIFICATION_APPROVED)


def test_approving_twice_is_a_conflict(policies, templates):
    h = build(policies=policies, templates=templates, members=(member(1),))
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=NOC)
    h.service.approve(batch.proposals[0].id, APPROVER)
    with pytest.raises(Conflict):
        h.service.approve(batch.proposals[0].id, APPROVER)


def test_approving_an_unknown_notification_is_not_found(policies, templates):
    h = build(policies=policies, templates=templates)
    with pytest.raises(NotFound):
        h.service.approve("NTF_missing", APPROVER)


def test_reject_requires_a_reason_and_publishes_the_event(policies, templates):
    h = build(policies=policies, templates=templates, members=(member(1),))
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=NOC)
    with pytest.raises(ValidationFailed, match="reason is required"):
        h.service.reject(batch.proposals[0].id, APPROVER, "  ")
    rejected = h.service.reject(batch.proposals[0].id, APPROVER, "wrong area named")
    assert rejected.status is NotificationStatus.REJECTED
    assert rejected.rejected_reason == "wrong area named"
    assert h.publisher.of(EventType.NOTIFICATION_REJECTED)


def test_a_rejected_proposal_cannot_be_approved(policies, templates):
    h = build(policies=policies, templates=templates, members=(member(1),))
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=NOC)
    h.service.reject(batch.proposals[0].id, APPROVER, "not accurate")
    with pytest.raises(Conflict):
        h.service.approve(batch.proposals[0].id, APPROVER)


# ================================================================== send


def _approved(policies: Policies, templates: Any, **kwargs: Any) -> tuple[Harness, str]:
    h = build(policies=policies, templates=templates, members=(member(1),), **kwargs)
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=NOC)
    approved = h.service.approve(batch.proposals[0].id, APPROVER)
    return h, approved.id


def test_sending_without_approval_raises_policy_violation(policies, templates):
    h = build(policies=policies, templates=templates, members=(member(1),))
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=NOC)
    with pytest.raises(PolicyViolation, match="after human approval"):
        h.service.send_approved(batch.proposals[0].id)


def test_send_goes_through_the_gateway_and_publishes_sent(policies, templates):
    h, notification_id = _approved(policies, templates)
    sent = h.service.send_approved(notification_id)
    assert sent.status is NotificationStatus.SENT
    assert len(h.gateway.sent) == 1
    assert h.gateway.sent[0].destination == token(1)
    assert h.publisher.of(EventType.NOTIFICATION_SENT)


def test_the_mock_gateway_performs_no_io_and_records_what_would_be_sent(policies, templates):
    h, notification_id = _approved(policies, templates)
    h.service.send_approved(notification_id)
    message = h.gateway.last()
    assert message is not None
    assert message.template_id == "known_issue"
    assert "Westlands" in message.text


def test_send_refuses_a_gateway_mode_other_than_mock(policies, templates):
    class LiveMode(MockSmsGateway):
        def mode(self) -> str:
            return "live"

    h, notification_id = _approved(policies, templates, gateway=LiveMode())
    with pytest.raises(PolicyViolation, match="not supported in v1"):
        h.service.send_approved(notification_id)


def test_an_approval_that_ages_into_quiet_hours_is_suppressed_not_sent(policies, templates):
    """PRD section 15: "clock skew" — the window is judged at send time, not at approval."""
    h, notification_id = _approved(policies, templates)
    h.clock.set(datetime(2026, 3, 17, 19, 0, tzinfo=UTC))  # 22:00 Africa/Nairobi
    with pytest.raises(PolicyViolation, match="Quiet hours"):
        h.service.send_approved(notification_id)
    assert h.gateway.sent == []
    stored = h.repo.get(notification_id)
    assert stored is not None
    assert stored.status is NotificationStatus.SUPPRESSED
    assert stored.suppressed_reason is SuppressionReason.QUIET_HOURS


def test_sending_twice_is_refused(policies, templates):
    h, notification_id = _approved(policies, templates)
    h.service.send_approved(notification_id)
    with pytest.raises(PolicyViolation):
        h.service.send_approved(notification_id)
    assert len(h.gateway.sent) == 1


def test_send_records_the_receipt_on_the_decision_row(policies, templates):
    h, notification_id = _approved(policies, templates)
    h.service.send_approved(notification_id)
    entry = next(e for e in h.decisions.entries if e["action"] == "notification_send")
    assert entry["inputs"]["gateway_mode"] == "mock"
    assert entry["inputs"]["receipt_id"]
    assert entry["inputs"]["approved_by"] == APPROVER.subject


def test_the_full_happy_path_emits_the_events_in_order(policies, templates):
    h, notification_id = _approved(policies, templates)
    h.service.send_approved(notification_id)
    assert h.publisher.types() == [
        EventType.NOTIFICATION_PROPOSED,
        EventType.NOTIFICATION_APPROVED,
        EventType.NOTIFICATION_SENT,
    ]


def test_saving_the_same_proposal_twice_is_idempotent(policies, templates):
    """PRD section 15: "duplicate and out-of-order events"."""
    h = build(policies=policies, templates=templates, members=(member(1),))
    batch = h.service.propose(INCIDENT, template_id="known_issue", slots=SLOTS, principal=NOC)
    proposal = batch.proposals[0]
    h.repo.save(proposal)
    h.repo.save(proposal)
    assert len(h.repo.rows) == 1


def test_templates_only_cannot_be_switched_off_in_the_policy_file(policies):
    """The loader refuses `templates_only: false`, so there is no "free text" mode."""
    assert policies.notification.templates_only is True
    assert policies.notification.respect_opt_out is True


def test_in_memory_template_repository_satisfies_the_same_port(policies):
    template = Template(
        id="known_issue",
        version="t",
        channel="sms",
        languages={"en": "Hi {{area}}", "sw": "Habari {{area}}"},
        required_slots=("area",),
        max_length=160,
    )
    h = build(
        policies=policies,
        templates=InMemoryTemplateRepository({"known_issue": template}),
        members=(member(1),),
    )
    batch = h.service.propose(
        INCIDENT, template_id="known_issue", slots={"area": "Kilimani"}, principal=PROPOSER
    )
    assert batch.proposals[0].rendered_text == "Hi Kilimani"
