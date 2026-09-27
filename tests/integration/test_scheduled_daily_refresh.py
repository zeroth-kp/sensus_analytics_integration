"""Integration tests for the recurring daily-statistics refresh added
alongside the SensusAnalyticsDailyUsageSensor state_class fix - since the
sensor no longer has a native state_class, its long-term statistics need
this scheduled refresh (see coordinator.py's
async_refresh_recent_daily_statistics and __init__.py's
_scheduled_daily_refresh) to stay fresh between manual
backfill_daily_history calls.
"""

from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers.event import async_track_time_interval
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sensus_analytics.const import DOMAIN
from custom_components.sensus_analytics.coordinator import SensusAnalyticsDataUpdateCoordinator

from .conftest import config_entry_data, make_mock_session

# make_mock_session's fixed DAILY_RESPONSE entry lands on 2026-07-20. Now that
# the scheduled refresh's trailing window is actually enforced (rather than
# silently including everything Sensus's zoom=month response happens to
# return), tests need a `days` wide enough for that fixed date to still fall
# inside it relative to the real wall clock - matching the 60-day "safe
# historical minimum" already used elsewhere in this module, rather than
# mocking `datetime` broadly (which would also break the canonical-hour
# `datetime.combine` call inside _build_daily_statistics, in the same
# call chain).
_DAYS_WIDE_ENOUGH_FOR_FIXTURE = 60


@pytest.mark.asyncio
async def test_setup_registers_and_cancels_scheduled_refresh(recorder_mock, enable_custom_integrations, hass):
    entry = MockConfigEntry(domain=DOMAIN, data=config_entry_data())
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.sensus_analytics.coordinator.requests.Session",
            return_value=make_mock_session(),
        ),
        patch(
            "custom_components.sensus_analytics.async_track_time_interval",
            wraps=async_track_time_interval,
        ) as spy,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    spy.assert_called_once()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_refresh_imports_statistics_with_baseline_sum(recorder_mock, enable_custom_integrations, hass):
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
        imported = await coordinator.async_refresh_recent_daily_statistics(days=_DAYS_WIDE_ENOUGH_FOR_FIXTURE)

    # DAILY_RESPONSE's single 2026-07-20 entry falls inside the widened
    # trailing window, so the refresh should actually import it - not just
    # return >= 0, which a wrongly-empty result would also satisfy.
    assert imported > 0


@pytest.mark.asyncio
async def test_refresh_aborts_when_a_write_races_the_baseline(recorder_mock, enable_custom_integrations, hass, caplog):
    """A concurrent write to the same statistic between this refresh's
    baseline read and its own write must be detected and abort the run,
    instead of silently overwriting using the now-stale baseline.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=config_entry_data())
    entry.add_to_hass(hass)

    with patch(
        "custom_components.sensus_analytics.coordinator.requests.Session",
        return_value=make_mock_session(),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    # First call is the real baseline read (nothing exists yet -> None);
    # second call is the pre-write verification, made to see a real value -
    # as if a different write had landed on this statistic in between.
    coordinator._get_existing_sum_before = AsyncMock(side_effect=[None, 12345.0])

    with (
        patch(
            "custom_components.sensus_analytics.coordinator.requests.Session",
            return_value=make_mock_session(),
        ),
        caplog.at_level("ERROR"),
    ):
        imported = await coordinator.async_refresh_recent_daily_statistics(days=_DAYS_WIDE_ENOUGH_FOR_FIXTURE)

    assert imported == 0
    assert "changed" in caplog.text


@pytest.mark.asyncio
async def test_refresh_does_not_reprocess_an_entry_outside_its_requested_window(
    recorder_mock, enable_custom_integrations, hass
):
    """Regression test for out-of-window entries in the scheduled refresh:
    Sensus's zoom=month endpoint can return an entry from weeks before the
    requested window, and without response-side filtering that entry would
    be reprocessed and silently overwrite an already-correct, much-older
    statistic - using a baseline that was only ever valid for the narrow
    recent window actually requested. Because the refresh also runs once on
    setup, every config-entry reload would repeat the same bad overwrite.
    """
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.models import StatisticData
    from homeassistant.components.recorder.statistics import statistics_during_period
    from homeassistant.util import dt as dt_util

    coordinator = SensusAnalyticsDataUpdateCoordinator.__new__(SensusAnalyticsDataUpdateCoordinator)
    coordinator.hass = hass
    coordinator.base_url = "https://example.invalid/"
    coordinator.username = "user"
    coordinator.password = "pass"
    coordinator.account_number = "acct"
    coordinator.meter_number = "meter"
    coordinator.config_entry = SimpleNamespace(entry_id="test_entry", data=config_entry_data(unit_type="gal"))
    statistic_id = "sensor.sensus_analytics_daily_usage"

    # Must match _build_daily_statistics's own canonical-hour computation
    # (23:00 *local*, not UTC) exactly, or this seeds/checks a different row
    # than the one the bug (and the fix) actually touch.
    local_tz = dt_util.get_time_zone(hass.config.time_zone)
    old_canonical_hour = dt_util.as_utc(datetime.combine(datetime(2026, 5, 10).date(), time(23, 0), tzinfo=local_tz))
    correct_old_sum = 300000.0
    coordinator._import_statistics(
        statistic_id,
        "gal",
        [StatisticData(start=old_canonical_hour, state=25.0, sum=correct_old_sum, last_reset=old_canonical_hour)],
        "test seed",
    )
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    now = datetime.now(timezone.utc)
    recent_entry_ts_ms = int((now - timedelta(hours=6)).timestamp() * 1000)
    # Simulates Sensus's zoom=month behavior: it returns an entry far
    # outside the requested narrow window alongside the genuinely-recent
    # one the refresh actually asked for.
    # The old entry's own value is small and plausible - it's the baseline
    # it lands on, not the value itself, that's wrong, so this must stay
    # under the sanity ceiling
    # (_MAX_PLAUSIBLE_DAILY_USAGE_GAL) to actually exercise the date-range
    # fix rather than that unrelated, already-existing guard.
    usage_list = [
        ["gal"],
        [int(old_canonical_hour.timestamp() * 1000), 25.0],
        [recent_entry_ts_ms, 90],
    ]

    def fake_get(url, params=None, timeout=None):
        response = SimpleNamespace()
        response.raise_for_status = lambda: None
        response.json = lambda: {"operationSuccess": True, "data": {"usage": usage_list}}
        return response

    with patch(
        "custom_components.sensus_analytics.coordinator.requests.Session",
        return_value=SimpleNamespace(get=fake_get, post=lambda *a, **k: SimpleNamespace(status_code=302)),
    ):
        await coordinator.async_refresh_recent_daily_statistics(days=3)
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        old_canonical_hour,
        old_canonical_hour + timedelta(hours=1),
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    rows = stats[statistic_id]
    # The out-of-window entry must not have touched this row - it should
    # still hold the value seeded above, not a sum rebased onto the refresh
    # window's baseline.
    assert rows[-1]["sum"] == pytest.approx(correct_old_sum)
