"""Tests for the daily-history backfill window fixes:

- Finding 2: the daily-granularity fetch used to be hardcoded to a fixed
  60-day window regardless of how old the cutover date was, silently
  leaving a gap between the monthly-aggregate backfill and the daily
  window for any cutover more than 60 days in the past.
- Finding 5: the monthly/yearly pagination walk-back always started from
  "now" instead of the cutover's month boundary, wastefully fetching and
  fully discarding pages whenever the cutover was more than ~1 page
  (~400 days) old.

Most of these need only a lightly-constructed coordinator (no `hass`
required for `_fetch_daily_history_window` itself - only for the two
top-level async methods that fetch `hass.config.time_zone`), so they're
kept out of the full pytest-homeassistant-custom-component harness where
possible for speed; one true end-to-end test is included at the bottom.
"""

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sensus_analytics.const import DOMAIN
from custom_components.sensus_analytics.coordinator import SensusAnalyticsDataUpdateCoordinator

from .conftest import config_entry_data, make_mock_session

UTC = timezone.utc


def _make_coordinator():
    coordinator = SensusAnalyticsDataUpdateCoordinator.__new__(SensusAnalyticsDataUpdateCoordinator)
    coordinator.base_url = "https://example.invalid/"
    coordinator.username = "user"
    coordinator.password = "pass"
    coordinator.account_number = "acct"
    coordinator.meter_number = "meter"
    coordinator.config_entry = SimpleNamespace(data={"unit_type": "gal"})
    return coordinator


def test_fetch_daily_entries_in_range_uses_explicit_bounds():
    coordinator = _make_coordinator()
    start_local = datetime(2026, 5, 1, tzinfo=UTC)
    end_local = datetime(2026, 7, 1, tzinfo=UTC)
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["params"] = params
        response = SimpleNamespace()
        response.raise_for_status = lambda: None
        response.json = lambda: {"operationSuccess": False}
        return response

    session = SimpleNamespace(get=fake_get)
    coordinator._fetch_daily_entries_in_range(session, start_local, end_local)

    assert captured["params"]["start"] == int(start_local.timestamp() * 1000)
    assert captured["params"]["end"] == int(end_local.timestamp() * 1000)
    assert captured["params"]["zoom"] == "month"


def test_window_widens_to_reach_old_boundary():
    coordinator = _make_coordinator()
    now = datetime(2026, 7, 20, tzinfo=UTC)
    boundary_local = now - timedelta(days=120)
    boundary_month_start = boundary_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    captured = {}

    def fake_fetch_daily_entries_in_range(session, start_local, end_local):
        captured["start_local"] = start_local
        return []

    with (
        patch.object(coordinator, "_create_authenticated_session", return_value=object()),
        patch.object(coordinator, "_fetch_yearly_page", return_value=None),
        patch.object(coordinator, "_fetch_daily_entries_in_range", side_effect=fake_fetch_daily_entries_in_range),
        patch("custom_components.sensus_analytics.coordinator.datetime") as mock_datetime,
    ):
        mock_datetime.now.return_value = now
        coordinator._fetch_daily_history_window(boundary_month_start, boundary_local)

    # Widened well past the old fixed 60-day window to actually reach the boundary month.
    assert captured["start_local"] <= boundary_month_start


def test_window_keeps_default_60_days_for_recent_boundary():
    coordinator = _make_coordinator()
    now = datetime(2026, 7, 20, tzinfo=UTC)
    boundary_local = now - timedelta(days=10)
    boundary_month_start = boundary_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    captured = {}

    def fake_fetch_daily_entries_in_range(session, start_local, end_local):
        captured["start_local"] = start_local
        return []

    with (
        patch.object(coordinator, "_create_authenticated_session", return_value=object()),
        patch.object(coordinator, "_fetch_yearly_page", return_value=None),
        patch.object(coordinator, "_fetch_daily_entries_in_range", side_effect=fake_fetch_daily_entries_in_range),
        patch("custom_components.sensus_analytics.coordinator.datetime") as mock_datetime,
    ):
        mock_datetime.now.return_value = now
        coordinator._fetch_daily_history_window(boundary_month_start, boundary_local)

    # Should not regress the common case: still at least 60 days back.
    assert captured["start_local"] <= now - timedelta(days=60)


