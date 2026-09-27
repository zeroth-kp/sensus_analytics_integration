"""Config flow for Sensus Analytics Integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_BASE_URL,
    CONF_HOUR_SETTLE_DELAY_MINUTES,
    CONF_METER_NUMBER,
    CONF_PASSWORD,
    CONF_STATISTICS_TARGET,
    CONF_USERNAME,
    DEFAULT_HOUR_SETTLE_DELAY_MINUTES,
    DEFAULT_STATISTICS_TARGET,
    DOMAIN,
    STATISTICS_TARGETS,
)
from .coordinator import SensusAuthError, SensusFetchError, authenticated_session

_LOGGER = logging.getLogger(__name__)


class SensusAnalyticsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sensus Analytics Integration."""

    VERSION = 1

    def is_matching(self, other_flow):
        """Determine if this flow matches another flow."""
        # Implement matching logic if necessary
        return False  # Return False if you don't have specific matching logic

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            _LOGGER.debug("User input: %s", user_input)
            # Set a unique ID based on account and meter number
            unique_id = f"{user_input[CONF_ACCOUNT_NUMBER]}_{user_input[CONF_METER_NUMBER]}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            if error := await self._async_check_credentials(user_input):
                errors["base"] = error
            else:
                return self.async_create_entry(title="Sensus Analytics", data=user_input)

        data_schema = vol.Schema(
            {
                vol.Required(CONF_BASE_URL): str,
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_ACCOUNT_NUMBER): str,
                vol.Required(CONF_METER_NUMBER): str,
                vol.Required("unit_type", default="CCF"): vol.In(["CCF", "gal"]),
                vol.Optional("included_gallons"): cv.positive_float,
                vol.Optional("tier1_gallons"): cv.positive_float,
                vol.Required("tier1_price", default=12.80): cv.positive_float,
                vol.Optional("tier2_gallons"): cv.positive_float,
                vol.Optional("tier2_price"): cv.positive_float,
                vol.Optional("tier3_gallons"): cv.positive_float,
                vol.Optional("tier3_price"): cv.positive_float,
                vol.Optional("tier4_price"): cv.positive_float,
                vol.Required("service_fee", default=15.00): cv.positive_float,
                vol.Optional(
                    CONF_HOUR_SETTLE_DELAY_MINUTES,
                    default=DEFAULT_HOUR_SETTLE_DELAY_MINUTES,
                ): cv.positive_int,
            }
        )
        return self.async_show_form(step_id="user", data_schema=data_schema, errors=errors)

    async def _async_check_credentials(self, data) -> str | None:
        """Try to log in; return an error key for the form, or None on success."""
        try:
            await self.hass.async_add_executor_job(
                authenticated_session, data[CONF_BASE_URL], data[CONF_USERNAME], data[CONF_PASSWORD]
            )
        except SensusAuthError:
            return "auth"
        except SensusFetchError as error:
            _LOGGER.warning("Could not reach Sensus Analytics to check credentials: %s", error)
            return "cannot_connect"
        return None

    async def async_step_reauth(self, _entry_data) -> FlowResult:
        """Start reauthentication after Sensus rejected the stored credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None) -> FlowResult:
        """Ask for new credentials, check them, then update and reload the entry."""
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            if error := await self._async_check_credentials(data):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=entry.data.get(CONF_USERNAME)): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(_config_entry):
        """Get the options flow for this handler."""
        return SensusAnalyticsOptionsFlow()


class SensusAnalyticsOptionsFlow(config_entries.OptionsFlow):
    """Handle Sensus Analytics options."""

    async def async_step_init(self, user_input=None) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            _LOGGER.debug("User updated options: %s", user_input)
            # Update the entry with new options
            self.hass.config_entries.async_update_entry(self.config_entry, data=user_input)
            # Force a sensor refresh
            coordinator = self.hass.data[DOMAIN][self.config_entry.entry_id]
            await coordinator.async_request_refresh()
            return self.async_create_entry(title="", data={})

        # Fetch current configuration data
        current_data = self.config_entry.data

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_BASE_URL,
                    default=current_data.get(CONF_BASE_URL),
                ): str,
                vol.Required(
                    CONF_USERNAME,
                    default=current_data.get(CONF_USERNAME),
                ): str,
                vol.Required(
                    CONF_PASSWORD,
                    default=current_data.get(CONF_PASSWORD),
                ): str,
                vol.Required(
                    CONF_ACCOUNT_NUMBER,
                    default=current_data.get(CONF_ACCOUNT_NUMBER),
                ): str,
                vol.Required(
                    CONF_METER_NUMBER,
                    default=current_data.get(CONF_METER_NUMBER),
                ): str,
                vol.Required(
                    "unit_type",
                    default=current_data.get("unit_type", "CCF"),
                ): vol.In(["CCF", "gal"]),
                # Pre-fill optional pricing fields via suggested_value rather than
                # default, so an unset field is left out instead of validated as None.
                vol.Optional(
                    "included_gallons",
                    description={"suggested_value": current_data.get("included_gallons")},
                ): cv.positive_float,
                vol.Optional(
                    "tier1_gallons",
                    description={"suggested_value": current_data.get("tier1_gallons")},
                ): cv.positive_float,
                vol.Required(
                    "tier1_price",
                    default=current_data.get("tier1_price", 12.80),
                ): cv.positive_float,
                vol.Optional(
                    "tier2_gallons",
                    description={"suggested_value": current_data.get("tier2_gallons")},
                ): cv.positive_float,
                vol.Optional(
                    "tier2_price",
                    description={"suggested_value": current_data.get("tier2_price")},
                ): cv.positive_float,
                vol.Optional(
                    "tier3_gallons",
                    description={"suggested_value": current_data.get("tier3_gallons")},
                ): cv.positive_float,
                vol.Optional(
                    "tier3_price",
                    description={"suggested_value": current_data.get("tier3_price")},
                ): cv.positive_float,
                vol.Optional(
                    "tier4_price",
                    description={"suggested_value": current_data.get("tier4_price")},
                ): cv.positive_float,
                vol.Required(
                    "service_fee",
                    default=current_data.get("service_fee", 15.00),
                ): cv.positive_float,
                vol.Optional(
                    CONF_HOUR_SETTLE_DELAY_MINUTES,
                    default=current_data.get(CONF_HOUR_SETTLE_DELAY_MINUTES, DEFAULT_HOUR_SETTLE_DELAY_MINUTES),
                ): cv.positive_int,
                vol.Required(
                    CONF_STATISTICS_TARGET,
                    default=current_data.get(CONF_STATISTICS_TARGET, DEFAULT_STATISTICS_TARGET),
                ): vol.In(STATISTICS_TARGETS),
            }
        )

        return self.async_show_form(step_id="init", data_schema=data_schema)
