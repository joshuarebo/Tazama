"""Domain value objects: MSISDN normalisation, geography, windows, KPI polarity.

MSISDN handling is in the PRD's edge-case register, so the accepted and rejected shapes
are pinned down here rather than discovered in production.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tazama.domain.value_objects import (
    NETWORK_CATEGORIES,
    Channel,
    ComplaintCategory,
    Confidence,
    GeoPoint,
    KpiName,
    Msisdn,
    MsisdnError,
    Region,
    TimeWindow,
    confidence_band,
    is_degraded,
)

# ------------------------------------------------------------------ MSISDN


@pytest.mark.parametrize(
    "raw",
    [
        "254712345678",
        "+254712345678",
        "0712345678",
        "712345678",
        "+254 712 345 678",
        "0712-345-678",
        "(0712) 345678",
        "00254712345678",
        "\u00a0254712345678\u00a0",  # non-breaking spaces from a copy-paste
        # full-width digits, folded by NFKC
        "\uff12\uff15\uff14\uff17\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18",
    ],
)
def test_every_real_world_shape_normalises_to_one_value(raw: str) -> None:
    assert Msisdn.parse(raw).value == "254712345678"


def test_safaricom_and_newer_prefixes_both_work() -> None:
    assert Msisdn.parse("0110123456").value == "254110123456"
    assert Msisdn.parse("+254110123456").national == "0110123456"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "0612345678",  # not a mobile prefix
        "020123456",  # landline
        "123",  # short code
        "07123456789",  # too long
        "071234567",  # too short
        "+4471234567890",  # foreign
        "not a number",
        "254712345678901234",
    ],
)
def test_unusable_numbers_are_refused(raw: str) -> None:
    with pytest.raises(MsisdnError):
        Msisdn.parse(raw)
    assert Msisdn.try_parse(raw) is None


def test_parse_is_idempotent() -> None:
    once = Msisdn.parse("0712345678")
    assert Msisdn.parse(once.value) == once


def test_formats_for_display_and_e164() -> None:
    number = Msisdn.parse("254712345678")
    assert number.national == "0712345678"
    assert number.e164 == "+254712345678"
    assert str(number) == "254712345678"


def test_direct_construction_validates() -> None:
    with pytest.raises(MsisdnError):
        Msisdn(value="0712345678")  # not normalised


def test_msisdn_is_hashable_and_sortable() -> None:
    numbers = {Msisdn.parse("0712345678"), Msisdn.parse("254712345678")}
    assert len(numbers) == 1
    assert sorted([Msisdn.parse("0712345679"), Msisdn.parse("0712345678")])[0].value.endswith("678")


@given(st.integers(min_value=100_000_00, max_value=999_999_99))
def test_any_valid_nine_digit_7_number_round_trips(tail: int) -> None:
    raw = f"07{tail:08d}"
    assert Msisdn.parse(raw).national == raw


# --------------------------------------------------------------- geography


def test_distance_between_known_cities_is_plausible() -> None:
    nairobi = GeoPoint(lat=-1.2864, lon=36.8172)
    mombasa = GeoPoint(lat=-4.0435, lon=39.6682)
    # Great-circle Nairobi to Mombasa is roughly 440 km.
    assert 420 < nairobi.distance_km(mombasa) < 460


def test_distance_to_self_is_zero() -> None:
    point = GeoPoint(lat=-1.2864, lon=36.8172)
    assert point.distance_km(point) == pytest.approx(0.0, abs=1e-9)


def test_distance_is_symmetric() -> None:
    a, b = GeoPoint(lat=-1.3, lon=36.8), GeoPoint(lat=-0.1, lon=34.8)
    assert a.distance_km(b) == pytest.approx(b.distance_km(a))


@pytest.mark.parametrize(("lat", "lon"), [(91.0, 0.0), (-91.0, 0.0), (0.0, 181.0), (0.0, -181.0)])
def test_impossible_coordinates_are_refused(lat: float, lon: float) -> None:
    with pytest.raises(ValueError, match="out of range"):
        GeoPoint(lat=lat, lon=lon)


def test_seeded_regions_are_the_five_in_the_prd() -> None:
    assert {r.value for r in Region} == {"Nairobi", "Mombasa", "Kisumu", "Nakuru", "Eldoret"}


# ----------------------------------------------------------------- windows


def test_window_is_half_open() -> None:
    window = TimeWindow(
        start=datetime(2026, 3, 17, 10, tzinfo=UTC),
        end=datetime(2026, 3, 17, 11, tzinfo=UTC),
    )
    assert window.contains(window.start)
    assert not window.contains(window.end)
    assert window.duration == timedelta(hours=1)


def test_window_requires_aware_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        TimeWindow(
            start=datetime(2026, 3, 17, 10),
            end=datetime(2026, 3, 17, 11, tzinfo=UTC),
        )


def test_window_must_move_forwards() -> None:
    moment = datetime(2026, 3, 17, 10, tzinfo=UTC)
    with pytest.raises(ValueError, match="after start"):
        TimeWindow(start=moment, end=moment)


def test_overlap_and_expand() -> None:
    first = TimeWindow(
        start=datetime(2026, 3, 17, 10, tzinfo=UTC),
        end=datetime(2026, 3, 17, 11, tzinfo=UTC),
    )
    second = TimeWindow(
        start=datetime(2026, 3, 17, 11, tzinfo=UTC),
        end=datetime(2026, 3, 17, 12, tzinfo=UTC),
    )
    assert not first.overlaps(second)  # touching is not overlapping
    assert first.expand(timedelta(minutes=1)).overlaps(second)


# --------------------------------------------------------- KPIs and labels


@pytest.mark.parametrize(
    ("name", "value", "degraded"),
    [
        (KpiName.AVAILABILITY, 100.0, False),
        (KpiName.AVAILABILITY, 98.5, True),
        (KpiName.RRC_SETUP_SUCCESS, 99.2, False),
        (KpiName.RRC_SETUP_SUCCESS, 95.0, True),
        (KpiName.DROP_RATE, 0.4, False),
        (KpiName.DROP_RATE, 2.5, True),
        (KpiName.PRB_UTILISATION, 55.0, False),
        (KpiName.PRB_UTILISATION, 92.0, True),
        (KpiName.DL_THROUGHPUT, 12.0, False),
        (KpiName.DL_THROUGHPUT, 1.2, True),
        (KpiName.LATENCY, 40.0, False),
        (KpiName.LATENCY, 120.0, True),
    ],
)
def test_degradation_respects_each_kpi_direction(
    name: KpiName,
    value: float,
    degraded: bool,
) -> None:
    assert is_degraded(name, value) is degraded


def test_every_kpi_declares_a_direction_and_threshold() -> None:
    from tazama.domain.value_objects import KPI_POLARITY

    assert set(KPI_POLARITY) == set(KpiName)


@pytest.mark.parametrize(
    ("score", "band"),
    [
        (0.0, Confidence.LOW),
        (0.49, Confidence.LOW),
        (0.5, Confidence.MEDIUM),
        (0.74, Confidence.MEDIUM),
        (0.75, Confidence.HIGH),
        (1.0, Confidence.HIGH),
    ],
)
def test_confidence_bands(score: float, band: Confidence) -> None:
    assert confidence_band(score) is band


@pytest.mark.parametrize("score", [-0.01, 1.01])
def test_confidence_outside_zero_to_one_is_refused(score: float) -> None:
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        confidence_band(score)


def test_non_network_categories_cannot_raise_a_network_incident() -> None:
    assert ComplaintCategory.BILLING not in NETWORK_CATEGORIES
    assert ComplaintCategory.BUNDLE_CONFUSION not in NETWORK_CATEGORIES
    assert ComplaintCategory.SIM_SWAP not in NETWORK_CATEGORIES
    assert ComplaintCategory.NO_SERVICE in NETWORK_CATEGORIES


def test_all_six_channels_from_the_prd_exist() -> None:
    assert {c.value for c in Channel} == {
        "call_centre",
        "one_app",
        "sms",
        "ussd",
        "email",
        "social",
    }
