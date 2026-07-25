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


def test_day_with_no_existing_hours_writes_single_row():
    coordinator = _make_coordinator()
    day1 = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    entries = [(day1, 5, "GAL")]

    statistics, running_sum = coordinator._build_daily_statistics(entries, {}, UTC, starting_sum=0.0)

    assert len(statistics) == 1
    assert statistics[0]["start"] == day1
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


def test_day_with_existing_hours_flattens_all_but_last():
    coordinator = _make_coordinator()
    day1 = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    hour_a = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
    hour_b = datetime(2026, 7, 1, 15, 0, tzinfo=UTC)
    hour_c = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)
    existing_hours_by_day = {day1.date(): [hour_a, hour_b, hour_c]}
    entries = [(day1, 10, "GAL")]

    statistics, running_sum = coordinator._build_daily_statistics(entries, existing_hours_by_day, UTC, starting_sum=0.0)

    assert len(statistics) == 3
    assert statistics[0]["start"] == hour_a
    assert statistics[0]["state"] == 0
    assert statistics[0]["sum"] == 0.0
    assert statistics[1]["start"] == hour_b
    assert statistics[1]["state"] == 0
    assert statistics[1]["sum"] == 0.0
    # The full corrected total lands on the last existing hour, since HA's
    # day/month views read the *last* hourly row of the day, not a
    # recomputation.
    assert statistics[2]["start"] == hour_c
    assert statistics[2]["state"] == 10.0
    assert statistics[2]["sum"] == 10.0
    assert running_sum == 10.0


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