def test_gap_warning_logged_when_sensus_data_does_not_reach_boundary(caplog):
    coordinator = _make_coordinator()
    now = datetime(2026, 7, 20, tzinfo=UTC)
    boundary_local = now - timedelta(days=120)
    boundary_month_start = boundary_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Sensus only actually returns data starting 30 days ago, well short of
    # the 120-day-old boundary we asked for.
    short_entry_time = now - timedelta(days=30)
    short_entries = [(int(short_entry_time.timestamp() * 1000), 5, "GAL")]

    with (
        patch.object(coordinator, "_create_authenticated_session", return_value=object()),
        patch.object(coordinator, "_fetch_yearly_page", return_value=None),
        patch.object(coordinator, "_fetch_daily_entries_in_range", return_value=short_entries),
        patch("custom_components.sensus_analytics.coordinator.datetime") as mock_datetime,
        caplog.at_level(logging.WARNING),
    ):
        mock_datetime.now.return_value = now
        coordinator._fetch_daily_history_window(boundary_month_start, boundary_local)

    assert any("Sensus only" in record.message for record in caplog.records)


def test_pagination_anchors_first_page_at_boundary_not_now():
    coordinator = _make_coordinator()
    now = datetime(2026, 7, 20, tzinfo=UTC)
    boundary_local = now - timedelta(days=500)
    boundary_month_start = boundary_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    captured_end_ms = []

    def fake_fetch_yearly_page(session, end_ms):
        captured_end_ms.append(end_ms)
        return None  # stop after the first call

    with (
        patch.object(coordinator, "_create_authenticated_session", return_value=object()),
        patch.object(coordinator, "_fetch_yearly_page", side_effect=fake_fetch_yearly_page),
        patch.object(coordinator, "_fetch_daily_entries_in_range", return_value=[]),
        patch("custom_components.sensus_analytics.coordinator.datetime") as mock_datetime,
    ):
        mock_datetime.now.return_value = now
        coordinator._fetch_daily_history_window(boundary_month_start, boundary_local)

    assert captured_end_ms == [int(boundary_month_start.timestamp() * 1000)]


def test_monthly_totals_only_include_entries_before_boundary():
    coordinator = _make_coordinator()
    now = datetime(2026, 7, 20, tzinfo=UTC)
    boundary_local = now - timedelta(days=200)
    boundary_month_start = boundary_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    before_boundary = boundary_month_start - timedelta(days=30)
    after_boundary = boundary_month_start + timedelta(days=5)
    page_entries = [
        (int(before_boundary.timestamp() * 1000), 10, "GAL"),
        (int(after_boundary.timestamp() * 1000), 20, "GAL"),
    ]

    def fake_fetch_yearly_page(session, end_ms):
        if fake_fetch_yearly_page.calls == 0:
            fake_fetch_yearly_page.calls += 1
            return page_entries, False, None
        return None

    fake_fetch_yearly_page.calls = 0

    with (
        patch.object(coordinator, "_create_authenticated_session", return_value=object()),
        patch.object(coordinator, "_fetch_yearly_page", side_effect=fake_fetch_yearly_page),
        patch.object(coordinator, "_fetch_daily_entries_in_range", return_value=[]),
        patch("custom_components.sensus_analytics.coordinator.datetime") as mock_datetime,
    ):
        mock_datetime.now.return_value = now
        monthly_totals, _ = coordinator._fetch_daily_history_window(boundary_month_start, boundary_local)

    assert len(monthly_totals) == 1
    assert monthly_totals[0][1] == 10


@pytest.mark.asyncio
async def test_async_backfill_daily_history_end_to_end(recorder_mock, enable_custom_integrations, hass):
    entry = MockConfigEntry(domain=DOMAIN, data=config_entry_data())
    entry.add_to_hass(hass)

    with patch(
        "custom_components.sensus_analytics.coordinator.requests.Session",
        return_value=make_mock_session(),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]

    with patch(
        "custom_components.sensus_analytics.coordinator.requests.Session",
        return_value=make_mock_session(),
    ):
        imported = await coordinator.async_backfill_daily_history(datetime(2026, 6, 1).date())

    assert imported >= 0


