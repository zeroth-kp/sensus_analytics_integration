"""Pure unit tests for coordinator._build_daily_statistics - the shared
hour-overwrite/running-sum logic used by both the one-time cutover backfill
(async_backfill_daily_history) and the recurring scheduled refresh
(async_refresh_recent_daily_statistics).
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from custom_components.sensus_analytics.coordinator import SensusAnalyticsDataUpdateCoordinator

UTC = timezone.utc


def _make_coordinator(unit_type="gal"):
    coordinator = SensusAnalyticsDataUpdateCoordinator.__new__(SensusAnalyticsDataUpdateCoordinator)
    coordinator.config_entry = SimpleNamespace(data={"unit_type": unit_type})
    return coordinator


def _canonical_hour(day):
    return datetime.combine(day.date(), SensusAnalyticsDataUpdateCoordinator._DAILY_CANONICAL_HOUR, tzinfo=UTC)


def test_day_with_no_existing_hours_writes_single_row_on_canonical_hour():
    coordinator = _make_coordinator()
    day1 = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    entries = [(day1, 5, "GAL")]

    statistics, running_sum = coordinator._build_daily_statistics(entries, {}, UTC, starting_sum=0.0)

    assert len(statistics) == 1
    assert statistics[0]["start"] == _canonical_hour(day1)
    assert statistics[0]["state"] == 5.0
    assert statistics[0]["sum"] == 5.0
    assert running_sum == 5.0


def test_starting_sum_carries_forward():
    coordinator = _make_coordinator()
    day1 = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    entries = [(day1, 5, "GAL")]

    statistics, running_sum = coordinator._build_daily_statistics(entries, {}, UTC, starting_sum=100.0)

    assert statistics[0]["sum"] == 105.0
    assert running_sum == 105.0


def test_day_with_existing_hours_flattens_all_and_lands_on_canonical_hour():
    coordinator = _make_coordinator()
    day1 = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    hour_a = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
    hour_b = datetime(2026, 7, 1, 15, 0, tzinfo=UTC)
    hour_c = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)
    existing_hours_by_day = {day1.date(): [hour_a, hour_b, hour_c]}
    entries = [(day1, 10, "GAL")]

    statistics, running_sum = coordinator._build_daily_statistics(entries, existing_hours_by_day, UTC, starting_sum=0.0)

    # None of the pre-existing hours is the canonical hour, so all three get
    # flattened to zero, plus a new row lands the real total on 23:00 -
    # unlike the old "land on whichever hour happens to be last" behavior,
    # which would have put the real total on hour_c (20:00) instead.
    assert len(statistics) == 4
    assert statistics[0]["start"] == hour_a
    assert statistics[0]["state"] == 0
    assert statistics[0]["sum"] == 0.0
    assert statistics[1]["start"] == hour_b
    assert statistics[1]["state"] == 0
    assert statistics[1]["sum"] == 0.0
    assert statistics[2]["start"] == hour_c
    assert statistics[2]["state"] == 0
    assert statistics[2]["sum"] == 0.0
    assert statistics[3]["start"] == _canonical_hour(day1)
    assert statistics[3]["state"] == 10.0
    assert statistics[3]["sum"] == 10.0
    assert running_sum == 10.0


def test_existing_canonical_hour_is_overwritten_not_duplicated():
    """A day already carrying a correct canonical-hour row (e.g. from a
    prior run) should get exactly one row for that hour, not a flattened
    zero row followed by a second write."""
    coordinator = _make_coordinator()
    day1 = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    canonical_hour = _canonical_hour(day1)
    existing_hours_by_day = {day1.date(): [canonical_hour]}
    entries = [(day1, 10, "GAL")]

    statistics, running_sum = coordinator._build_daily_statistics(entries, existing_hours_by_day, UTC, starting_sum=0.0)

    assert len(statistics) == 1
    assert statistics[0]["start"] == canonical_hour
    assert statistics[0]["state"] == 10.0
    assert statistics[0]["sum"] == 10.0
    assert running_sum == 10.0


def test_rerun_with_prior_output_as_existing_hours_is_idempotent():
    """Simulates running the same backfill twice in a row, where the second
    run's `existing_hours_by_day` reflects exactly what the first run
    wrote. The second run must reproduce identical sums, not drift - this
    is what "landing on a fixed hour" guarantees that "landing on whichever
    hour already exists" did not.
    """
    coordinator = _make_coordinator()
    day1 = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    day2 = datetime(2026, 7, 2, 0, 0, tzinfo=UTC)
    entries = [(day1, 5, "GAL"), (day2, 3, "GAL")]

    first_statistics, first_running_sum = coordinator._build_daily_statistics(entries, {}, UTC, starting_sum=0.0)

    existing_hours_by_day = {}
    for row in first_statistics:
        day = row["start"].astimezone(UTC).date()
        existing_hours_by_day.setdefault(day, []).append(row["start"])

    second_statistics, second_running_sum = coordinator._build_daily_statistics(
        entries, existing_hours_by_day, UTC, starting_sum=0.0
    )

    assert second_running_sum == first_running_sum
    assert [(s["start"], s["sum"]) for s in second_statistics] == [(s["start"], s["sum"]) for s in first_statistics]


def test_unconvertible_usage_is_skipped():
    coordinator = _make_coordinator()
    day1 = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    entries = [(day1, None, "GAL")]

    statistics, running_sum = coordinator._build_daily_statistics(entries, {}, UTC, starting_sum=5.0)

    assert statistics == []
    assert running_sum == 5.0


def test_multiple_days_accumulate_running_sum():
    coordinator = _make_coordinator()
    day1 = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    day2 = datetime(2026, 7, 2, 0, 0, tzinfo=UTC)
    entries = [(day1, 5, "GAL"), (day2, 3, "GAL")]

    statistics, running_sum = coordinator._build_daily_statistics(entries, {}, UTC, starting_sum=0.0)

    assert [s["sum"] for s in statistics] == [5.0, 8.0]
    assert running_sum == 8.0


def test_implausibly_large_day_is_skipped_not_imported():
    """A day whose value is far beyond any plausible residential reading
    (e.g. a source misattributing a monthly total to one day) must be
    dropped entirely, not written to the sum chain - see
    _MAX_PLAUSIBLE_DAILY_USAGE_GAL."""
    coordinator = _make_coordinator()
    day1 = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    entries = [(day1, 50000, "GAL")]

    statistics, running_sum = coordinator._build_daily_statistics(entries, {}, UTC, starting_sum=100.0)

    assert statistics == []
    assert running_sum == 100.0


def test_implausibly_large_day_does_not_disrupt_neighboring_days():
    coordinator = _make_coordinator()
    day1 = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    day2 = datetime(2026, 7, 2, 0, 0, tzinfo=UTC)
    day3 = datetime(2026, 7, 3, 0, 0, tzinfo=UTC)
    entries = [(day1, 5, "GAL"), (day2, 50000, "GAL"), (day3, 3, "GAL")]

    statistics, running_sum = coordinator._build_daily_statistics(entries, {}, UTC, starting_sum=0.0)

    assert [s["sum"] for s in statistics] == [5.0, 8.0]
    assert running_sum == 8.0


def test_value_just_under_ceiling_is_imported_normally():
    coordinator = _make_coordinator()
    day1 = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    just_under = SensusAnalyticsDataUpdateCoordinator._MAX_PLAUSIBLE_DAILY_USAGE_GAL - 1
    entries = [(day1, just_under, "GAL")]

    statistics, running_sum = coordinator._build_daily_statistics(entries, {}, UTC, starting_sum=0.0)

    assert len(statistics) == 1
    assert statistics[0]["state"] == float(just_under)
    assert running_sum == float(just_under)


def test_ceiling_converts_to_configured_unit():
    """The ceiling is defined in gallons but must convert correctly when
    the entry is configured in CCF - a raw-gallon comparison against a
    CCF-converted value would be wrong by ~a factor of 748."""
    coordinator = _make_coordinator(unit_type="CCF")
    day1 = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    # ~20 CCF is roughly the gallon ceiling converted to CCF - comfortably
    # over it, should be skipped.
    entries = [(day1, 25, "CCF")]

    statistics, running_sum = coordinator._build_daily_statistics(entries, {}, UTC, starting_sum=0.0)

    assert statistics == []
    assert running_sum == 0.0
