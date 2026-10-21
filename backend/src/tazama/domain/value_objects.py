"""Pure value objects. No I/O, no framework, no ambient time.

The MSISDN rules here are the single definition used by ingestion, the seed, the care
lookup and the tests. Kenyan numbering (PRD section 8): country code 254, mobile prefixes
7xx (and 1xx, allocated later), written locally as 07xx/01xx and internationally as
+2547xx/2541xx.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Self

# --------------------------------------------------------------------- MSISDN

COUNTRY_CODE: Final = "254"
#: Kenyan mobile national significant number: 7xxxxxxxx or 1xxxxxxxx (9 digits).
_NSN = re.compile(r"^(?:7|1)\d{8}$")
_NON_DIGITS = re.compile(r"[^\d+]")


class MsisdnError(ValueError):
    """The input is not a usable Kenyan mobile number."""


@dataclass(frozen=True, slots=True, order=True)
class Msisdn:
    """A normalised Kenyan MSISDN in E.164 form without the plus (`2547xxxxxxxx`).

    Construct with `Msisdn.parse`. The raw value is deliberately *not* kept: raw
    identifiers must not travel past the ingestion boundary (ADR 0008).
    """

    value: str

    def __post_init__(self) -> None:
        if not re.fullmatch(rf"{COUNTRY_CODE}(?:7|1)\d{{8}}", self.value):
            raise MsisdnError(f"not a normalised Kenyan MSISDN: {self.value!r}")

    @classmethod
    def parse(cls, raw: str) -> Self:
        """Accept the formats seen in the wild and return one canonical value.

        Accepted: `+254712345678`, `254712345678`, `0712345678`, `712345678`,
        `0110123456`, and the same with spaces, dashes, brackets or NBSPs.
        Rejected: anything else — including landlines, short codes and foreign numbers,
        which are routed to human review rather than guessed at.
        """
        if raw is None:  # type: ignore[comparison-overlap]
            raise MsisdnError("MSISDN is required")
        text = unicodedata.normalize("NFKC", str(raw)).strip()
        # Strip every space the channels send us, non-breaking ones included.
        text = text.replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
        text = _NON_DIGITS.sub("", text)
        if not text:
            raise MsisdnError("MSISDN is empty")
        text = text.removeprefix("+")
        text = text.removeprefix("00")

        if text.startswith(COUNTRY_CODE):
            nsn = text[len(COUNTRY_CODE) :]
        elif text.startswith("0"):
            nsn = text[1:]
        else:
            nsn = text

        if not _NSN.fullmatch(nsn):
            raise MsisdnError(f"not a Kenyan mobile number: {raw!r}")
        return cls(value=f"{COUNTRY_CODE}{nsn}")

    @classmethod
    def try_parse(cls, raw: str) -> Self | None:
        """Parse, or None. Used where an unparseable number is data, not an error."""
        try:
            return cls.parse(raw)
        except MsisdnError:
            return None

    @property
    def national(self) -> str:
        """Local format, e.g. `0712345678` — for display to a care agent only."""
        return "0" + self.value[len(COUNTRY_CODE) :]

    @property
    def e164(self) -> str:
        return f"+{self.value}"

    def __str__(self) -> str:
        return self.value


# ------------------------------------------------------------------ geography


@dataclass(frozen=True, slots=True)
class GeoPoint:
    """WGS84 coordinate. Kenya spans roughly 34-42E, -5 to 5N; we validate globally and
    let the seed stay inside Kenya."""

    lat: float
    lon: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.lat <= 90.0:
            raise ValueError(f"latitude out of range: {self.lat}")
        if not -180.0 <= self.lon <= 180.0:
            raise ValueError(f"longitude out of range: {self.lon}")

    def distance_km(self, other: GeoPoint) -> float:
        """Great-circle distance (haversine). Correlation uses this for clustering."""
        radius_km = 6371.0088
        phi1, phi2 = math.radians(self.lat), math.radians(other.lat)
        d_phi = phi2 - phi1
        d_lambda = math.radians(other.lon - self.lon)
        a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
        return 2 * radius_km * math.asin(math.sqrt(a))


class Region(StrEnum):
    """Seeded regions (PRD section 7). Live mode maps real regions via source mappings."""

    NAIROBI = "Nairobi"
    MOMBASA = "Mombasa"
    KISUMU = "Kisumu"
    NAKURU = "Nakuru"
    ELDORET = "Eldoret"


# ------------------------------------------------------------------- network


class Technology(StrEnum):
    G2 = "2G"
    G3 = "3G"
    G4 = "4G"
    G5 = "5G"


class NodeKind(StrEnum):
    """Topology node kinds, core -> cell (PRD section 7)."""

    CORE = "core"
    AGGREGATION = "aggregation"
    TRANSMISSION_LINK = "transmission_link"
    SITE = "site"
    CELL = "cell"


class AlarmSeverity(StrEnum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    WARNING = "warning"


class KpiName(StrEnum):
    """Cell-level KPI families.

    Names and typical thresholds follow standard RAN practice (accessibility via RRC and
    E-RAB setup success, retainability via drop rate, integrity via throughput and
    latency, utilisation via PRB). Thresholds are operator-specific: the seeded values are
    modelled, flagged UNVERIFIED in docs/data-catalog.md, and must be reconciled with the
    operator's own counter definitions before live use.
    """

    AVAILABILITY = "availability_pct"
    RRC_SETUP_SUCCESS = "rrc_setup_success_pct"
    ERAB_SETUP_SUCCESS = "erab_setup_success_pct"
    DROP_RATE = "drop_rate_pct"
    PRB_UTILISATION = "prb_utilisation_pct"
    DL_THROUGHPUT = "dl_throughput_mbps"
    UL_THROUGHPUT = "ul_throughput_mbps"
    LATENCY = "latency_ms"
    ACTIVE_USERS = "active_users"
    SINR = "sinr_db"


#: Direction in which a KPI becomes worse, and the modelled alert threshold.
#: `lower_is_worse=True` means values below `threshold` indicate degradation.
KPI_POLARITY: Final[dict[KpiName, tuple[bool, float]]] = {
    KpiName.AVAILABILITY: (True, 99.0),
    KpiName.RRC_SETUP_SUCCESS: (True, 98.0),
    KpiName.ERAB_SETUP_SUCCESS: (True, 98.0),
    KpiName.DROP_RATE: (False, 1.0),
    KpiName.PRB_UTILISATION: (False, 80.0),
    KpiName.DL_THROUGHPUT: (True, 3.0),
    KpiName.UL_THROUGHPUT: (True, 1.0),
    KpiName.LATENCY: (False, 80.0),
    KpiName.ACTIVE_USERS: (False, 400.0),
    KpiName.SINR: (True, 3.0),
}


def is_degraded(name: KpiName, value: float) -> bool:
    """Whether a KPI sample breaches its modelled threshold."""
    lower_is_worse, threshold = KPI_POLARITY[name]
    return value < threshold if lower_is_worse else value > threshold


# ------------------------------------------------------------------ complaints


class Channel(StrEnum):
    CALL_CENTRE = "call_centre"
    ONE_APP = "one_app"
    SMS = "sms"
    USSD = "ussd"
    EMAIL = "email"
    SOCIAL = "social"


class Language(StrEnum):
    ENGLISH = "en"
    SWAHILI = "sw"
    SHENG = "sheng"
    MIXED = "mixed"


class ComplaintCategory(StrEnum):
    NO_SERVICE = "no_service"
    SLOW_DATA = "slow_data"
    CALL_DROPS = "call_drops"
    VOICE_QUALITY = "voice_quality"
    NETWORK_CONGESTION = "network_congestion"
    BILLING = "billing"
    BUNDLE_CONFUSION = "bundle_confusion"
    SIM_SWAP = "sim_swap"
    OTHER = "other"


#: Categories that point at the network. The rest must never raise a network incident
#: (the non-network surge scenario depends on this distinction).
NETWORK_CATEGORIES: Final[frozenset[ComplaintCategory]] = frozenset(
    {
        ComplaintCategory.NO_SERVICE,
        ComplaintCategory.SLOW_DATA,
        ComplaintCategory.CALL_DROPS,
        ComplaintCategory.VOICE_QUALITY,
        ComplaintCategory.NETWORK_CONGESTION,
    },
)


class Priority(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class IncidentStatus(StrEnum):
    DETECTED = "detected"
    DIAGNOSING = "diagnosing"
    MITIGATING = "mitigating"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    CLOSED = "closed"
    REOPENED = "reopened"
    SUPPRESSED = "suppressed"


class IncidentCause(StrEnum):
    """Ground-truth cause labels, shared by the seed and the diagnosis evals."""

    CELL_OUTAGE = "cell_outage"
    TRANSMISSION_LINK_FAILURE = "transmission_link_failure"
    CONGESTION = "congestion"
    POWER_FAILURE = "power_failure"
    CONFIGURATION_CHANGE = "configuration_change"
    PLANNED_MAINTENANCE = "planned_maintenance"
    FLAPPING = "flapping"
    NON_NETWORK = "non_network"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def confidence_band(score: float) -> Confidence:
    """Bucket a 0..1 score. Bands are shown in the UI; the raw score is stored."""
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"confidence must be in [0, 1], got {score}")
    if score >= 0.75:
        return Confidence.HIGH
    if score >= 0.5:
        return Confidence.MEDIUM
    return Confidence.LOW


# ------------------------------------------------------------------- windows


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """Half-open window `[start, end)`. Every query in the system uses this convention."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("TimeWindow requires timezone-aware datetimes")
        if self.end <= self.start:
            raise ValueError("TimeWindow end must be after start")

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def overlaps(self, other: TimeWindow) -> bool:
        return self.start < other.end and other.start < self.end

    def expand(self, by: timedelta) -> TimeWindow:
        return TimeWindow(start=self.start - by, end=self.end + by)