@pytest.mark.asyncio
async def test_backfill_bridges_existing_sum_across_a_retention_gap(recorder_mock, enable_custom_integrations, hass):
    """A re-run whose cutover month Sensus can no longer fully re-derive (its
    daily-granularity retention starts later than the cutover month, as
    make_mock_session's fixed 2026-07-20 DAILY_RESPONSE does relative to a
    2026-06-01 cutover) must not discard the already-recorded sum for the
    gap - it should bridge from what's already there instead of resetting to
    the monthly-aggregate-only baseline (which would create a downward
    discontinuity, undercounting everything from the gap forward).

    Builds the coordinator directly rather than through
    hass.config_entries.async_setup, so the platform's automatic
    startup-triggered scheduled refresh (which runs against real wall-clock
    "now", not this test's fixed 2026 dates) can't interfere with the
    controlled scenario below.
    """
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.models import StatisticData
    from homeassistant.components.recorder.statistics import statistics_during_period

    coordinator = SensusAnalyticsDataUpdateCoordinator.__new__(SensusAnalyticsDataUpdateCoordinator)
    coordinator.hass = hass
    coordinator.base_url = "https://example.invalid/"
    coordinator.username = "user"
    coordinator.password = "pass"
    coordinator.account_number = "acct"
    coordinator.meter_number = "meter"
    coordinator.config_entry = SimpleNamespace(entry_id="test_entry", data=config_entry_data())
    # No entity registered under this unique_id, so _resolve_daily_usage_statistic_id
    # falls back to this fixed statistic_id.
    statistic_id = "sensor.sensus_analytics_daily_usage"

    # Seed a pre-existing statistic inside the gap (between the 2026-06-01
    # cutover and make_mock_session's fixed 2026-07-20 daily entry) with a
    # large sum, simulating an entity that's already been accumulating real
    # history there - exactly the case where Sensus's retention has since
    # moved past what a fresh backfill call can independently re-derive.
    existing_sum = 500000.0
    # Within the 7-day lookback _get_existing_sum_before uses ahead of the
    # first real daily entry (2026-07-20 per DAILY_RESPONSE).
    pre_existing_hour = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    coordinator._import_statistics(
        statistic_id,
        "CCF",
        [StatisticData(start=pre_existing_hour, state=0, sum=existing_sum, last_reset=pre_existing_hour)],
        "test seed",
    )
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    with patch(
        "custom_components.sensus_analytics.coordinator.requests.Session",
        return_value=make_mock_session(),
    ):
        await coordinator.async_backfill_daily_history(datetime(2026, 6, 1).date())
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 7, 19, tzinfo=timezone.utc),
        None,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    rows = stats[statistic_id]
    # DAILY_RESPONSE contributes a single +2 CCF entry on top of whatever the
    # backfill bridged from - bridging from existing_sum (not the ~30 CCF
    # monthly-aggregate-only baseline) is what proves the gap didn't regress
    # the already-recorded total.
    assert rows[-1]["sum"] == pytest.approx(existing_sum + 2)


@pytest.mark.asyncio
async def test_backfill_aborts_when_a_write_races_the_bridging_baseline(
    recorder_mock, enable_custom_integrations, hass, caplog
):
    """A second write (another concurrent backfill/refresh call, or a manual
    recorder/adjust_sum_statistics correction) lands on the bridging anchor
    between when this backfill first reads its baseline and when it would
    otherwise write - it must detect the change and abort rather than
    silently overwrite using the now-stale baseline it read at the start.

    Same setup as test_backfill_bridges_existing_sum_across_a_retention_gap,
    but _get_existing_sum_before is patched to return a *different* value on
    its second call (the new pre-write verification) than its first (the
    original bridging read) - simulating exactly that race.
    """
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.models import StatisticData
    from homeassistant.components.recorder.statistics import statistics_during_period

    coordinator = SensusAnalyticsDataUpdateCoordinator.__new__(SensusAnalyticsDataUpdateCoordinator)
    coordinator.hass = hass
    coordinator.base_url = "https://example.invalid/"
    coordinator.username = "user"
    coordinator.password = "pass"
    coordinator.account_number = "acct"
    coordinator.meter_number = "meter"
    coordinator.config_entry = SimpleNamespace(entry_id="test_entry", data=config_entry_data())
    statistic_id = "sensor.sensus_analytics_daily_usage"

    existing_sum = 500000.0
    pre_existing_hour = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    coordinator._import_statistics(
        statistic_id,
        "CCF",
        [StatisticData(start=pre_existing_hour, state=0, sum=existing_sum, last_reset=pre_existing_hour)],
        "test seed",
    )
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    # First call: the real bridging read (matches the seeded value). Second
    # call: the pre-write verification, made to see a different number - as
    # if a different write had landed on the same statistic in between.
    coordinator._get_existing_sum_before = AsyncMock(side_effect=[existing_sum, existing_sum + 999_999.0])

    with patch(
        "custom_components.sensus_analytics.coordinator.requests.Session",
        return_value=make_mock_session(),
    ), caplog.at_level("ERROR"):
        imported = await coordinator.async_backfill_daily_history(datetime(2026, 6, 1).date())

    assert imported == 0
    assert "changed" in caplog.text
    assert statistic_id in caplog.text

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 7, 1, tzinfo=timezone.utc),
        None,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    rows = stats[statistic_id]
    # Nothing beyond the seeded row should exist - the abort must have
    # prevented the write entirely, not just returned early after writing.
    assert len(rows) == 1
    assert rows[0]["sum"] == existing_sum
