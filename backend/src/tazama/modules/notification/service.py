"""Proactive customer notification: propose, approve, send — in that order, always.

The product value is telling a customer about an outage before they call. The risk is
telling two million customers something wrong at 03:00. So this service is a sequence of
refusals with a narrow path through the middle:

    1. authorise the caller (capability, decided server-side)
    2. storm guard — a system-wide ceiling on proposals per minute
    3. quiet hours — evaluated in the policy's timezone against a UTC instant
    4. per-subscriber gates — opt-out, per-incident cap, daily cap, dedupe window
    5. render from an approved template (never free text)
    6. human approval, with separation of duties
    7. send through the SMS port, in `mock` mode only in v1

Every refusal publishes `NotificationSuppressed` and writes a decision row: a message that
was *not* sent is as auditable as one that was.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tazama.core.clock import Clock, to_display
from tazama.core.errors import Conflict, NotFound, PolicyViolation, ValidationFailed
from tazama.core.ids import deterministic_id, get_correlation_id, new_id
from tazama.core.logging import get_logger
from tazama.core.policy import ApprovalRule, Policies
from tazama.core.security import Principal
from tazama.domain.events import (
    AggregateType,
    DomainEvent,
    NotificationApproved,
    NotificationProposed,
    NotificationRejected,
    NotificationSent,
    NotificationSuppressed,
)
from tazama.modules.notification.models import (
    AudienceMember,
    IncidentBrief,
    NotificationProposal,
    NotificationStatus,
    ProposalBatch,
    SmsMessage,
    Suppression,
    SuppressionReason,
)
from tazama.modules.notification.ports import (
    DecisionLog,
    EventPublisher,
    NotificationRepository,
    SmsGateway,
    SubscriberAudience,
    TemplateRepository,
)
from tazama.modules.notification.templates import render

_log = get_logger(__name__)

#: The only SMS gateway mode v1 supports. A live gateway is a separate, reviewed change.
SUPPORTED_GATEWAY_MODE = "mock"

#: Action name in `config/policies.yaml` that governs sending.
APPROVAL_ACTION = "notification_send"

ENTITY_TYPE = "notification"

#: Upper bound on one `propose()` call, independent of the policy. A single call that fans
#: out to more subscribers than this is a bug in the caller, not a large incident.
MAX_AUDIENCE_PER_CALL = 5_000

_STORM_WINDOW = timedelta(minutes=1)


class NotificationService:
    def __init__(
        self,
        *,
        repository: NotificationRepository,
        audience: SubscriberAudience,
        templates: TemplateRepository,
        gateway: SmsGateway,
        publisher: EventPublisher,
        decisions: DecisionLog,
        policies: Policies,
        clock: Clock,
    ) -> None:
        self._repo = repository
        self._audience = audience
        self._templates = templates
        self._gateway = gateway
        self._publisher = publisher
        self._decisions = decisions
        self._policies = policies
        self._clock = clock

    # ------------------------------------------------------------- propose

    def propose(
        self,
        incident: IncidentBrief,
        audience: Sequence[AudienceMember] | None = None,
        *,
        template_id: str,
        slots: Mapping[str, object],
        principal: Principal,
    ) -> ProposalBatch:
        """Propose one notification per eligible subscriber. Nothing is sent here.

        `audience` may be supplied (the caller already has the members) or omitted, in
        which case it is read through the `SubscriberAudience` port.
        """
        principal.require("notification:propose")
        policy = self._policies.notification
        template = self._templates.get(template_id)
        if template is None:
            raise ValidationFailed(
                f"Unknown notification template {template_id!r}. Messages may only be sent "
                f"from an approved template.",
            )

        members = (
            tuple(audience)
            if audience is not None
            else self._audience.for_incident(incident.id, limit=MAX_AUDIENCE_PER_CALL)
        )
        if len(members) > MAX_AUDIENCE_PER_CALL:
            raise ValidationFailed(
                f"Audience of {len(members)} exceeds the {MAX_AUDIENCE_PER_CALL} per-call "
                f"limit; split the incident's audience into batches.",
            )

        now = self._clock.now()

        # 2. Storm guard, before anything is rendered or written. Counting the candidates
        #    in with the recent total is what makes an accidental storm impossible: a
        #    fan-out that *would* breach the ceiling never starts.
        recent = self._repo.count_proposed_since(now - _STORM_WINDOW)
        if recent + len(members) > policy.max_per_minute:
            return self._trip_storm_guard(
                incident,
                template_id=template_id,
                candidates=len(members),
                recent=recent,
                principal=principal,
            )

        # 3. Quiet hours apply to the whole batch: a proactive message is not urgent enough
        #    to wake anyone, and "urgent" is not a field a caller gets to set.
        if self._in_quiet_hours():
            suppressions = tuple(
                Suppression(
                    subscriber_id=member.subscriber_id,
                    incident_id=incident.id,
                    template_id=template_id,
                    reason=SuppressionReason.QUIET_HOURS,
                    detail=self._quiet_hours_detail(),
                )
                for member in members
            )
            self._record_batch_suppression(
                incident,
                template_id=template_id,
                reason=SuppressionReason.QUIET_HOURS,
                detail=self._quiet_hours_detail(),
                count=len(members),
                principal=principal,
            )
            return ProposalBatch(suppressions=suppressions)

        proposals: list[NotificationProposal] = []
        suppressed: list[Suppression] = []
        for member in members:
            reason, detail = self._gate(member, incident, template_id=template_id, now=now)
            if reason is not None:
                suppressed.append(
                    Suppression(
                        subscriber_id=member.subscriber_id,
                        incident_id=incident.id,
                        template_id=template_id,
                        reason=reason,
                        detail=detail,
                    ),
                )
                self._suppress(
                    incident,
                    subscriber_id=member.subscriber_id,
                    template_id=template_id,
                    reason=reason,
                    detail=detail,
                    principal=principal,
                )
                continue

            language = member.language if template.body(member.language) else "en"
            text = render(template, language=language, slots=slots)
            proposal = NotificationProposal(
                id=new_id("NTF", self._clock),
                incident_id=incident.id,
                subscriber_id=member.subscriber_id,
                template_id=template.id,
                template_version=template.version,
                language=language,
                channel=template.channel,
                slots={name: str(slots[name]) for name in template.required_slots},
                rendered_text=text,
                status=NotificationStatus.PROPOSED,
                proposed_at=now,
                proposed_by=principal.subject,
                dedupe_key=self._dedupe_key(incident.id, member.subscriber_id, template.id),
                gateway_mode=self._gateway.mode(),
            )
            stored = self._repo.save(proposal)
            proposals.append(stored)
            self._publish(
                NotificationProposed(
                    aggregate_type=AggregateType.NOTIFICATION,
                    aggregate_id=stored.id,
                    occurred_at=now,
                    dedupe_key=stored.dedupe_key,
                    correlation_id=get_correlation_id(),
                    payload={
                        "incident_id": incident.id,
                        "subscriber_id": stored.subscriber_id,
                        "template_id": stored.template_id,
                        "template_version": stored.template_version,
                        "language": stored.language,
                        "parts": stored.parts,
                    },
                ),
            )
            self._decisions.record(
                action="notification_propose",
                entity_type=ENTITY_TYPE,
                entity_id=stored.id,
                outcome="proposed",
                inputs={
                    "incident_id": incident.id,
                    "subscriber_id": stored.subscriber_id,
                    "template_id": stored.template_id,
                    "template_version": stored.template_version,
                    "language": stored.language,
                    "slots": dict(stored.slots),
                },
                rule_version=self._policies.rule_version,
            )
        return ProposalBatch(proposals=tuple(proposals), suppressions=tuple(suppressed))

    # ------------------------------------------------------------- approval

    def approve(self, notification_id: str, principal: Principal) -> NotificationProposal:
        """Human approval. Required by policy; never granted to the proposer."""
        principal.require("notification:approve")
        rule = self._policies.approval_for(APPROVAL_ACTION)
        if rule.roles and principal.role not in rule.roles:
            raise PolicyViolation(
                f"Role {principal.role!r} may not approve a customer notification "
                f"(policy allows: {', '.join(rule.roles)}).",
            )
        proposal = self._require(notification_id)
        if proposal.status is not NotificationStatus.PROPOSED:
            raise Conflict(
                f"Notification {notification_id} is {proposal.status} and can no longer be "
                f"approved.",
            )
        if _names_two_approvers(rule) and proposal.proposed_by == principal.subject:
            raise PolicyViolation(
                "You may not approve a notification you proposed yourself; the policy "
                "names more than one approver so that two people see the message.",
            )

        now = self._clock.now()
        approved = self._repo.save(proposal.approved(by=principal.subject, at=now))
        self._publish(
            NotificationApproved(
                aggregate_type=AggregateType.NOTIFICATION,
                aggregate_id=approved.id,
                occurred_at=now,
                dedupe_key=f"{approved.id}:approved",
                correlation_id=get_correlation_id(),
                payload={
                    "incident_id": approved.incident_id,
                    "approved_by": principal.subject,
                    "template_id": approved.template_id,
                },
            ),
        )
        self._decisions.record(
            action="notification_approve",
            entity_type=ENTITY_TYPE,
            entity_id=approved.id,
            outcome="approved",
            inputs={
                "approver": principal.subject,
                "approver_role": principal.role,
                "proposed_by": approved.proposed_by,
                "policy_roles": list(rule.roles),
            },
            rule_version=self._policies.rule_version,
        )
        return approved

    def reject(
        self, notification_id: str, principal: Principal, reason: str
    ) -> NotificationProposal:
        """Rejection is a decision too, and it needs a reason an auditor can read."""
        principal.require("notification:approve")
        rule = self._policies.approval_for(APPROVAL_ACTION)
        if rule.roles and principal.role not in rule.roles:
            raise PolicyViolation(
                f"Role {principal.role!r} may not decide a customer notification "
                f"(policy allows: {', '.join(rule.roles)}).",
            )
        clean_reason = reason.strip()
        if not clean_reason:
            raise ValidationFailed("A rejection reason is required.")
        proposal = self._require(notification_id)
        if proposal.status is not NotificationStatus.PROPOSED:
            raise Conflict(
                f"Notification {notification_id} is {proposal.status} and can no longer be "
                f"rejected.",
            )

        now = self._clock.now()
        rejected = self._repo.save(
            proposal.rejected(by=principal.subject, at=now, reason=clean_reason[:200]),
        )
        self._publish(
            NotificationRejected(
                aggregate_type=AggregateType.NOTIFICATION,
                aggregate_id=rejected.id,
                occurred_at=now,
                dedupe_key=f"{rejected.id}:rejected",
                correlation_id=get_correlation_id(),
                payload={
                    "incident_id": rejected.incident_id,
                    "rejected_by": principal.subject,
                    "reason": rejected.rejected_reason,
                },
            ),
        )
        self._decisions.record(
            action="notification_reject",
            entity_type=ENTITY_TYPE,
            entity_id=rejected.id,
            outcome="rejected",
            inputs={"approver": principal.subject, "reason": rejected.rejected_reason},
            rule_version=self._policies.rule_version,
        )
        return rejected

    # ---------------------------------------------------------------- send

    def send_approved(self, notification_id: str) -> NotificationProposal:
        """Hand an approved message to the SMS port. Refuses everything else."""
        proposal = self._require(notification_id)
        if proposal.status is not NotificationStatus.APPROVED:
            raise PolicyViolation(
                f"Notification {notification_id} is {proposal.status}: a customer message "
                f"is only ever sent after human approval.",
            )
        mode = self._gateway.mode()
        if mode != SUPPORTED_GATEWAY_MODE:
            raise PolicyViolation(
                f"SMS gateway mode {mode!r} is not supported in v1; only "
                f"{SUPPORTED_GATEWAY_MODE!r} may send.",
            )
        if self._in_quiet_hours():
            # Approval can be minutes or hours old. Quiet hours are judged at send time.
            suppressed = self._repo.save(
                proposal.suppressed(reason=SuppressionReason.QUIET_HOURS),
            )
            self._suppress_existing(suppressed, detail=self._quiet_hours_detail())
            raise PolicyViolation(
                f"Quiet hours are in force ({self._quiet_hours_detail()}); the approved "
                f"notification was suppressed rather than sent.",
            )

        now = self._clock.now()
        receipt = self._gateway.send(
            SmsMessage(
                destination=proposal.subscriber_id,
                text=proposal.rendered_text,
                template_id=proposal.template_id,
                language=proposal.language,
                reference=proposal.id,
                metadata={"incident_id": proposal.incident_id, "mode": mode},
            ),
        )
        sent = self._repo.save(proposal.sent(at=now, receipt_id=receipt.id))
        self._publish(
            NotificationSent(
                aggregate_type=AggregateType.NOTIFICATION,
                aggregate_id=sent.id,
                occurred_at=now,
                dedupe_key=f"{sent.id}:sent",
                correlation_id=get_correlation_id(),
                payload={
                    "incident_id": sent.incident_id,
                    "subscriber_id": sent.subscriber_id,
                    "template_id": sent.template_id,
                    "gateway_mode": mode,
                    "receipt_id": receipt.id,
                    "receipt_status": receipt.status,
                },
            ),
        )
        self._decisions.record(
            action="notification_send",
            entity_type=ENTITY_TYPE,
            entity_id=sent.id,
            outcome="sent",
            inputs={
                "gateway_mode": mode,
                "receipt_id": receipt.id,
                "receipt_status": receipt.status,
                "approved_by": sent.approved_by,
            },
            rule_version=self._policies.rule_version,
        )
        return sent

    # ------------------------------------------------------------- helpers

    def _require(self, notification_id: str) -> NotificationProposal:
        proposal = self._repo.get(notification_id)
        if proposal is None:
            raise NotFound(f"Unknown notification {notification_id}.")
        return proposal

    def _in_quiet_hours(self) -> bool:
        """Quiet hours live in the policy's timezone; the instant we hold is UTC.

        Converting before comparing is the whole point: at 21:30 Africa/Nairobi the UTC
        clock reads 18:30, and a UTC comparison would send into the night.
        """
        quiet = self._policies.notification.quiet_hours
        local = to_display(self._clock.now(), ZoneInfo(quiet.tz))
        return quiet.covers(local.time())

    def _quiet_hours_detail(self) -> str:
        quiet = self._policies.notification.quiet_hours
        return (
            f"quiet hours {quiet.start.strftime('%H:%M')}-{quiet.end.strftime('%H:%M')} {quiet.tz}"
        )

    def _gate(
        self,
        member: AudienceMember,
        incident: IncidentBrief,
        *,
        template_id: str,
        now: datetime,
    ) -> tuple[SuppressionReason | None, str]:
        """Per-subscriber checks, cheapest and most absolute first."""
        policy = self._policies.notification
        # One instant for the whole batch: two members of the same fan-out must not land
        # in different dedupe windows because a second ticked over mid-loop.
        instant = now

        opted_out = member.opt_out or self._audience.is_opted_out(member.subscriber_id)
        if policy.respect_opt_out and opted_out:
            # Absolute. There is no urgency flag that overrides an opt-out.
            return SuppressionReason.OPT_OUT, "subscriber has opted out of proactive messages"

        in_incident = self._repo.count_for_subscriber_in_incident(member.subscriber_id, incident.id)
        if in_incident >= policy.max_per_incident:
            return (
                SuppressionReason.PER_INCIDENT_CAP,
                f"{in_incident} already sent for this incident (cap {policy.max_per_incident})",
            )

        since_day = instant - timedelta(days=1)
        per_day = self._repo.count_for_subscriber_since(member.subscriber_id, since_day)
        if per_day >= policy.max_per_subscriber_per_day:
            return (
                SuppressionReason.DAILY_CAP,
                f"{per_day} in the last 24h (cap {policy.max_per_subscriber_per_day})",
            )

        window_start = instant - timedelta(hours=policy.dedupe_window_hours)
        duplicate = self._repo.find_recent_duplicate(
            subscriber_id=member.subscriber_id,
            template_id=template_id,
            incident_id=incident.id,
            since=window_start,
        )
        if duplicate is not None:
            return (
                SuppressionReason.DEDUPE_WINDOW,
                f"duplicate of {duplicate.id} inside the "
                f"{policy.dedupe_window_hours}h dedupe window",
            )
        return None, ""

    def _trip_storm_guard(
        self,
        incident: IncidentBrief,
        *,
        template_id: str,
        candidates: int,
        recent: int,
        principal: Principal,
    ) -> ProposalBatch:
        policy = self._policies.notification
        detail = (
            f"storm guard: {recent} proposals in the last minute plus {candidates} "
            f"candidates would exceed {policy.max_per_minute}/min; "
            f"action={policy.storm_breach_action}"
        )
        _log.error("notification storm guard tripped", extra={"detail": detail})
        self._record_batch_suppression(
            incident,
            template_id=template_id,
            reason=SuppressionReason.STORM_GUARD,
            detail=detail,
            count=candidates,
            principal=principal,
        )
        return ProposalBatch(storm_guard_tripped=True)

    def _record_batch_suppression(
        self,
        incident: IncidentBrief,
        *,
        template_id: str,
        reason: SuppressionReason,
        detail: str,
        count: int,
        principal: Principal,
    ) -> None:
        now = self._clock.now()
        self._publish(
            NotificationSuppressed(
                aggregate_type=AggregateType.INCIDENT,
                aggregate_id=incident.id,
                occurred_at=now,
                dedupe_key=f"{incident.id}:{template_id}:{reason}",
                correlation_id=get_correlation_id(),
                payload={
                    "reason": str(reason),
                    "detail": detail,
                    "template_id": template_id,
                    "suppressed_count": count,
                    "breach_action": self._policies.notification.storm_breach_action,
                },
            ),
        )
        self._decisions.record(
            action="notification_suppress",
            entity_type="incident",
            entity_id=incident.id,
            outcome=str(reason),
            inputs={
                "template_id": template_id,
                "detail": detail,
                "suppressed_count": count,
                "actor": principal.subject,
            },
            rule_version=self._policies.rule_version,
        )

    def _suppress(
        self,
        incident: IncidentBrief,
        *,
        subscriber_id: str,
        template_id: str,
        reason: SuppressionReason,
        detail: str,
        principal: Principal,
    ) -> None:
        now = self._clock.now()
        self._publish(
            NotificationSuppressed(
                aggregate_type=AggregateType.INCIDENT,
                aggregate_id=incident.id,
                occurred_at=now,
                dedupe_key=f"{incident.id}:{subscriber_id}:{template_id}:{reason}",
                correlation_id=get_correlation_id(),
                payload={
                    "reason": str(reason),
                    "detail": detail,
                    "subscriber_id": subscriber_id,
                    "template_id": template_id,
                },
            ),
        )
        self._decisions.record(
            action="notification_suppress",
            entity_type="incident",
            entity_id=incident.id,
            outcome=str(reason),
            inputs={
                "subscriber_id": subscriber_id,
                "template_id": template_id,
                "detail": detail,
                "actor": principal.subject,
            },
            rule_version=self._policies.rule_version,
        )

    def _suppress_existing(self, proposal: NotificationProposal, *, detail: str) -> None:
        reason = proposal.suppressed_reason or SuppressionReason.QUIET_HOURS
        now = self._clock.now()
        self._publish(
            NotificationSuppressed(
                aggregate_type=AggregateType.NOTIFICATION,
                aggregate_id=proposal.id,
                occurred_at=now,
                dedupe_key=f"{proposal.id}:{reason}",
                correlation_id=get_correlation_id(),
                payload={
                    "reason": str(reason),
                    "detail": detail,
                    "incident_id": proposal.incident_id,
                    "subscriber_id": proposal.subscriber_id,
                },
            ),
        )
        self._decisions.record(
            action="notification_suppress",
            entity_type=ENTITY_TYPE,
            entity_id=proposal.id,
            outcome=str(reason),
            inputs={"detail": detail, "incident_id": proposal.incident_id},
            rule_version=self._policies.rule_version,
        )

    def _publish(self, event: DomainEvent) -> None:
        self._publisher.publish(event)

    @staticmethod
    def _dedupe_key(incident_id: str, subscriber_id: str, template_id: str) -> str:
        """Stable across retries: the same fact yields the same key, so a replay is a no-op."""
        return deterministic_id("NDK", incident_id, subscriber_id, template_id)


def _names_two_approvers(rule: ApprovalRule) -> bool:
    """Whether the policy expects separation of duties for this action.

    `config/policies.yaml` expresses this two ways: an explicit two-approver risk tier, or
    an approver list naming more than one role. Either way the intent is that a second pair
    of eyes sees the message, which a proposer approving their own work defeats.
    """
    return rule.required and (bool(rule.two_approver_risk_tiers) or len(rule.roles) > 1)
