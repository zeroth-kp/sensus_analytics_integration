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
from unittest.mock import patch

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
