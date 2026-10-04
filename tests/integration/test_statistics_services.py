"""Integration tests for the probe_retention, sync_statistics, verify_statistics and export_statistics actions."""

from pathlib import Path
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sensus_analytics.const import DOMAIN

from .conftest import config_entry_data, make_mock_session

STATISTICS_SERVICES = ("probe_retention", "sync_statistics", "verify_statistics", "export_statistics")


async def _setup_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=config_entry_data(unit_type="gal"))
    entry.add_to_hass(hass)
    with patch(
        "custom_components.sensus_analytics.coordinator.requests.Session",
        return_value=make_mock_session(),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _call(hass, service, data):
    with patch(
        "custom_components.sensus_analytics.coordinator.requests.Session",
        return_value=make_mock_session(),
    ):
        return await hass.services.async_call(DOMAIN, service, data, blocking=True, return_response=True)


@pytest.mark.asyncio
async def test_setup_attaches_an_importer_and_registers_the_services(
    fixture_now, recorder_mock, enable_custom_integrations, hass
):
    entry = await _setup_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.statistics_importer.statistic_id.startswith(f"{DOMAIN}:")
    for service in STATISTICS_SERVICES:
        assert hass.services.has_service(DOMAIN, service)


@pytest.mark.asyncio
async def test_probe_retention_returns_a_response_per_entry(
    fixture_now, recorder_mock, enable_custom_integrations, hass
):
    entry = await _setup_entry(hass)
    response = await _call(hass, "probe_retention", {})
    result = response["entries"][entry.entry_id]
    assert result["ok"] is True
    assert result["oldest_hourly_day"] is not None


@pytest.mark.asyncio
async def test_verify_statistics_on_an_empty_statistic_is_ok(
    fixture_now, recorder_mock, enable_custom_integrations, hass
):
    entry = await _setup_entry(hass)
    response = await _call(hass, "verify_statistics", {"start_date": "2026-07-01"})
    assert response["entries"][entry.entry_id] == {
        "ok": True,
        "rows_checked": 0,
        "first_bad_hour": None,
        "problem": None,
    }


@pytest.mark.asyncio
async def test_sync_statistics_reports_its_result(fixture_now, recorder_mock, enable_custom_integrations, hass):
    entry = await _setup_entry(hass)
    response = await _call(hass, "sync_statistics", {"start_date": fixture_now.date().isoformat()})
    result = response["entries"][entry.entry_id]
    assert set(result) >= {"ok", "reason", "rows_written", "error", "verify"}
    assert result["reason"] == "service"


@pytest.mark.asyncio
async def test_export_statistics_writes_a_file_and_reports_it(
    fixture_now, recorder_mock, enable_custom_integrations, hass, tmp_path
):
    hass.config.config_dir = str(tmp_path)
    entry = await _setup_entry(hass)
    response = await _call(hass, "export_statistics", {"config_entry_id": entry.entry_id})
    result = response["entries"][entry.entry_id]
    assert result["ok"] is True
    assert result["path"].startswith(str(tmp_path))
    assert Path(result["path"]).is_file()


@pytest.mark.asyncio
async def test_services_are_removed_with_the_last_entry(fixture_now, recorder_mock, enable_custom_integrations, hass):
    entry = await _setup_entry(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    for service in STATISTICS_SERVICES:
        assert not hass.services.has_service(DOMAIN, service)
