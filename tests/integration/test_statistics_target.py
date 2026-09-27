"""Integration tests for the statistics_target option and the legacy-writer gating it controls."""

import logging
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sensus_analytics.const import CONF_STATISTICS_TARGET, DOMAIN
from custom_components.sensus_analytics.coordinator import SensusAnalyticsDataUpdateCoordinator

from .conftest import config_entry_data, make_mock_session

SESSION = "custom_components.sensus_analytics.coordinator.requests.Session"


# Values for every optional pricing field, so a submitted options form is
# valid apart from whatever a test changes.
PRICING = {
    "included_gallons": 2000.0,
    "tier1_gallons": 10000.0,
    "tier1_price": 4.5,
    "tier2_gallons": 15000.0,
    "tier2_price": 5.5,
    "tier3_gallons": 20000.0,
    "tier3_price": 6.5,
    "tier4_price": 9.5,
    "service_fee": 20.0,
}


def _full_options_input(entry, form):
    keys = {str(schema_key) for schema_key in form["data_schema"].schema}
    user_input = {key: value for key, value in {**entry.data, **PRICING}.items() if key in keys}
    user_input.setdefault("hour_settle_delay_minutes", 90)
    return user_input


async def _setup_entry(hass, **overrides):
    entry = MockConfigEntry(domain=DOMAIN, data=config_entry_data(unit_type="gal", **overrides))
    entry.add_to_hass(hass)
    with patch(SESSION, return_value=make_mock_session()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@pytest.mark.asyncio
async def test_legacy_writers_are_skipped_in_live_mode(
    fixture_now, recorder_mock, enable_custom_integrations, hass, caplog
):
    entry = await _setup_entry(hass, **{CONF_STATISTICS_TARGET: "live"})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.legacy_statistics_enabled is False

    with patch(SESSION) as session_factory, caplog.at_level(logging.WARNING):
        assert await coordinator.async_backfill_hourly_statistics(24) == 0
        assert await coordinator.async_refresh_recent_daily_statistics() == 0
        assert await coordinator.async_backfill_daily_history(date(2026, 7, 1)) == 0

    session_factory.assert_not_called()
    skipped = [record for record in caplog.records if "skipped: statistics_target is 'live'" in record.getMessage()]
    assert len(skipped) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(("target", "refresh_runs"), [("shadow", True), ("live", False)])
async def test_scheduled_daily_refresh_runs_only_in_shadow_mode(
    fixture_now, recorder_mock, enable_custom_integrations, hass, target, refresh_runs
):
    with patch.object(
        SensusAnalyticsDataUpdateCoordinator, "async_refresh_recent_daily_statistics", AsyncMock(return_value=0)
    ) as refresh:
        await _setup_entry(hass, **{CONF_STATISTICS_TARGET: target})
    assert refresh.await_count == (1 if refresh_runs else 0)


@pytest.mark.asyncio
async def test_legacy_writers_stay_enabled_without_the_option(
    fixture_now, recorder_mock, enable_custom_integrations, hass
):
    entry = await _setup_entry(hass)
    assert CONF_STATISTICS_TARGET not in entry.data
    assert hass.data[DOMAIN][entry.entry_id].legacy_statistics_enabled is True


@pytest.mark.asyncio
async def test_options_flow_offers_and_saves_the_statistics_target(
    fixture_now, recorder_mock, enable_custom_integrations, hass
):
    entry = await _setup_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    field = next(key for key in result["data_schema"].schema if key == CONF_STATISTICS_TARGET)
    assert field.default() == "shadow"

    user_input = _full_options_input(entry, result)
    user_input[CONF_STATISTICS_TARGET] = "live"
    with patch(SESSION, return_value=make_mock_session()):
        result = await hass.config_entries.options.async_configure(result["flow_id"], user_input=user_input)
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_STATISTICS_TARGET] == "live"
    assert hass.data[DOMAIN][entry.entry_id].legacy_statistics_enabled is False


@pytest.mark.asyncio
async def test_options_flow_rejects_an_unknown_target(fixture_now, recorder_mock, enable_custom_integrations, hass):
    entry = await _setup_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    user_input = _full_options_input(entry, result)
    user_input[CONF_STATISTICS_TARGET] = "everything"
    with pytest.raises(InvalidData) as error:
        await hass.config_entries.options.async_configure(result["flow_id"], user_input=user_input)
    assert set(error.value.schema_errors) == {CONF_STATISTICS_TARGET}
    assert CONF_STATISTICS_TARGET not in entry.data
