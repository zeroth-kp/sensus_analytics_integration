"""Initialize the Sensus Analytics Integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN
from .coordinator import SensusAnalyticsDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_BACKFILL_HOURLY = "backfill_hourly_statistics"
ATTR_HOURS = "hours"

BACKFILL_HOURLY_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_HOURS, default=24): vol.All(vol.Coerce(int), vol.Range(min=1, max=168)),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Sensus Analytics from a config entry."""
    coordinator = SensusAnalyticsDataUpdateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])

    _async_register_services(hass)

    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration-level services (once)."""
    if hass.services.has_service(DOMAIN, SERVICE_BACKFILL_HOURLY):
        return

    async def _handle_backfill(call: ServiceCall) -> None:
        """Backfill the last N hours of hourly statistics for all entries."""
        hours = call.data[ATTR_HOURS]
        coordinators = hass.data.get(DOMAIN, {})
        if not coordinators:
            _LOGGER.warning("Backfill requested but no Sensus Analytics entries are loaded")
            return
        for coordinator in coordinators.values():
            imported = await coordinator.async_backfill_hourly_statistics(hours)
            _LOGGER.info("Backfilled %s hourly statistics row(s)", imported)

    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKFILL_HOURLY,
        _handle_backfill,
        schema=BACKFILL_HOURLY_SCHEMA,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Sensus Analytics config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        # Remove the service once the last config entry is gone.
        if not hass.data[DOMAIN] and hass.services.has_service(DOMAIN, SERVICE_BACKFILL_HOURLY):
            hass.services.async_remove(DOMAIN, SERVICE_BACKFILL_HOURLY)

    return unload_ok
