"""DTOs for the notification module (PRD section 8, "Notification").

Plain data only: the service builds these, the API serialises them, and a repository
adapter maps them onto the `notification` table.

Two invariants are encoded here rather than left to convention:
  * a proposal carries a **pseudonym token** as its only subscriber identity — there is no
    field that could hold an MSISDN, so a leak needs a schema change, not a mistake;
  * message text is never assembled by the caller: `rendered_text` is produced by
    `templates.render()` from an approved template plus slot *values*.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

#: One SMS part on GSM-7. A longer message is concatenated, and each part is billed and
#: can arrive out of order, so the templates cap themselves well below the limit.
SMS_PART_CHARS = 160

#: Hard ceiling on parts, whatever a template file asks for. Three-part customer SMS is a
#: support incident of its own.
MAX_SMS_PARTS = 2

#: Slot values are short by construction: a cell name, an area, a time. Anything longer is
#: free text wearing a slot's clothes.
MAX_SLOT_CHARS = 60


class NotificationStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT = "sent"
    SUPPRESSED = "suppressed"


class SuppressionReason(StrEnum):
    """Why a subscriber was not notified. Recorded, never silent."""

    OPT_OUT = "opt_out"
    QUIET_HOURS = "quiet_hours"
    DEDUPE_WINDOW = "dedupe_window"
    PER_INCIDENT_CAP = "per_incident_cap"
    DAILY_CAP = "daily_cap"
    STORM_GUARD = "storm_guard"


@dataclass(frozen=True, slots=True)
class Template:
    """An approved message template. The only legal source of customer-facing text."""

    id: str
    version: str
    channel: str
    #: language code -> body with `{{slot}}` placeholders. `en` and `sw` are mandatory.
    languages: Mapping[str, str]
    required_slots: tuple[str, ...]
    max_length: int
    approval_required: bool = True
    max_parts: int = 1
    description: str = ""

    def body(self, language: str) -> str | None:
        return self.languages.get(language)

    @property
    def slot_names(self) -> frozenset[str]:
        return frozenset(self.required_slots)


@dataclass(frozen=True, slots=True)
class AudienceMember:
    """One notifiable subscriber, already pseudonymised upstream."""

    subscriber_id: str
    language: str = "en"
    opt_out: bool = False
    region: str | None = None
    cell_id: str | None = None
    sla_tier: str | None = None


@dataclass(frozen=True, slots=True)
class IncidentBrief:
    """The incident facts a notification needs. Passed in; never fetched from another module."""

    id: str
    region: str
    status: str
    cause: str | None = None
    default_language: str = "en"
    affected_elements: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NotificationProposal:
    """A proposed customer message and its approval trail.

    Immutable: every state change returns a new value via `approved`/`rejected`/`sent`, so
    a stale copy cannot be written back over a newer one.
    """

    id: str
    incident_id: str
    subscriber_id: str
    template_id: str
    template_version: str
    language: str
    channel: str
    slots: Mapping[str, str]
    rendered_text: str
    status: NotificationStatus
    proposed_at: datetime
    proposed_by: str
    dedupe_key: str
    gateway_mode: str = "mock"
    approved_by: str | None = None
    approved_at: datetime | None = None
    rejected_reason: str | None = None
    sent_at: datetime | None = None
    suppressed_reason: SuppressionReason | None = None
    gateway_receipt_id: str | None = None

    @property
    def parts(self) -> int:
        """How many SMS parts the rendered text occupies."""
        return max(1, -(-len(self.rendered_text) // SMS_PART_CHARS))

    def approved(self, *, by: str, at: datetime) -> NotificationProposal:
        return replace(self, status=NotificationStatus.APPROVED, approved_by=by, approved_at=at)

    def rejected(self, *, by: str, at: datetime, reason: str) -> NotificationProposal:
        return replace(
            self,
            status=NotificationStatus.REJECTED,
            approved_by=by,
            approved_at=at,
            rejected_reason=reason,
        )

    def sent(self, *, at: datetime, receipt_id: str) -> NotificationProposal:
        return replace(
            self, status=NotificationStatus.SENT, sent_at=at, gateway_receipt_id=receipt_id
        )

    def suppressed(self, *, reason: SuppressionReason) -> NotificationProposal:
        return replace(self, status=NotificationStatus.SUPPRESSED, suppressed_reason=reason)

    def to_row(self) -> dict[str, Any]:
        """Flat shape the `notification` table stores."""
        return {
            "id": self.id,
            "incident_id": self.incident_id,
            "subscriber_id": self.subscriber_id,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "slots": dict(self.slots),
            "rendered_text": self.rendered_text,
            "language": self.language,
            "channel": self.channel,
            "status": str(self.status),
            "proposed_at": self.proposed_at,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "sent_at": self.sent_at,
            "suppressed_reason": (
                str(self.suppressed_reason) if self.suppressed_reason is not None else None
            ),
            "dedupe_key": self.dedupe_key,
            "gateway_mode": self.gateway_mode,
        }


@dataclass(frozen=True, slots=True)
class Suppression:
    """One subscriber who was deliberately not notified."""

    subscriber_id: str
    incident_id: str
    template_id: str
    reason: SuppressionReason
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ProposalBatch:
    """The outcome of one `propose()` call: what was proposed and what was held back."""

    proposals: tuple[NotificationProposal, ...] = ()
    suppressions: tuple[Suppression, ...] = ()
    storm_guard_tripped: bool = False

    def __iter__(self) -> Iterator[NotificationProposal]:
        """A batch iterates over its proposals, so callers can treat it as the list."""
        return iter(self.proposals)

    def __len__(self) -> int:
        return len(self.proposals)

    def reasons(self) -> tuple[SuppressionReason, ...]:
        return tuple(s.reason for s in self.suppressions)


@dataclass(frozen=True, slots=True)
class SmsMessage:
    """What the gateway port is handed. `destination` is a pseudonym token, never a number."""

    destination: str
    text: str
    template_id: str
    language: str
    reference: str
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SendReceipt:
    """Gateway acknowledgement. `id` and `status` are the contract both adapters honour."""

    id: str
    status: str
    provider: str
    accepted: bool = True
    detail: str = ""
