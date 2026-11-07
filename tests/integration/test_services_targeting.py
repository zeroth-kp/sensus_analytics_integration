"""Integration tests for the per-config-entry targeting of the services.

Every service takes an optional config_entry_id. Without one it applies to
every loaded Sensus Analytics config entry; with one it applies to that entry
only, so a household with two accounts can act on one of them.
"""

from datetime import date
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sensus_analytics.const import DOMAIN

from .conftest import config_entry_data, make_mock_session

START_DATE = date(2026, 7, 1)


async def _setup_entry(hass, account_number):
    entry = MockConfigEntry(domain=DOMAIN, data=config_entry_data(account_number=account_number))
    entry.add_to_hass(hass)
    with patch(
        "custom_components.sensus_analytics.coordinator.requests.Session",
        return_value=make_mock_session(),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _mock_verify(hass, entry):
    importer = hass.data[DOMAIN][entry.entry_id].statistics_importer
    return patch.object(importer, "async_verify", AsyncMock(return_value=Mock(as_dict=dict)))


@pytest.mark.asyncio
async def test_verify_with_no_config_entry_id_targets_all_accounts(recorder_mock, enable_custom_integrations, hass):
    entry_a = await _setup_entry(hass, "acct-a")
    entry_b = await _setup_entry(hass, "acct-b")

    with _mock_verify(hass, entry_a) as mock_a, _mock_verify(hass, entry_b) as mock_b:
        response = await hass.services.async_call(
            DOMAIN, "verify_statistics", {"start_date": START_DATE}, blocking=True, return_response=True
        )

    mock_a.assert_awaited_once()
    mock_b.assert_awaited_once()
    assert set(response["entries"]) == {entry_a.entry_id, entry_b.entry_id}


@pytest.mark.asyncio
async def test_verify_with_config_entry_id_targets_only_that_account(recorder_mock, enable_custom_integrations, hass):
    entry_a = await _setup_entry(hass, "acct-a")
    entry_b = await _setup_entry(hass, "acct-b")

    with _mock_verify(hass, entry_a) as mock_a, _mock_verify(hass, entry_b) as mock_b:
        response = await hass.services.async_call(
            DOMAIN,
            "verify_statistics",
            {"start_date": START_DATE, "config_entry_id": entry_a.entry_id},
            blocking=True,
            return_response=True,
        )

    mock_a.assert_awaited_once()
    mock_b.assert_not_awaited()
    assert set(response["entries"]) == {entry_a.entry_id}


@pytest.mark.asyncio
async def test_verify_with_unknown_config_entry_id_raises(recorder_mock, enable_custom_integrations, hass):
    await _setup_entry(hass, "acct-a")

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "verify_statistics",
            {"start_date": START_DATE, "config_entry_id": "does-not-exist"},
            blocking=True,
            return_response=True,
        )
