"""DataUpdateCoordinator for Sensus Analytics Integration."""

import logging
import math
from datetime import datetime, time, timedelta
from urllib.parse import urljoin, urlsplit

import requests
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import async_import_statistics, statistics_during_period
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
    CONF_STATISTICS_TARGET,
    CONF_USERNAME,
    DEFAULT_STATISTICS_TARGET,
    DOMAIN,
    STATISTICS_TARGET_LIVE,
)
from .usage_conversion import convert_usage_value

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

    @property
    def legacy_statistics_enabled(self) -> bool:
        """Return whether the legacy statistics writers may run.

        Disabled once the hourly statistics importer writes the live Daily
        Usage statistic, so the importer is that statistic's only writer.
        """
        target = self.config_entry.data.get(CONF_STATISTICS_TARGET, DEFAULT_STATISTICS_TARGET)
        return target != STATISTICS_TARGET_LIVE

    def _legacy_statistics_blocked(self, log_label: str) -> bool:
        """Log and return True when a legacy statistics writer must not run."""
        if self.legacy_statistics_enabled:
            return False
        _LOGGER.warning(
            "%s skipped: statistics_target is %r, so the hourly statistics importer is the only "
            "writer of water statistics. Use the sync_statistics action instead.",
            log_label,
            STATISTICS_TARGET_LIVE,
        )
        return True

    def daily_usage_statistic_id(self) -> str:
        """Return the statistic id of the Daily Usage sensor."""
        return self._resolve_daily_usage_statistic_id()

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
        if self._legacy_statistics_blocked("Hourly backfill"):
            return 0
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

        if not await self._verify_baseline_unchanged(
            statistic_id, first_start, baseline_sum, log_label="Hourly backfill"
        ):
            return 0

        return self._import_statistics(statistic_id, unit, statistics, "Hourly backfill")

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
        """Return the cumulative sum of the most recent hour before the window.

        Looks back up to 7 days, not just the single immediately-preceding
        hour - a fetch that comes back thinner than expected (e.g. only
        today's data, if yesterday's/day-before's API calls silently
        returned nothing) would otherwise make window_start land right at
        the start of real history, find no row in that single adjacent
        hour, and fall back to 0.0 - resetting the cumulative sum and
        corrupting every hour from there forward. This can happen when a
        fetch returns much less than the requested window (e.g. only
        same-day data instead of several days), leaving the 1-hour
        lookback nothing to anchor to.
        """
        existing = await self._get_existing_sum_before(statistic_id, window_start)
        return existing if existing is not None else 0.0

    async def _get_existing_sum_before(self, statistic_id: str, window_start: datetime):
        """Return the cumulative sum of the most recent hour before the window,
        or None if no statistics exist there at all (as opposed to legitimately
        being 0.0) - see _get_baseline_sum for the 7-day lookback rationale.
        """
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            window_start - timedelta(days=7),
            window_start,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = stats.get(statistic_id) if stats else None
        if rows:
            return rows[-1].get("sum") or 0.0
        return None

    async def _verify_baseline_unchanged(
        self, statistic_id: str, anchor: datetime, expected_sum, *, log_label: str
    ) -> bool:
        """Re-read the sum anchor immediately before writing and confirm nothing
        else has written to this statistic since the baseline was first read.

        Every write path in this module reads a baseline sum, then spends real
        wall-clock time (an HTTP fetch, sometimes a multi-page one) building the
        rows to import before finally writing them - a classic check-then-act
        race. If a different write lands on the same statistic in that gap (a
        concurrent service call, another automation, a manual
        ``recorder/adjust_sum_statistics`` correction), the in-flight write has
        no way to know and simply overwrites it using its now-stale baseline,
        silently discarding whatever the other write just committed.

        Best-effort, not a true compare-and-swap: ``async_import_statistics``
        only enqueues a job on the recorder's own queue rather than writing
        synchronously, so a write that landed moments ago may not be visible
        yet even to this re-read. This narrows the race window from "the
        entire fetch+build phase" down to "the gap between this check and the
        subsequent write," which is enough to catch the failure mode this
        guards against - independent operations are realistically minutes apart, not
        microseconds - but does not eliminate the race at the database level.

        ``expected_sum`` may be ``None`` or already coerced to ``0.0`` by a
        caller upstream (``_get_baseline_sum`` does this) - both mean "found
        nothing at the anchor," so both sides are coerced the same way here
        before comparing. Re-reading and finding a real nonzero value where
        there was none before is just as much a race as two disagreeing
        non-zero sums.
        """
        current_sum = await self._get_existing_sum_before(statistic_id, anchor)
        if math.isclose(current_sum or 0.0, expected_sum or 0.0, abs_tol=1e-6):
            return True
        _LOGGER.error(
            "%s: the cumulative sum baseline for %s changed (was %s, now %s) while this "
            "run was in progress - another write raced this one. Aborting without "
            "importing anything, since writing now would silently discard whatever the "
            "other write just committed. Re-run once nothing else is updating this "
            "statistic.",
            log_label,
            statistic_id,
            expected_sum,
            current_sum,
        )
        return False

    @staticmethod
    def _floor_to_hour_utc(timestamp_ms: int) -> datetime:
        """Convert a ms epoch timestamp to a UTC datetime floored to the hour."""
        return dt_util.utc_from_timestamp(timestamp_ms / 1000).replace(minute=0, second=0, microsecond=0)

    def _convert_usage_value(self, usage, usage_unit):
        """Convert a native usage value to the configured unit."""
        config_unit = self.config_entry.data.get("unit_type")
        return convert_usage_value(usage, usage_unit, config_unit)

    def _import_statistics(
        self, statistic_id: str, unit, statistics: list, log_label: str, *, level: int = logging.INFO
    ) -> int:
        """Build metadata and import statistics rows, logging the result.

        Shared by all three statistics-writing entry points (hourly
        backfill, daily backfill, and the recurring daily refresh).

        ``level`` defaults to INFO for the two high-frequency callers
        (hourly backfill runs every hour, the scheduled refresh every
        24h - logging those at WARNING by default would just be noise).
        ``async_backfill_daily_history`` overrides this to WARNING: it's
        rare, operator-invoked, and reprocesses every day from its
        cutover through today in one call - the highest blast-radius
        write this integration makes. At INFO, the only trace of a
        successful (or corrupting) run would be invisible at this
        integration's default WARNING log level; a completed run of
        *that* call should always be visible.
        """
        metadata = StatisticMetaData(
            has_sum=True,
            name=None,
            source="recorder",
            statistic_id=statistic_id,
            unit_of_measurement=unit,
        )
        apply_sum_statistic_fields(metadata)
        async_import_statistics(self.hass, metadata, statistics)
        _LOGGER.log(level, "%s: imported %s statistics row(s) for %s", log_label, len(statistics), statistic_id)
        return len(statistics)

    def _build_monthly_statistics(self, monthly_totals, starting_sum=0.0):
        """Build StatisticData rows for pre-aggregated monthly totals.

        Monthly rows are always a true historical gap - no existing hourly
        data can exist that far back - so a single row per month is
        sufficient (unlike daily entries, which may need existing hours
        flattened; see _build_daily_statistics).
        """
        statistics = []
        running_sum = starting_sum
        for start, usage, usage_unit in monthly_totals:
            value = self._convert_usage_value(usage, usage_unit)
            if value is None:
                continue
            running_sum += value
            statistics.append(StatisticData(start=start, state=value, sum=running_sum, last_reset=start))
        return statistics, running_sum

    # The fixed hour every day's corrected total lands on - see the
    # docstring below for why this replaced "whichever hour already
    # existed".
    _DAILY_CANONICAL_HOUR = time(23, 0)

    # Ceiling on a single day's usage, in gallons, above which a daily entry
    # is treated as implausible rather than imported. Sensus's pre-aggregated
    # monthly-total endpoint and its real daily-granularity endpoint can
    # disagree at a month boundary; in the worst case a full month's total
    # can end up misattributed to a single day, producing a one-day value
    # many times larger than any real residential meter would show. This
    # ceiling sits comfortably above the highest plausible single-day
    # reading for a household (including heavy irrigation use) while still
    # catching a misattributed monthly total, so a boundary disagreement is
    # skipped and logged loudly instead of silently corrupting the
    # cumulative sum chain.
    _MAX_PLAUSIBLE_DAILY_USAGE_GAL = 15000

    def _max_plausible_daily_value(self) -> float:
        """Return the sanity ceiling in the sensor's configured unit."""
        config_unit = self.config_entry.data.get("unit_type")
        converted = convert_usage_value(self._MAX_PLAUSIBLE_DAILY_USAGE_GAL, "GAL", config_unit)
        return converted if converted is not None else self._MAX_PLAUSIBLE_DAILY_USAGE_GAL

    def _build_daily_statistics(  # pylint: disable=too-many-locals
        self, daily_entries, existing_hours_by_day, local_tz, starting_sum=0.0
    ):
        """Build StatisticData rows for daily entries, overwriting any existing
        hourly rows for days that already have real hourly history.

        HA's day/month aggregation reads the *last* existing hourly row of
        each calendar day as that day's ending total - it is not recomputed
        live from a single imported row. Any day that already has real
        hourly history needs every one of those existing hours overwritten,
        or the day view keeps reading the old (uncorrected) value from
        whatever hour happens to be last, regardless of what's written to
        the day's first/midnight hour.

        Every day's total lands on a FIXED canonical hour (23:00 local),
        not "whichever hour happens to already exist" - the latter made
        repeat calls non-idempotent: if a call landed a day's total on a
        newly-created hour, a second call moments later would see that hour
        as "existing" and re-derive/overwrite it from a fresh (and
        possibly different) baseline, producing a phantom spike-then-revert
        pair when the two calls' views of the day disagreed. Landing on a
        fixed hour every time means re-running this (with the same or
        overlapping inputs) converges on the same absolute sums instead of
        drifting.

        A day whose value exceeds _MAX_PLAUSIBLE_DAILY_USAGE_GAL is skipped
        entirely (no rows written, running_sum left untouched) rather than
        imported - see that constant's docstring for why. This is a
        last-line-of-defense guard against one specific failure mode, not a
        substitute for reconciling the two endpoints properly.

        Shared by the one-time cutover backfill and the recurring scheduled
        refresh (see async_backfill_daily_history and
        async_refresh_recent_daily_statistics).
        """
        statistics = []
        running_sum = starting_sum
        ceiling = self._max_plausible_daily_value()
        for start, usage, usage_unit in daily_entries:
            value = self._convert_usage_value(usage, usage_unit)
            if value is None:
                continue
            day_date = start.astimezone(local_tz).date()
            if value > ceiling:
                _LOGGER.error(
                    "Daily usage for %s (%s %s) exceeds the plausibility ceiling (%s %s) - "
                    "treating as implausible and skipping rather than importing it. "
                    "Check the source portal directly for this day's real figure.",
                    day_date,
                    value,
                    self.config_entry.data.get("unit_type"),
                    ceiling,
                    self.config_entry.data.get("unit_type"),
                )
                continue
            day_start_sum = running_sum
            running_sum += value
            canonical_hour = dt_util.as_utc(datetime.combine(day_date, self._DAILY_CANONICAL_HOUR, tzinfo=local_tz))
            for hour in existing_hours_by_day.get(day_date, []):
                if hour == canonical_hour:
                    continue
                statistics.append(StatisticData(start=hour, state=0, sum=day_start_sum, last_reset=hour))
            statistics.append(
                StatisticData(start=canonical_hour, state=value, sum=running_sum, last_reset=canonical_hour)
            )
        return statistics, running_sum

    # ------------------------------------------------------------------
    # Recurring daily-statistics refresh
    #
    # SensusAnalyticsDailyUsageSensor deliberately has no state_class (see
    # sensor.py) to avoid the same dual-writer recorder collision that was
    # fixed for LastHourUsageSensor - which means HA's native recorder no
    # longer auto-compiles long-term statistics for it. Without this
    # recurring refresh, the sensor's statistics would only ever be as
    # fresh as the last manual backfill_daily_history service call.
    # ------------------------------------------------------------------

    async def async_refresh_recent_daily_statistics(self, days: int = 3) -> int:
        """Keep the Daily Usage sensor's long-term statistics fresh automatically.

        Re-imports the last ``days`` days using the same real-daily-figures
        and existing-hour-overwrite logic as the one-time historical
        backfill, correcting for late-settling data along the way. Returns
        the number of statistics rows imported.
        """
        if self._legacy_statistics_blocked("Scheduled daily refresh"):
            return 0
        local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
        now_local = datetime.now(local_tz)
        window_start_local = (now_local - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)

        raw_entries = await self.hass.async_add_executor_job(
            self._fetch_daily_entries_for_refresh, window_start_local, now_local
        )
        daily_entries = [
            (dt_util.as_utc(dt_util.utc_from_timestamp(ts_ms / 1000).astimezone(local_tz)), usage, usage_unit)
            for ts_ms, usage, usage_unit in raw_entries
        ]
        if not daily_entries:
            return 0

        statistic_id = self._resolve_daily_usage_statistic_id()
        config_unit = self.config_entry.data.get("unit_type")
        unit = config_unit if config_unit in ("gal", "CCF") else None

        existing_hours_by_day = await self._get_existing_hours_by_day(statistic_id, window_start_local, local_tz)
        baseline_anchor = dt_util.as_utc(window_start_local)
        baseline_sum = await self._get_baseline_sum(statistic_id, baseline_anchor)
        statistics, _ = self._build_daily_statistics(daily_entries, existing_hours_by_day, local_tz, baseline_sum)

        if not statistics:
            return 0

        if not await self._verify_baseline_unchanged(
            statistic_id, baseline_anchor, baseline_sum, log_label="Scheduled daily refresh"
        ):
            return 0

        return self._import_statistics(statistic_id, unit, statistics, "Scheduled daily refresh")

    def _fetch_daily_entries_for_refresh(self, start_local: datetime, end_local: datetime):
        """Fetch daily entries for the scheduled refresh window (runs in executor)."""
        session = self._create_authenticated_session()
        return self._fetch_daily_entries_in_range(session, start_local, end_local)

    # ------------------------------------------------------------------
    # One-time historical backfill for Daily Usage
    #
    # Sensus's `zoom=year` view returns pre-aggregated monthly totals
    # covering roughly the last 12 months per page; paginating further
    # back is done by re-requesting with `end` set to just before the
    # previous page's `start`, following `hasPrev` until it runs out.
    # `zoom=month` returns real daily-granularity data; the window
    # requested is widened as needed to reach the cutover month (at
    # least 60 days, more if the cutover is older), though Sensus's own
    # daily-granularity retention may itself be capped below that - see
    # the gap-detection warning in _fetch_daily_history_window. Neither
    # endpoint goes back further than Sensus itself retains.
    #
    # This backfills sensor.*_daily_usage's own long-term statistics:
    # full calendar months before `cutover_date`, then real daily
    # figures for the partial month(s) up to and including that date -
    # which also corrects any days HA already recorded but undercounted
    # due to late settlement (the same issue fixed for the hourly
    # sensor). Everything is written as one continuously-increasing
    # cumulative sum so there's no seam between backfilled history and
    # the live entity's ongoing data.
    # ------------------------------------------------------------------

    async def async_backfill_daily_history(self, cutover_date) -> int:  # pylint: disable=too-many-locals
        """Backfill sensor.*_daily_usage's long-term statistics before ``cutover_date``.

        ``cutover_date`` only controls where the monthly-aggregate backfill
        stops - full calendar months before it come from Sensus's
        pre-aggregated monthly totals. From the start of that month through
        today, real daily figures are used instead, which also corrects any
        already-recorded days that were undercounted. Returns the number of
        statistics rows imported.
        """
        if self._legacy_statistics_blocked("Daily history backfill"):
            return 0
        local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
        boundary_local = datetime.combine(cutover_date, datetime.min.time(), tzinfo=local_tz)
        boundary_month_start = boundary_local.replace(day=1)

        monthly_totals, daily_entries = await self.hass.async_add_executor_job(
            self._fetch_daily_history_window, boundary_month_start, boundary_local
        )

        statistic_id = self._resolve_daily_usage_statistic_id()
        config_unit = self.config_entry.data.get("unit_type")
        unit = config_unit if config_unit in ("gal", "CCF") else None

        # HA's day/month aggregation reads the *last* existing hourly row of each
        # calendar day as that day's ending total - it is not recomputed live from
        # a single imported row. Any day that already has real hourly history (an
        # entity that's been running a few days, say) needs every one of those
        # existing hours overwritten, or the day view keeps reading the old
        # (uncorrected) value from whatever hour happens to be last, regardless of
        # what we write to the day's first/midnight hour.
        existing_hours_by_day = await self._get_existing_hours_by_day(statistic_id, boundary_month_start, local_tz)

        monthly_statistics, monthly_running_sum = self._build_monthly_statistics(monthly_totals)
        daily_starting_sum = monthly_running_sum

        # Sensus's own daily-granularity retention can fall short of
        # boundary_month_start (see _warn_if_daily_data_falls_short), leaving a
        # gap between the monthly-aggregate total and the first real daily
        # entry. If statistics already exist spanning that gap - true for
        # every re-run except the very first cutover backfill - resetting the
        # running sum to the monthly-aggregate total silently discards
        # whatever was already recorded for the gap and creates a downward
        # discontinuity in every day from there forward. Bridge from the
        # existing recorded sum instead, so a re-run can never regress
        # history it can no longer independently re-derive.
        bridge_anchor = None
        if daily_entries and daily_entries[0][0] > boundary_month_start:
            bridge_anchor = daily_entries[0][0]
            existing_sum = await self._get_existing_sum_before(statistic_id, bridge_anchor)
            if existing_sum is not None:
                daily_starting_sum = existing_sum

        daily_statistics, _ = self._build_daily_statistics(
            daily_entries, existing_hours_by_day, local_tz, daily_starting_sum
        )
        statistics = monthly_statistics + daily_statistics

        if not statistics:
            _LOGGER.warning("Daily history backfill: no convertible usage values found")
            return 0

        # Only the bridging branch above reads a live baseline from the
        # recorder to re-verify - the monthly-aggregate starting sum used
        # otherwise has no existing row to compare against (this is always
        # the very first backfill for that date range), so there is nothing
        # for a race to have clobbered yet.
        if bridge_anchor is not None and not await self._verify_baseline_unchanged(
            statistic_id, bridge_anchor, daily_starting_sum, log_label="Daily history backfill"
        ):
            return 0

        return self._import_statistics(statistic_id, unit, statistics, "Daily history backfill", level=logging.WARNING)

    async def _get_existing_hours_by_day(self, statistic_id: str, start_time: datetime, local_tz) -> dict:
        """Return existing statistics hour start-datetimes, grouped by local calendar date."""
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start_time,
            None,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = stats.get(statistic_id) if stats else []
        by_day: dict = {}
        for row in rows:
            # statistics_during_period returns "start" as raw epoch seconds.
            dt_utc = dt_util.utc_from_timestamp(row["start"])
            dt_local = dt_utc.astimezone(local_tz)
            by_day.setdefault(dt_local.date(), []).append(dt_utc)
        for day_hours in by_day.values():
            day_hours.sort()
        return by_day

    def _resolve_daily_usage_statistic_id(self) -> str:
        """Return the entity_id (statistic_id) of the Daily Usage sensor."""
        unique_id = f"{DOMAIN}_{self.config_entry.entry_id}_daily_usage"
        entity_registry = er.async_get(self.hass)
        entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        return entity_id or "sensor.sensus_analytics_daily_usage"

    def _fetch_daily_history_window(self, boundary_month_start: datetime, boundary_local: datetime):
        """Fetch monthly totals before the boundary month, plus real daily data
        from the boundary month through today (runs in executor).

        ``boundary_local`` (the cutover date) only controls where the monthly
        backfill stops - the daily correction window always extends through
        the present, since the goal is to fix every already-recorded day that
        might be undercounted, not just the ones before the cutover.
        """
        session = self._create_authenticated_session()
        local_tz = boundary_local.tzinfo
        monthly_totals = self._fetch_monthly_totals_before(session, boundary_month_start, local_tz)
        daily_entries = self._fetch_daily_totals_from(session, boundary_month_start, local_tz)
        return monthly_totals, daily_entries

    def _fetch_monthly_totals_before(self, session, boundary_month_start: datetime, local_tz) -> list:
        """Walk back through zoom=year pages, collecting entries strictly before boundary_month_start.

        Anchors the walk-back at boundary_month_start, not "now": months
        on/after the boundary are never wanted from this endpoint (the
        daily window covers them instead), so starting from "now" would
        just fetch and fully discard a page whenever the cutover date is
        more than ~1 page (~400 days) old. The per-entry filter below stays
        as a defensive backstop.
        """
        monthly_totals = []
        end_ms = int(boundary_month_start.timestamp() * 1000)
        for _ in range(60):  # safety cap; Sensus currently retains ~24 months
            page = self._fetch_yearly_page(session, end_ms)
            if not page:
                break
            entries, has_prev, page_start_ms = page
            for ts_ms, usage, usage_unit in entries:
                entry_time = dt_util.utc_from_timestamp(ts_ms / 1000).astimezone(local_tz)
                if entry_time >= boundary_month_start:
                    continue
                monthly_totals.append((dt_util.as_utc(entry_time), usage, usage_unit))
            if not has_prev or page_start_ms is None:
                break
            end_ms = page_start_ms - 1
        monthly_totals.sort(key=lambda row: row[0])
        return monthly_totals

    def _fetch_daily_totals_from(self, session, boundary_month_start: datetime, local_tz) -> list:
        """Fetch real daily entries from boundary_month_start through today.

        Requests a range wide enough to actually reach boundary_month_start,
        not a fixed 60 days - a cutover date older than 60 days used to
        leave a silent gap between the monthly totals and this daily
        window. Sensus's daily-granularity retention may itself be capped
        below what we ask for; detect and warn rather than assume the
        request is honored.
        """
        now_local = datetime.now(local_tz)
        daily_start_local = min(boundary_month_start, now_local - timedelta(days=60))
        raw_entries = self._fetch_daily_entries_in_range(session, daily_start_local, now_local)
        self._warn_if_daily_data_falls_short(raw_entries, boundary_month_start, local_tz)

        daily_entries = []
        for ts_ms, usage, usage_unit in raw_entries:
            entry_time = dt_util.utc_from_timestamp(ts_ms / 1000).astimezone(local_tz)
            if entry_time >= boundary_month_start:
                daily_entries.append((dt_util.as_utc(entry_time), usage, usage_unit))
        daily_entries.sort(key=lambda row: row[0])
        return daily_entries

    @staticmethod
    def _warn_if_daily_data_falls_short(raw_entries, boundary_month_start: datetime, local_tz) -> None:
        """Log a warning if Sensus's daily-granularity data doesn't actually reach the boundary."""
        if not raw_entries:
            return
        earliest_dt = dt_util.utc_from_timestamp(min(entry[0] for entry in raw_entries) / 1000).astimezone(local_tz)
        if earliest_dt > boundary_month_start:
            _LOGGER.warning(
                "Daily history backfill: requested daily data back to %s, but Sensus only "
                "returned daily-granularity data starting %s - days in between will have no "
                "statistics from this backfill (likely a server-side Sensus retention limit).",
                boundary_month_start.date(),
                earliest_dt.date(),
            )

    def _fetch_yearly_page(self, session, end_ms: int):
        """Fetch one zoom=year page ending at ``end_ms``.

        Returns (entries, has_prev, page_start_ms) or None on failure.
        """
        usage_url = urljoin(self.base_url, f"water/usage/{self.account_number}/{self.meter_number}")
        params = {
            "start": end_ms - (400 * 24 * 3600 * 1000),
            "end": end_ms,
            "zoom": "year",
            "page": "null",
            "weather": "1",
        }
        try:
            response = session.get(usage_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            _LOGGER.error("Yearly data retrieval failed: %s", e)
            return None

        if not data.get("operationSuccess", False):
            return None

        payload = data.get("data", {})
        usage_list = payload.get("usage", [])
        if not usage_list or len(usage_list) < 2:
            return None

        usage_unit = usage_list[0][0]
        entries = [(row[0], row[1], usage_unit) for row in usage_list[1:]]
        return entries, bool(payload.get("hasPrev")), payload.get("start")

    def _fetch_daily_entries_in_range(self, session, start_local: datetime, end_local: datetime):
        """Fetch daily-granularity entries (zoom=month) for an explicit local-time range.

        Sensus's zoom=month endpoint does not reliably honor a narrow
        start/end window - it can return entries well before a requested
        few-day range, typically covering at least the
        containing calendar month(s) regardless of how tight start/end
        are. Every entry is filtered against the requested lower bound
        before being returned, so a caller asking for a short recent
        window (the scheduled refresh's default 3-day lookback, in
        particular) can't silently receive - and reprocess - weeks of
        unrelated older history using a baseline that was only ever
        computed for the narrow window it actually asked for. No upper
        bound is enforced against end_local: Sensus has no reason to
        return anything timestamped after "now" (every caller passes
        the current moment as end_local), so there's nothing real for
        that to guard against.
        """
        usage_url = urljoin(self.base_url, f"water/usage/{self.account_number}/{self.meter_number}")
        params = {
            "start": int(start_local.timestamp() * 1000),
            "end": int(end_local.timestamp() * 1000),
            "zoom": "month",
            "page": "null",
            "weather": "1",
        }
        try:
            response = session.get(usage_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            _LOGGER.error("Daily window retrieval failed: %s", e)
            return []

        if not data.get("operationSuccess", False):
            return []

        usage_list = data.get("data", {}).get("usage", [])
        if not usage_list or len(usage_list) < 2:
            return []

        usage_unit = usage_list[0][0]
        entries = [(row[0], row[1], usage_unit) for row in usage_list[1:]]
        return [
            (ts_ms, usage, unit)
            for ts_ms, usage, unit in entries
            if dt_util.utc_from_timestamp(ts_ms / 1000) >= start_local
        ]
