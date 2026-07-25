"""Initialize the Sensus Analytics Integration."""

from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN
from .coordinator import SensusAnalyticsDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

ATTR_CONFIG_ENTRY_ID = "config_entry_id"

# How often the Daily Usage sensor's long-term statistics are refreshed
# automatically. It has no native state_class (see sensor.py), so without
# this its statistics would only ever be as fresh as the last manual
# backfill_daily_history call.
DAILY_STATS_REFRESH_INTERVAL = timedelta(hours=24)

SERVICE_BACKFILL_HOURLY = "backfill_hourly_statistics"
ATTR_HOURS = "hours"

BACKFILL_HOURLY_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_HOURS, default=24): vol.All(vol.Coerce(int), vol.Range(min=1, max=168)),
        vol.Optional(ATTR_CONFIG_ENTRY_ID): str,
    }
)

SERVICE_BACKFILL_DAILY_HISTORY = "backfill_daily_history"
ATTR_CUTOVER_DATE = "cutover_date"

BACKFILL_DAILY_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CUTOVER_DATE): cv.date,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): str,
    }
)


def _select_coordinators(coordinators: dict, config_entry_id: str | None) -> list:
    """Return the coordinators a service call should act on.

    None -> all loaded coordinators (today's broadcast behavior, preserved
    as the default so single-account households don't need to change
    anything). A provided id -> a single-item list, or [] if no matching
    coordinator is loaded - callers must treat [] as an error, not a
    silent no-op, so a stale/mistyped id on a multi-account setup doesn't
    silently apply to the wrong account.
    """
    if config_entry_id is None:
        return list(coordinators.values())
    coordinator = coordinators.get(config_entry_id)
    return [coordinator] if coordinator else []


def _resolve_targets(hass: HomeAssistant, config_entry_id: str | None, log_prefix: str) -> list:
    """Resolve which coordinators a service call should act on, or [] if none are loaded.

    Raises ServiceValidationError if a config_entry_id was given but doesn't
    match any loaded entry - a stale/mistyped id on a multi-account setup
    should fail loudly, not silently apply to the wrong account.
    """
    coordinators = hass.data.get(DOMAIN, {})
    if not coordinators:
        _LOGGER.warning("%s requested but no Sensus Analytics entries are loaded", log_prefix)
        return []
    targets = _select_coordinators(coordinators, config_entry_id)
    if not targets:
        raise ServiceValidationError(
            f"No loaded Sensus Analytics config entry matches config_entry_id={config_entry_id!r}"
        )
    return targets


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Sensus Analytics from a config entry."""
    coordinator = SensusAnalyticsDataUpdateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])

    _async_register_services(hass)

    async def _scheduled_daily_refresh(_now=None) -> None:
        try:
            await coordinator.async_refresh_recent_daily_statistics()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Scheduled daily statistics refresh failed")

    entry.async_on_unload(async_track_time_interval(hass, _scheduled_daily_refresh, DAILY_STATS_REFRESH_INTERVAL))
    # Run once immediately so statistics don't wait a full refresh interval
    # to catch up after setup.
    hass.async_create_task(_scheduled_daily_refresh())

    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration-level services (once)."""
    if not hass.services.has_service(DOMAIN, SERVICE_BACKFILL_HOURLY):

        async def _handle_backfill(call: ServiceCall) -> None:
            """Backfill the last N hours of hourly statistics for the targeted entries."""
            hours = call.data[ATTR_HOURS]
            config_entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
            for coordinator in _resolve_targets(hass, config_entry_id, "Backfill"):
                imported = await coordinator.async_backfill_hourly_statistics(hours)
                _LOGGER.info("Backfilled %s hourly statistics row(s)", imported)

        hass.services.async_register(
            DOMAIN,
            SERVICE_BACKFILL_HOURLY,
            _handle_backfill,
            schema=BACKFILL_HOURLY_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_BACKFILL_DAILY_HISTORY):

        async def _handle_backfill_daily_history(call: ServiceCall) -> None:
            """Backfill historical daily/monthly statistics for the targeted entries."""
            cutover_date = call.data[ATTR_CUTOVER_DATE]
            config_entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
            for coordinator in _resolve_targets(hass, config_entry_id, "Daily history backfill"):
                imported = await coordinator.async_backfill_daily_history(cutover_date)
                _LOGGER.info("Backfilled %s daily/monthly statistics row(s)", imported)

        hass.services.async_register(
            DOMAIN,
            SERVICE_BACKFILL_DAILY_HISTORY,
            _handle_backfill_daily_history,
            schema=BACKFILL_DAILY_HISTORY_SCHEMA,
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Sensus Analytics config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        # Remove the services once the last config entry is gone.
        if not hass.data[DOMAIN]:
            if hass.services.has_service(DOMAIN, SERVICE_BACKFILL_HOURLY):
                hass.services.async_remove(DOMAIN, SERVICE_BACKFILL_HOURLY)
            if hass.services.has_service(DOMAIN, SERVICE_BACKFILL_DAILY_HISTORY):
                hass.services.async_remove(DOMAIN, SERVICE_BACKFILL_DAILY_HISTORY)

    return unload_ok
