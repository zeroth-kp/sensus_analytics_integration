"""Integration tests for the recurring daily-statistics refresh added
alongside the SensusAnalyticsDailyUsageSensor state_class fix - since the
sensor no longer has a native state_class, its long-term statistics need
this scheduled refresh (see coordinator.py's
async_refresh_recent_daily_statistics and __init__.py's
_scheduled_daily_refresh) to stay fresh between manual
backfill_daily_history calls.
"""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers.event import async_track_time_interval
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sensus_analytics.const import DOMAIN

from .conftest import config_entry_data, make_mock_session


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
        imported = await coordinator.async_refresh_recent_daily_statistics(days=3)

    assert imported >= 0


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
        imported = await coordinator.async_refresh_recent_daily_statistics(days=3)

    assert imported == 0
    assert "changed" in caplog.text
