"""DataUpdateCoordinator for Sensus Analytics Integration."""

import logging
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlsplit

import requests
from homeassistant.components.recorder.models import StatisticMeanType, StatisticMetaData
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_BASE_URL,
    CONF_METER_NUMBER,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def apply_sum_statistic_fields(metadata: StatisticMetaData) -> StatisticMetaData:
    """Set the mean type and unit class a volume sum statistic needs."""
    metadata["mean_type"] = StatisticMeanType.NONE
    metadata["unit_class"] = VolumeConverter.UNIT_CLASS
    return metadata


class SensusFetchError(Exception):
    """A Sensus request failed at the transport or response-format level."""


class SensusAuthError(SensusFetchError):
    """Sensus rejected the username or password."""


def check_login_response(status: int, location: str) -> None:
    """Classify a Sensus login response; raise unless it is a successful login.

    Sensus redirects on success, and on rejected credentials either redirects
    back to the login page with an error marker (e.g. ``?error`` or
    ``login.html#/failed`` - the marker can be in the fragment) or re-renders
    the login form. Any other status (5xx, 429, a firewall's 403, ...) is a
    service problem, not a credential problem, so it must not force a reauth.
    """
    if status == 302:
        redirect = urlsplit(location or "")
        markers = f"{redirect.query}#{redirect.fragment}".lower()
        if "error" in markers or "fail" in markers:
            raise SensusAuthError("Sensus Analytics rejected the username or password")
        return
    if status in (200, 401):
        raise SensusAuthError(f"Authentication failed with status {status}")
    raise SensusFetchError(f"Authentication request returned unexpected status {status}")


def authenticated_session(base_url: str, username: str, password: str) -> requests.Session:
    """Log in to Sensus and return the session (blocking; run in an executor)."""
    session = requests.Session()
    try:
        response = session.post(
            urljoin(base_url, "j_spring_security_check"),
            data={"j_username": username, "j_password": password},
            allow_redirects=False,
            timeout=10,
        )
    except requests.exceptions.RequestException as error:
        raise SensusFetchError(f"Authentication request failed: {error}") from error
    check_login_response(response.status_code, response.headers.get("Location", ""))
    return session


def parse_hourly_rows(usage_list: list) -> list[dict]:
    """Convert Sensus's ``[units, [ts, usage, rain, temp], ...]`` list into entry dicts."""
    units = usage_list[0]
    if not isinstance(units, list) or not units:
        raise SensusFetchError("Hourly data response did not include units")
    usage_unit, rain_unit, temp_unit = (units + [None, None])[:3]
    entries = []
    for row in usage_list[1:]:
        if not isinstance(row, list) or len(row) < 2:
            raise SensusFetchError("Hourly data response contained a malformed row")
        timestamp, usage, rain, temp = (row + [None, None])[:4]
        entries.append(
            {
                "timestamp": timestamp,
                "usage": usage,
                "rain": rain,
                "temp": temp,
                "usage_unit": usage_unit,
                "rain_unit": rain_unit,
                "temp_unit": temp_unit,
            }
        )
    return entries


class SensusAnalyticsDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(self, hass: HomeAssistant, config_entry):
        """Initialize."""
        self.hass = hass
        self.base_url = config_entry.data[CONF_BASE_URL]
        self.username = config_entry.data[CONF_USERNAME]
        self.password = config_entry.data[CONF_PASSWORD]
        self.account_number = config_entry.data[CONF_ACCOUNT_NUMBER]
        self.meter_number = config_entry.data[CONF_METER_NUMBER]
        self.config_entry = config_entry

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=5),
        )

    async def _async_update_data(self):
        """Fetch data from API."""
        _LOGGER.debug("Async update of data started")
        return await self.hass.async_add_executor_job(self._fetch_data)

    def _fetch_data(self):
        """Fetch data from the Sensus Analytics API."""
        _LOGGER.debug("Starting data fetch from Sensus Analytics API")
        try:
            session = self._create_authenticated_session()

            # Fetch daily data
            data = self._fetch_daily_data(session)

            # Fetch hourly data. Sensus now publishes same-day hourly data on
            # demand, so try today first and only fall back to yesterday if
            # today's data isn't available yet (preserves prior behavior for
            # accounts/times where it still lags).
            _LOGGER.debug("Fetching hourly data")
            local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
            now_local = datetime.now(local_tz)
            hourly_data = self._retrieve_hourly_data(session, now_local)
            if not hourly_data:
                target_date = now_local - timedelta(days=1)
                hourly_data = self._retrieve_hourly_data(session, target_date)

            if hourly_data:
                data["hourly_usage_data"] = hourly_data
            else:
                _LOGGER.warning("Failed to fetch hourly data")

            return data

        except SensusAuthError as error:
            raise ConfigEntryAuthFailed(str(error)) from error
        except SensusFetchError as error:
            # Outages and maintenance pages are expected; the coordinator
            # logs the first failure and the recovery, so no traceback here.
            raise UpdateFailed(str(error)) from error
        except Exception as error:
            _LOGGER.error("Unexpected error: %s", error)
            raise UpdateFailed(f"Unexpected error: {error}") from error

    def _create_authenticated_session(self):
        """Create and return an authenticated session."""
        return authenticated_session(self.base_url, self.username, self.password)

    def _fetch_daily_data(self, session):
        """Fetch daily meter data."""
        widget_url = urljoin(self.base_url, "water/widget/byPage")
        try:
            response = session.post(
                widget_url,
                json={
                    "group": "meters",
                    "accountNumber": self.account_number,
                    "deviceId": self.meter_number,
                },
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.exceptions.RequestException as error:
            raise SensusFetchError(f"Daily data request failed: {error}") from error
        except ValueError as error:
            # An HTML maintenance or login page instead of JSON
            raise SensusFetchError("Daily data response was not valid JSON") from error
        _LOGGER.debug("Raw response data: %s", payload)
        try:
            widget_data = payload["widgetList"][0]["data"]
        except (KeyError, IndexError, TypeError) as error:
            raise SensusFetchError("Daily data response did not contain meter data") from error
        # Sensus answers "nodata" with an empty device list while its backend is having trouble
        devices = widget_data.get("devices") if isinstance(widget_data, dict) else None
        if not devices:
            raise SensusFetchError(
                "Sensus Analytics returned no meter data; the service may be temporarily unavailable"
            )
        return devices[0]

    def _retrieve_hourly_data(self, session: requests.Session, target_date: datetime):
        """Return hourly entries for a local date, or None if there are none or the fetch failed."""
        try:
            entries = self.fetch_hourly_day(session, target_date)
        except SensusFetchError as error:
            _LOGGER.error("Hourly data retrieval failed: %s", error)
            return None
        if not entries:
            _LOGGER.error("Hourly usage data is missing, incomplete, or reported as unsuccessful.")
            return None
        return entries

    def _get_start_end_timestamps(self, target_date):
        """Get start and end timestamps in milliseconds for the target date."""
        # Use HA's local timezone
        local_tz = dt_util.get_time_zone(self.hass.config.time_zone)

        # Start and end of the day in local time with timezone
        start_dt = datetime.combine(target_date, datetime.min.time(), tzinfo=local_tz)
        end_dt = datetime.combine(target_date, datetime.max.time(), tzinfo=local_tz)

        # Convert to timestamps in milliseconds
        start_ts = int(start_dt.timestamp() * 1000)
        end_ts = int(end_dt.timestamp() * 1000)
        return start_ts, end_ts

    def _construct_hourly_data_request(self, start_ts, end_ts):
        """Construct the hourly data request URL and parameters."""
        usage_url = urljoin(self.base_url, f"water/usage/{self.account_number}/{self.meter_number}")
        params = {
            "start": start_ts,
            "end": end_ts,
            "zoom": "day",
            "page": "null",
            "weather": "1",
        }
        return usage_url, params

    def daily_usage_statistic_id(self) -> str:
        """Return the statistic id of the Daily Usage sensor."""
        return self._resolve_daily_usage_statistic_id()

    def last_hour_usage_statistic_id(self) -> str:
        """Return the statistic id of the Last Hour Usage sensor."""
        return self._resolve_usage_statistic_id()

    def open_session(self) -> requests.Session:
        """Return a newly authenticated session (blocking; run in an executor)."""
        return self._create_authenticated_session()

    def fetch_hourly_day(self, session: requests.Session, day) -> list | None:
        """Fetch one local day's hourly entries (blocking; run in an executor).

        - raises ``SensusFetchError`` for transport errors and malformed responses;
        - returns ``None`` when Sensus answers but reports the request as unsuccessful;
        - returns ``[]`` when Sensus answers successfully with no hourly rows.

        No filtering is applied - callers validate the requested window.
        """
        start_ts, end_ts = self._get_start_end_timestamps(day)
        usage_url, params = self._construct_hourly_data_request(start_ts, end_ts)
        try:
            response = session.get(usage_url, params=params, timeout=10)
            response.raise_for_status()
            payload = response.json()
        except requests.exceptions.RequestException as error:
            raise SensusFetchError(f"Hourly data request failed: {error}") from error
        except ValueError as error:
            raise SensusFetchError("Hourly data response was not valid JSON") from error

        if not isinstance(payload, dict):
            raise SensusFetchError("Hourly data response was not an object")
        if not payload.get("operationSuccess", False):
            return None

        data = payload.get("data")
        usage_list = data.get("usage") if isinstance(data, dict) else None
        if not usage_list or len(usage_list) < 2:
            return []
        return parse_hourly_rows(usage_list)

    def _resolve_usage_statistic_id(self) -> str:
        """Return the entity_id (statistic_id) of the Last Hour Usage sensor."""
        unique_id = f"{DOMAIN}_{self.config_entry.entry_id}_last_hour_usage"
        entity_registry = er.async_get(self.hass)
        entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        return entity_id or "sensor.sensus_analytics_last_hour_usage"

    def _resolve_daily_usage_statistic_id(self) -> str:
        """Return the entity_id (statistic_id) of the Daily Usage sensor."""
        unique_id = f"{DOMAIN}_{self.config_entry.entry_id}_daily_usage"
        entity_registry = er.async_get(self.hass)
        entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        return entity_id or "sensor.sensus_analytics_daily_usage"
