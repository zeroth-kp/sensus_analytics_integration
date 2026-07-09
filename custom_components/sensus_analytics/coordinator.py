"""DataUpdateCoordinator for Sensus Analytics Integration."""

import logging
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import async_import_statistics, statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import CONF_ACCOUNT_NUMBER, CONF_BASE_URL, CONF_METER_NUMBER, CONF_PASSWORD, CONF_USERNAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

# StatisticMetaData replaced the ``has_mean`` bool with ``mean_type`` during the
# 2025.x cycle. Import the enum when available and fall back for older cores so
# the backfill works across HA versions.
try:  # HA >= 2025.2
    from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData

    _MEAN_NONE = StatisticMeanType.NONE
except ImportError:  # pragma: no cover - older HA cores
    from homeassistant.components.recorder.models import StatisticData, StatisticMetaData

    _MEAN_NONE = None

# Usage conversion constants (mirror sensor.py; kept local to avoid importing
# the sensor module from the coordinator).
CF_TO_GALLON = 7.48052
CF_PER_CCF = 100  # 1 CCF = 100 cubic feet


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

        except UpdateFailed as error:
            raise error
        except Exception as error:
            _LOGGER.error("Unexpected error: %s", error)
            raise UpdateFailed(f"Unexpected error: {error}") from error

    def _create_authenticated_session(self):
        """Create and return an authenticated session."""
        session = requests.Session()
        # Authenticate and get session cookie
        login_url = urljoin(self.base_url, "j_spring_security_check")
        _LOGGER.debug("Authentication URL: %s", login_url)
        r_sec = session.post(
            login_url,
            data={"j_username": self.username, "j_password": self.password},
            allow_redirects=False,
            timeout=10,
        )
        # Check if login was successful
        if r_sec.status_code != 302:
            _LOGGER.error("Authentication failed with status code %s", r_sec.status_code)
            raise UpdateFailed("Authentication failed")

        _LOGGER.debug("Authentication successful")
        return session

    def _fetch_daily_data(self, session):
        """Fetch daily meter data."""
        widget_url = urljoin(self.base_url, "water/widget/byPage")
        _LOGGER.debug("Widget URL: %s", widget_url)
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
        data = response.json()
        _LOGGER.debug("Raw response data: %s", data)
        # Navigate to the specific data
        data = data.get("widgetList")[0].get("data").get("devices")[0]
        _LOGGER.debug("Parsed data: %s", data)
        return data

    def _retrieve_hourly_data(self, session: requests.Session, target_date: datetime):
        """Retrieve hourly usage data for a specific date based on local time."""
        # Prepare request parameters
        start_ts, end_ts = self._get_start_end_timestamps(target_date)
        usage_url, params = self._construct_hourly_data_request(start_ts, end_ts)

        _LOGGER.debug("Hourly data request URL: %s", usage_url)
        _LOGGER.debug("Hourly data request parameters: %s", params)

        try:
            response = session.get(usage_url, params=params, timeout=10)
            response.raise_for_status()
            hourly_data = response.json()
            _LOGGER.debug("Hourly data response: %s", hourly_data)

            # Validate and process the response
            hourly_entries = self._process_hourly_data_response(hourly_data)
            return hourly_entries

        except requests.exceptions.RequestException as e:
            _LOGGER.error("Hourly data retrieval failed: %s", e)
            return None
        except (KeyError, TypeError, ValueError) as e:
            _LOGGER.error("Error processing the hourly data response: %s", e)
            return None

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

    def _process_hourly_data_response(self, hourly_data):
        """Process and structure the hourly data response."""
        if not isinstance(hourly_data, dict):
            _LOGGER.error("Unexpected response format for hourly data.")
            return None

        if not hourly_data.get("operationSuccess", False):
            errors = hourly_data.get("errors", [])
            _LOGGER.error("API returned errors: %s", errors)
            return None

        usage_list = hourly_data.get("data", {}).get("usage", [])
        if not usage_list or len(usage_list) < 2:
            _LOGGER.error("Hourly usage data is missing or incomplete.")
            return None

        # The first element contains units
        units = usage_list[0]  # ["CCF", "INCHES", "FAHRENHEIT", "gal"]
        usage_unit = units[0]
        rain_unit = units[1]
        temp_unit = units[2]

        # The rest of the list contains hourly data
        hourly_entries = []
        for entry in usage_list[1:]:
            timestamp, usage, rain, temp = entry[:4]
            hourly_entries.append(
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

        return hourly_entries

    # ------------------------------------------------------------------
    # One-time hourly-statistics backfill
    #
    # Older versions always fetched *yesterday's* hourly array and matched
    # today's hour-of-day against it, so the Last Hour Usage sensor persisted
    # yesterday's usage shape under today's timestamps. This backfill re-imports
    # the last N hours using each entry's real timestamp, overwriting the
    # mislabeled long-term statistics rows for that sensor.
    # ------------------------------------------------------------------

    async def async_backfill_hourly_statistics(self, hours: int = 24) -> int:
        """Backfill the Last Hour Usage long-term statistics for the last N hours.

        Returns the number of hourly statistics rows imported.
        """
        entries = await self.hass.async_add_executor_job(self._fetch_hourly_window, hours)
        if not entries:
            _LOGGER.warning("Hourly backfill: no hourly data available to import")
            return 0

        statistic_id = self._resolve_usage_statistic_id()
        config_unit = self.config_entry.data.get("unit_type")
        unit = config_unit if config_unit in ("gal", "CCF") else None

        first_start = self._floor_to_hour_utc(entries[0]["timestamp"])
        baseline_sum = await self._get_baseline_sum(statistic_id, first_start)

        statistics = []
        running_sum = baseline_sum
        for entry in entries:
            value = self._convert_usage_value(entry["usage"], entry.get("usage_unit"))
            if value is None:
                continue
            start = self._floor_to_hour_utc(entry["timestamp"])
            running_sum += value
            statistics.append(
                StatisticData(
                    start=start,
                    state=value,
                    sum=running_sum,
                    last_reset=start,
                )
            )

        if not statistics:
            _LOGGER.warning("Hourly backfill: no convertible usage values found")
            return 0

        metadata = StatisticMetaData(
            has_sum=True,
            name=None,
            source="recorder",
            statistic_id=statistic_id,
            unit_of_measurement=unit,
        )
        # Populate the mean field under whichever key this HA core expects.
        if _MEAN_NONE is not None:
            metadata["mean_type"] = _MEAN_NONE
        else:
            metadata["has_mean"] = False
        async_import_statistics(self.hass, metadata, statistics)
        _LOGGER.info(
            "Hourly backfill: imported %s hourly statistics rows for %s",
            len(statistics),
            statistic_id,
        )
        return len(statistics)

    def _fetch_hourly_window(self, hours: int):
        """Fetch and merge the last ``hours`` of hourly entries (runs in executor)."""
        session = self._create_authenticated_session()
        local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
        now_local = datetime.now(local_tz)

        # Pull enough calendar days to fully cover the requested window. Add one
        # extra day so a window that straddles midnight is always complete.
        days_to_fetch = (hours // 24) + 2
        combined = {}
        for day_offset in range(days_to_fetch):
            target_date = now_local - timedelta(days=day_offset)
            day_entries = self._retrieve_hourly_data(session, target_date)
            if not day_entries:
                continue
            for entry in day_entries:
                # Dedupe by timestamp (today/yesterday windows can overlap).
                combined[entry["timestamp"]] = entry

        cutoff_ms = int((now_local - timedelta(hours=hours)).timestamp() * 1000)
        window = [entry for ts, entry in combined.items() if ts >= cutoff_ms]
        window.sort(key=lambda entry: entry["timestamp"])
        return window

    def _resolve_usage_statistic_id(self) -> str:
        """Return the entity_id (statistic_id) of the Last Hour Usage sensor."""
        unique_id = f"{DOMAIN}_{self.config_entry.entry_id}_last_hour_usage"
        entity_registry = er.async_get(self.hass)
        entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        return entity_id or "sensor.sensus_analytics_last_hour_usage"

    async def _get_baseline_sum(self, statistic_id: str, window_start: datetime) -> float:
        """Return the cumulative sum of the hour immediately before the window."""
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            window_start - timedelta(hours=1),
            window_start,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = stats.get(statistic_id) if stats else None
        if rows:
            return rows[-1].get("sum") or 0.0
        return 0.0

    @staticmethod
    def _floor_to_hour_utc(timestamp_ms: int) -> datetime:
        """Convert a ms epoch timestamp to a UTC datetime floored to the hour."""
        return dt_util.utc_from_timestamp(timestamp_ms / 1000).replace(minute=0, second=0, microsecond=0)

    def _convert_usage_value(self, usage, usage_unit):
        """Convert a native usage value to the configured unit (mirrors sensor.py)."""
        if usage is None:
            return None
        config_unit = self.config_entry.data.get("unit_type")
        try:
            usage_float = float(usage)
        except (ValueError, TypeError):
            return None

        if usage_unit == "CF" and config_unit == "gal":
            return round(usage_float * CF_TO_GALLON)
        if usage_unit == "CF" and config_unit == "CCF":
            return round(usage_float / CF_PER_CCF, 2)
        if usage_unit == "GAL" and config_unit == "gal":
            return usage_float
        if usage_unit == "GAL" and config_unit == "CCF":
            return round(usage_float / CF_TO_GALLON / CF_PER_CCF, 2)

        return usage_float
