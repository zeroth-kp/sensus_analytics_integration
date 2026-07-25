"""Integration tests for the per-config-entry targeting of the two backfill services.

Regression coverage for the fix where both services used to broadcast to
every loaded Sensus Analytics config entry unconditionally - a household
with two accounts calling either service would apply it to both accounts
at once.
"""

from unittest.mock import patch

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sensus_analytics.const import DOMAIN

from .conftest import config_entry_data, make_mock_session


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


@pytest.mark.asyncio
async def test_backfill_with_no_config_entry_id_targets_all_accounts(recorder_mock, enable_custom_integrations, hass):
    entry_a = await _setup_entry(hass, "acct-a")
    entry_b = await _setup_entry(hass, "acct-b")

    coordinator_a = hass.data[DOMAIN][entry_a.entry_id]
    coordinator_b = hass.data[DOMAIN][entry_b.entry_id]

    with (
        patch.object(coordinator_a, "async_backfill_hourly_statistics", return_value=1) as mock_a,
        patch.object(coordinator_b, "async_backfill_hourly_statistics", return_value=1) as mock_b,
    ):
        await hass.services.async_call(DOMAIN, "backfill_hourly_statistics", {"hours": 24}, blocking=True)

    mock_a.assert_called_once()
    mock_b.assert_called_once()


@pytest.mark.asyncio
async def test_backfill_with_config_entry_id_targets_only_that_account(recorder_mock, enable_custom_integrations, hass):
    entry_a = await _setup_entry(hass, "acct-a")
    entry_b = await _setup_entry(hass, "acct-b")

    coordinator_a = hass.data[DOMAIN][entry_a.entry_id]
    coordinator_b = hass.data[DOMAIN][entry_b.entry_id]

    with (
        patch.object(coordinator_a, "async_backfill_hourly_statistics", return_value=1) as mock_a,
        patch.object(coordinator_b, "async_backfill_hourly_statistics", return_value=1) as mock_b,
    ):
        await hass.services.async_call(
            DOMAIN,
            "backfill_hourly_statistics",
            {"hours": 24, "config_entry_id": entry_a.entry_id},
            blocking=True,
        )

    mock_a.assert_called_once()
    mock_b.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_with_unknown_config_entry_id_raises(recorder_mock, enable_custom_integrations, hass):
    await _setup_entry(hass, "acct-a")

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "backfill_hourly_statistics",
            {"hours": 24, "config_entry_id": "does-not-exist"},
            blocking=True,
        )
