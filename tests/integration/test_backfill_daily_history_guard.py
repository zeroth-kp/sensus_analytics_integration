"""Integration tests for backfill_daily_history's old-cutover-date guard.

Regression coverage for a hardening fix: a stray or mistaken call with a
cutover_date reaching back multiple weeks reprocesses daily statistics for
days that should already be stable and settled, with no way to tell whether
the call was actually intended. Requiring an explicit confirm_old_cutover
for anything past a short threshold turns that into a loud validation error
instead of a silent rewrite.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sensus_analytics.const import DOMAIN

from .conftest import config_entry_data, make_mock_session

_TODAY = datetime(2025, 3, 20, tzinfo=timezone.utc)


async def _setup_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=config_entry_data())
    entry.add_to_hass(hass)
    with patch(
        "custom_components.sensus_analytics.coordinator.requests.Session",
        return_value=make_mock_session(),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@pytest.mark.asyncio
async def test_old_cutover_without_confirmation_raises_and_skips_backfill(
    recorder_mock, enable_custom_integrations, hass
):
    entry = await _setup_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    with (
        patch.object(coordinator, "async_backfill_daily_history", return_value=1) as mock_backfill,
        patch("custom_components.sensus_analytics.datetime") as mock_datetime,
    ):
        mock_datetime.now.return_value = _TODAY
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                "backfill_daily_history",
                {"cutover_date": "2025-02-01"},  # 47 days before _TODAY
                blocking=True,
            )

    mock_backfill.assert_not_called()


@pytest.mark.asyncio
async def test_old_cutover_with_confirmation_proceeds(recorder_mock, enable_custom_integrations, hass):
    entry = await _setup_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    with (
        patch.object(coordinator, "async_backfill_daily_history", return_value=1) as mock_backfill,
        patch("custom_components.sensus_analytics.datetime") as mock_datetime,
    ):
        mock_datetime.now.return_value = _TODAY
        await hass.services.async_call(
            DOMAIN,
            "backfill_daily_history",
            {"cutover_date": "2025-02-01", "confirm_old_cutover": True},
            blocking=True,
        )

    mock_backfill.assert_called_once()


@pytest.mark.asyncio
async def test_recent_cutover_does_not_require_confirmation(recorder_mock, enable_custom_integrations, hass):
    entry = await _setup_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    with (
        patch.object(coordinator, "async_backfill_daily_history", return_value=1) as mock_backfill,
        patch("custom_components.sensus_analytics.datetime") as mock_datetime,
    ):
        mock_datetime.now.return_value = _TODAY
        await hass.services.async_call(
            DOMAIN,
            "backfill_daily_history",
            {"cutover_date": "2025-03-11"},  # 9 days before _TODAY, within threshold
            blocking=True,
        )

    mock_backfill.assert_called_once()
