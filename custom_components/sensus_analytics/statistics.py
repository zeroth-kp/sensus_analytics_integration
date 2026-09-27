"""Single-writer importer for hourly water-usage long-term statistics.

Every write to the importer's statistic goes through ``async_sync``, which:

1. fetches hourly usage one local day at a time, from ``start`` through the
   latest *settled* hour (an hour counts as settled once it ended at least
   the configured settle delay ago);
2. validates every day before anything is written - entries outside the
   requested day are dropped, and a negative, non-finite, implausibly large,
   duplicated or unconvertible value fails the whole run;
3. rebuilds the running sum forward from the last row before ``start``,
   never adjusting existing sums incrementally, so a run is idempotent and a
   late correction simply replaces the old value on the next run;
4. rebuilds the sums (not the values) of any rows already stored after the
   last written hour, so the chain stays continuous;
5. waits for the recorder to commit, then reads the range back and checks
   that every hour is present and every sum equals the previous sum plus
   that hour's value. A failure raises a Repairs issue and widens the next
   poll-triggered run to re-cover the broken range.

Nothing is ever written before the retention floor: the oldest local day
Sensus still has hourly data for, found by ``async_probe_retention``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time as time_module
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import async_add_external_statistics, statistics_during_period
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import (
    CONF_HOUR_SETTLE_DELAY_MINUTES,
    DEFAULT_HOUR_SETTLE_DELAY_MINUTES,
    DOMAIN,
    FETCH_DAY_DELAY_SECONDS,
    FETCH_RETRIES,
    FETCH_RETRY_DELAY_SECONDS,
    IMPORT_CHUNK_HOURS,
    MAX_PLAUSIBLE_HOURLY_USAGE_GAL,
    PROBE_MAX_DAYS,
    SYNC_TRAILING_DAYS,
)
from .coordinator import SensusFetchError, StatisticData, StatisticMetaData, apply_sum_statistic_fields
from .usage_conversion import convert_usage_value

try:
    from homeassistant.components.recorder.tasks import SynchronizeTask
except ImportError:  # pragma: no cover - older HA cores
    SynchronizeTask = None

_LOGGER = logging.getLogger(__name__)

ONE_HOUR = timedelta(hours=1)
SUPPORTED_UNITS = ("gal", "CCF")
# (Sensus unit, configured unit) pairs convert_usage_value knows how to convert.
_KNOWN_CONVERSIONS = {("CF", "gal"), ("CF", "CCF"), ("GAL", "gal"), ("GAL", "CCF")}
# Values are rounded to this many decimals; sums are compared with this tolerance.
VALUE_PRECISION = 6
SUM_TOLERANCE = 1e-4
SHADOW_STATISTIC_NAME = "Sensus Analytics water usage (shadow)"


class StatisticsValidationError(Exception):
    """Fetched hourly data failed validation, so nothing was written."""


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of reading a statistic back and checking its sum chain."""

    ok: bool
    rows_checked: int
    first_bad_hour: datetime | None = None
    problem: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a service-response friendly representation."""
        return {
            "ok": self.ok,
            "rows_checked": self.rows_checked,
            "first_bad_hour": self.first_bad_hour.isoformat() if self.first_bad_hour else None,
            "problem": self.problem,
        }


@dataclass(frozen=True)
class SyncResult:  # pylint: disable=too-many-instance-attributes
    """Outcome of one ``async_sync`` run."""

    ok: bool
    reason: str
    rows_written: int = 0
    first_hour: datetime | None = None
    last_hour: datetime | None = None
    zero_filled_hours: int = 0
    error: str | None = None
    verify: VerifyResult | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a service-response friendly representation."""
        return {
            "ok": self.ok,
            "reason": self.reason,
            "rows_written": self.rows_written,
            "first_hour": self.first_hour.isoformat() if self.first_hour else None,
            "last_hour": self.last_hour.isoformat() if self.last_hour else None,
            "zero_filled_hours": self.zero_filled_hours,
            "error": self.error,
            "verify": self.verify.as_dict() if self.verify else None,
        }


# ----------------------------------------------------------------------
# Pure helpers (no Home Assistant state) - unit tested directly
# ----------------------------------------------------------------------


def floor_to_hour(moment: datetime) -> datetime:
    """Return ``moment`` truncated to the start of its hour."""
    return moment.replace(minute=0, second=0, microsecond=0)


def local_day_bounds(day: date, local_tz: tzinfo) -> tuple[datetime, datetime]:
    """Return the UTC start (inclusive) and end (exclusive) of a local calendar day.

    Handles 23- and 25-hour days at DST transitions, since both bounds are
    local midnights converted independently.
    """
    start = dt_util.as_utc(datetime.combine(day, time.min, tzinfo=local_tz))
    end = dt_util.as_utc(datetime.combine(day + timedelta(days=1), time.min, tzinfo=local_tz))
    return start, end


def local_days_between(start: datetime, end: datetime, local_tz: tzinfo) -> list[date]:
    """Return every local calendar day touched by the hours in ``[start, end)``."""
    first_day = start.astimezone(local_tz).date()
    last_day = (end - ONE_HOUR).astimezone(local_tz).date()
    return [first_day + timedelta(days=offset) for offset in range((last_day - first_day).days + 1)]


def settled_end(now: datetime, settle_delay_minutes: int) -> datetime:
    """Return the start of the first hour that is not yet settled.

    An hour is settled once it ended at least ``settle_delay_minutes`` ago,
    so every hour strictly before the returned value is settled. Matches the
    Last Hour Usage sensor's notion of the most recent completed hour.
    """
    return floor_to_hour(now - timedelta(minutes=settle_delay_minutes))


def is_supported_unit(source_unit: Any, target_unit: str) -> bool:
    """Return whether a Sensus unit can be converted to the configured unit."""
    if not isinstance(source_unit, str) or not source_unit:
        return False
    source = source_unit.upper()
    return (source, target_unit) in _KNOWN_CONVERSIONS or source == target_unit.upper()


def max_plausible_hourly_value(target_unit: str) -> float:
    """Return the hourly usage ceiling in the configured unit."""
    converted = convert_usage_value(MAX_PLAUSIBLE_HOURLY_USAGE_GAL, "GAL", target_unit)
    return float(converted) if converted is not None else float(MAX_PLAUSIBLE_HOURLY_USAGE_GAL)


def entry_hour(entry: dict[str, Any]) -> datetime:
    """Return the UTC hour an hourly entry belongs to."""
    timestamp = entry.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise StatisticsValidationError(f"entry without a numeric timestamp: {timestamp!r}")
    return floor_to_hour(dt_util.utc_from_timestamp(timestamp / 1000))


def converted_hour_value(entry: dict[str, Any], hour: datetime, target_unit: str, ceiling: float) -> float:
    """Return an entry's usage in the configured unit, rejecting anything implausible."""
    usage = entry.get("usage")
    usage_unit = entry.get("usage_unit")
    if not is_supported_unit(usage_unit, target_unit):
        raise StatisticsValidationError(f"cannot convert usage unit {usage_unit!r} to {target_unit!r}")
    value = convert_usage_value(usage, usage_unit.upper(), target_unit)
    if value is None or not math.isfinite(value):
        raise StatisticsValidationError(f"non-numeric usage {usage!r} for {hour.isoformat()}")
    if value < 0:
        raise StatisticsValidationError(f"negative usage {value} for {hour.isoformat()}")
    if value > ceiling:
        raise StatisticsValidationError(
            f"usage {value} {target_unit} for {hour.isoformat()} exceeds the plausibility ceiling ({ceiling})"
        )
    return round(float(value), VALUE_PRECISION)


def parse_day_entries(
    entries: Iterable[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
    target_unit: str,
    *,
    allow_missing_values: bool,
) -> dict[datetime, float]:
    """Validate one day's hourly entries and return ``{utc_hour_start: value}``.

    Entries whose hour falls outside ``[window_start, window_end)`` are
    dropped: Sensus does not always honor the requested range. Any other
    problem raises ``StatisticsValidationError`` so the caller writes nothing.

    ``allow_missing_values`` lets entries without a usage value be skipped,
    which is expected only for hours that have not settled yet.
    """
    ceiling = max_plausible_hourly_value(target_unit)
    values: dict[datetime, float] = {}
    for entry in entries:
        hour = entry_hour(entry)
        if not window_start <= hour < window_end:
            continue
        if entry.get("usage") is None:
            if allow_missing_values:
                continue
            raise StatisticsValidationError(f"no usage value for settled hour {hour.isoformat()}")
        if hour in values:
            raise StatisticsValidationError(f"more than one entry for {hour.isoformat()}")
        values[hour] = converted_hour_value(entry, hour, target_unit, ceiling)
    return values


def process_day(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    day: date,
    entries: list[dict[str, Any]] | None,
    start: datetime,
    cutoff: datetime,
    unit: str,
    local_tz: tzinfo,
) -> tuple[dict[datetime, float], datetime | None, int]:
    """Validate one fetched local day within ``[start, cutoff)``.

    Returns the day's values, the last hour of the day to write (None when
    the day contributes nothing yet), and how many hours will be written as 0.

    A fully settled day must have data, and every hour of it is written
    (missing ones as 0). A partially settled day (today) stops at the last
    hour Sensus has a value for; later hours are written once they settle.
    """
    day_start, day_end = local_day_bounds(day, local_tz)
    window_start = max(day_start, start)
    fully_settled = day_end <= cutoff
    window_end = day_end if fully_settled else cutoff
    if fully_settled and not has_hourly_data(entries):
        raise StatisticsValidationError(f"Sensus returned no hourly data for settled day {day.isoformat()}")
    values = parse_day_entries(entries or [], window_start, window_end, unit, allow_missing_values=not fully_settled)
    if fully_settled:
        expected = int((window_end - window_start) / ONE_HOUR)
        return values, window_end - ONE_HOUR, expected - len(values)
    if not values:
        return values, None, 0
    partial_last = max(values)
    gaps = int((partial_last - window_start) / ONE_HOUR) + 1 - len(values)
    return values, partial_last, gaps


def build_sum_rows(
    hours: list[datetime], values: dict[datetime, float], anchor_sum: float
) -> list[tuple[datetime, float, float]]:
    """Return ``(hour, state, sum)`` rows, with sums rebuilt forward from ``anchor_sum``.

    Hours missing from ``values`` are written as 0.
    """
    rows = []
    running = anchor_sum
    for hour in hours:
        state = values.get(hour, 0.0)
        running += state
        rows.append((hour, state, round(running, VALUE_PRECISION)))
    return rows


def row_start(row: dict[str, Any]) -> datetime:
    """Return a statistics row's start as an aware UTC datetime."""
    start = row["start"]
    if isinstance(start, datetime):
        return dt_util.as_utc(start)
    return dt_util.utc_from_timestamp(start)


def check_sum_chain(
    rows: list[dict[str, Any]], anchor_sum: float | None, expected_first_hour: datetime | None
) -> VerifyResult:
    """Check that rows are hourly-contiguous and each sum is the previous sum plus the state.

    ``anchor_sum`` is the sum of the row immediately before the range (None
    when the statistic has no earlier rows, in which case the chain starts
    at 0 from the first row found). ``expected_first_hour`` is the hour the
    range must start at when an earlier row exists.
    """
    previous_sum = anchor_sum if anchor_sum is not None else 0.0
    expected = expected_first_hour if anchor_sum is not None else None
    for count, row in enumerate(rows):
        hour = row_start(row)
        state = row.get("state")
        total = row.get("sum")
        if expected is not None and hour != expected:
            return VerifyResult(False, count, expected, f"expected a row for {expected.isoformat()}")
        if state is None or total is None:
            return VerifyResult(False, count, hour, "row is missing its state or sum")
        if state < 0:
            return VerifyResult(False, count, hour, f"negative state {state}")
        if not math.isclose(total, previous_sum + state, abs_tol=SUM_TOLERANCE):
            return VerifyResult(
                False, count, hour, f"sum {total} is not previous sum {previous_sum} plus state {state}"
            )
        previous_sum = total
        expected = hour + ONE_HOUR
    return VerifyResult(True, len(rows))


def has_hourly_data(entries: list[dict[str, Any]] | None) -> bool:
    """Return whether a day's fetch returned at least one usage value."""
    return bool(entries) and any(entry.get("usage") is not None for entry in entries)


# ----------------------------------------------------------------------
# Importer
# ----------------------------------------------------------------------


class WaterStatisticsImporter:  # pylint: disable=too-many-instance-attributes
    """The only writer of one config entry's hourly water statistic."""

    def __init__(self, hass: HomeAssistant, coordinator) -> None:
        """Initialize the importer for a coordinator's config entry."""
        self.hass = hass
        self.coordinator = coordinator
        self.entry = coordinator.config_entry
        self.statistic_id = f"{DOMAIN}:{self.entry.entry_id.lower()}_water_shadow"
        self._lock = asyncio.Lock()
        self._poll_task: asyncio.Task | None = None
        self._last_clean_fingerprint: tuple | None = None
        self._resync_from: datetime | None = None
        self._floor_day: date | None = None
        self._floor_checked_on: date | None = None

    # -- configuration -------------------------------------------------

    @property
    def local_tz(self) -> tzinfo:
        """Return Home Assistant's configured time zone."""
        return dt_util.get_time_zone(self.hass.config.time_zone) or dt_util.DEFAULT_TIME_ZONE

    @property
    def unit(self) -> str | None:
        """Return the configured statistics unit, or None if unsupported."""
        unit = self.entry.data.get("unit_type")
        return unit if unit in SUPPORTED_UNITS else None

    @property
    def settle_delay_minutes(self) -> int:
        """Return the configured settle delay."""
        return int(self.entry.data.get(CONF_HOUR_SETTLE_DELAY_MINUTES, DEFAULT_HOUR_SETTLE_DELAY_MINUTES))

    @property
    def issue_id(self) -> str:
        """Return this entry's Repairs issue id."""
        return f"statistics_discontinuity_{self.entry.entry_id}"

    def local_midnight(self, day: date) -> datetime:
        """Return the UTC start of a local calendar day."""
        return local_day_bounds(day, self.local_tz)[0]

    def poll_window_start(self) -> datetime:
        """Return the start of the window every poll-triggered sync rewrites."""
        today = dt_util.now(self.local_tz).date()
        return self.local_midnight(today - timedelta(days=SYNC_TRAILING_DAYS))

    def build_metadata(self, unit: str) -> StatisticMetaData:
        """Return the statistic's metadata."""
        metadata = StatisticMetaData(
            has_sum=True,
            name=SHADOW_STATISTIC_NAME,
            source=DOMAIN,
            statistic_id=self.statistic_id,
            unit_of_measurement=unit,
        )
        return apply_sum_statistic_fields(metadata)

    # -- poll hook -----------------------------------------------------

    @callback
    def async_handle_coordinator_update(self) -> None:
        """Schedule a trailing-window sync after a successful coordinator refresh.

        Skipped when neither the fetched hourly data nor the settled cutoff
        changed since the last clean run, and while a previous run is active.
        """
        if not self.coordinator.last_update_success:
            return
        if self._poll_task is not None and not self._poll_task.done():
            return
        fingerprint = self._poll_fingerprint()
        if fingerprint == self._last_clean_fingerprint and self._resync_from is None:
            return
        self._poll_task = self.entry.async_create_background_task(
            self.hass, self._async_poll_sync(fingerprint), f"{DOMAIN} statistics sync {self.entry.entry_id}"
        )

    def _poll_fingerprint(self) -> tuple:
        data = self.coordinator.data or {}
        hourly = data.get("hourly_usage_data") or []
        cutoff = settled_end(dt_util.utcnow(), self.settle_delay_minutes)
        return (cutoff, tuple((entry.get("timestamp"), entry.get("usage")) for entry in hourly))

    async def _async_poll_sync(self, fingerprint: tuple) -> None:
        start = self.poll_window_start()
        if self._resync_from is not None:
            start = min(start, self._resync_from)
        try:
            result = await self.async_sync(start, reason="poll")
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Statistics sync for %s failed unexpectedly", self.statistic_id)
            return
        if result.ok:
            self._last_clean_fingerprint = fingerprint

    # -- retention floor -----------------------------------------------

    async def async_get_floor(self) -> date | None:
        """Return the retention floor, probing at most once per local day."""
        today = dt_util.now(self.local_tz).date()
        if self._floor_checked_on != today:
            await self.async_probe_retention()
        return self._floor_day

    async def async_probe_retention(self) -> dict[str, Any]:
        """Find the oldest local day Sensus still has hourly data for.

        Binary search between today minus ``PROBE_MAX_DAYS`` and yesterday,
        assuming data exists for every day from the floor onward. A
        transport error fails the probe; a day Sensus answers with no rows
        (or reports as unsuccessful) counts as having no data.
        """
        today = dt_util.now(self.local_tz).date()
        try:
            floor, requests_made = await self.hass.async_add_executor_job(self._probe_blocking, today)
        except SensusFetchError as error:
            _LOGGER.warning("Sensus retention probe failed: %s", error)
            return {"ok": False, "oldest_hourly_day": None, "requests": None, "error": str(error)}
        self._floor_checked_on = today
        if floor is None:
            _LOGGER.warning("Sensus retention probe found no hourly data for yesterday")
            return {"ok": False, "oldest_hourly_day": None, "requests": requests_made, "error": "no recent data"}
        if self._floor_day is None or floor >= self._floor_day:
            # The floor only ever moves forward: Sensus deletes old data, it never restores it.
            self._floor_day = floor
        return {"ok": True, "oldest_hourly_day": self._floor_day.isoformat(), "requests": requests_made, "error": None}

    def _probe_blocking(self, today: date) -> tuple[date | None, int]:
        session = self.coordinator.open_session()
        requests_made = 0

        def day_has_data(day: date) -> bool:
            nonlocal requests_made
            requests_made += 1
            return has_hourly_data(self.coordinator.fetch_hourly_day(session, day))

        newest = today - timedelta(days=1)
        if not day_has_data(newest):
            return None, requests_made
        oldest = today - timedelta(days=PROBE_MAX_DAYS)
        if day_has_data(oldest):
            return oldest, requests_made
        # Invariant: ``low`` has no data, ``high`` has data.
        low, high = oldest, newest
        while (high - low).days > 1:
            middle = low + timedelta(days=(high - low).days // 2)
            if day_has_data(middle):
                high = middle
            else:
                low = middle
        return high, requests_made

    # -- sync ------------------------------------------------------------

    async def async_sync(self, start: datetime, *, reason: str) -> SyncResult:
        """Rewrite every settled hour from ``start`` to now; see the module docstring."""
        async with self._lock:
            return await self._async_sync_locked(start, reason)

    async def _async_sync_locked(self, start: datetime, reason: str) -> SyncResult:  # pylint: disable=too-many-locals
        unit = self.unit
        if unit is None:
            return SyncResult(False, reason, error=f"unsupported unit {self.entry.data.get('unit_type')!r}")

        start = floor_to_hour(dt_util.as_utc(start))
        if start < self.poll_window_start():
            floor = await self.async_get_floor()
            if floor is None:
                return SyncResult(False, reason, error="retention floor unknown; refusing a long sync")
            start = max(start, self.local_midnight(floor))

        cutoff = settled_end(dt_util.utcnow(), self.settle_delay_minutes)
        if start >= cutoff:
            return SyncResult(True, reason, first_hour=start)

        try:
            values, last_hour, zero_filled = await self._async_fetch_range(start, cutoff, unit)
        except (SensusFetchError, StatisticsValidationError) as error:
            _LOGGER.warning("Statistics sync (%s) for %s wrote nothing: %s", reason, self.statistic_id, error)
            return SyncResult(False, reason, first_hour=start, error=str(error))
        if last_hour is None or last_hour < start:
            return SyncResult(True, reason, first_hour=start)

        hours = []
        hour = start
        while hour <= last_hour:
            hours.append(hour)
            hour += ONE_HOUR

        anchor = await self._async_sum_before(start)
        rows = build_sum_rows(hours, values, anchor or 0.0)
        rows.extend(await self._async_rebuild_later_rows(last_hour, rows[-1][2]))
        self._write_rows(unit, rows)
        await self._async_wait_for_commit()

        if zero_filled:
            _LOGGER.warning(
                "Statistics sync (%s) for %s: Sensus returned no value for %s settled hour(s); written as 0",
                reason,
                self.statistic_id,
                zero_filled,
            )
        verify = await self._async_verify_locked(start)
        _LOGGER.debug("Statistics sync (%s) for %s wrote %s row(s)", reason, self.statistic_id, len(rows))
        return SyncResult(verify.ok, reason, len(rows), start, last_hour, zero_filled, verify=verify)

    async def _async_fetch_range(
        self, start: datetime, cutoff: datetime, unit: str
    ) -> tuple[dict[datetime, float], datetime | None, int]:
        """Fetch and validate ``[start, cutoff)``; return values, the last hour to write, and zero-fill count."""
        local_tz = self.local_tz
        days = local_days_between(start, cutoff, local_tz)
        fetched = await self.hass.async_add_executor_job(self._fetch_days_blocking, days)

        values: dict[datetime, float] = {}
        last_hour: datetime | None = None
        zero_filled = 0
        for day in days:
            day_values, day_last_hour, day_zero_filled = process_day(day, fetched[day], start, cutoff, unit, local_tz)
            values.update(day_values)
            zero_filled += day_zero_filled
            if day_last_hour is not None:
                last_hour = day_last_hour
        return values, last_hour, zero_filled

    def _fetch_days_blocking(self, days: list[date]) -> dict[date, list[dict[str, Any]] | None]:
        session = self.coordinator.open_session()
        fetched: dict[date, list[dict[str, Any]] | None] = {}
        for index, day in enumerate(days):
            if index and len(days) > SYNC_TRAILING_DAYS + 1:
                time_module.sleep(FETCH_DAY_DELAY_SECONDS)
            attempt = 0
            while True:
                try:
                    fetched[day] = self.coordinator.fetch_hourly_day(session, day)
                    break
                except SensusFetchError:
                    if attempt >= FETCH_RETRIES:
                        raise
                    time_module.sleep(FETCH_RETRY_DELAY_SECONDS * 2**attempt)
                    attempt += 1
                    session = self.coordinator.open_session()
        return fetched

    def _write_rows(self, unit: str, rows: list[tuple[datetime, float, float]]) -> None:
        metadata = self.build_metadata(unit)
        statistics = [StatisticData(start=hour, state=state, sum=total) for hour, state, total in rows]
        for index in range(0, len(statistics), IMPORT_CHUNK_HOURS):
            async_add_external_statistics(self.hass, metadata, statistics[index : index + IMPORT_CHUNK_HOURS])

    async def _async_wait_for_commit(self) -> None:
        """Wait until every statistics write queued so far has been committed.

        The recorder's own ``async_block_till_done`` returns immediately when
        its queue is empty, even if a queued import is still executing, so
        queue a synchronize task behind the writes and wait for that instead.
        """
        instance = get_instance(self.hass)
        if SynchronizeTask is None:  # pragma: no cover - older HA cores
            await instance.async_block_till_done()
            return
        future = self.hass.loop.create_future()
        instance.queue_task(SynchronizeTask(future))
        await future

    async def _async_read(self, start: datetime, end: datetime | None, types: set[str]) -> list[dict[str, Any]]:
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, start, end, {self.statistic_id}, "hour", None, types
        )
        return list(stats.get(self.statistic_id, [])) if stats else []

    async def _async_sum_before(self, start: datetime) -> float | None:
        """Return the sum of the last row before ``start``, or None if there is none."""
        for period_start in (start - timedelta(days=7), dt_util.utc_from_timestamp(0)):
            rows = await self._async_read(period_start, start, {"sum"})
            if rows:
                return float(rows[-1].get("sum") or 0.0)
        return None

    async def _async_rebuild_later_rows(
        self, last_hour: datetime, running_sum: float
    ) -> list[tuple[datetime, float, float]]:
        """Return rows stored after ``last_hour`` with their sums rebuilt from ``running_sum``."""
        later = await self._async_read(last_hour + ONE_HOUR, None, {"state"})
        rows = []
        for row in later:
            state = float(row.get("state") or 0.0)
            running_sum += state
            rows.append((row_start(row), state, round(running_sum, VALUE_PRECISION)))
        return rows

    # -- verify ----------------------------------------------------------

    async def async_verify(self, start: datetime) -> VerifyResult:
        """Check the stored chain from ``start`` onward and raise or clear the Repairs issue."""
        async with self._lock:
            return await self._async_verify_locked(start)

    async def _async_verify_locked(self, start: datetime) -> VerifyResult:
        start = floor_to_hour(dt_util.as_utc(start))
        anchor = await self._async_sum_before(start)
        rows = await self._async_read(start, None, {"state", "sum"})
        result = check_sum_chain(rows, anchor, start)
        if result.ok:
            self._resync_from = None
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            return result

        bad_hour = result.first_bad_hour or start
        _LOGGER.error(
            "Statistics for %s failed verification at %s: %s", self.statistic_id, bad_hour.isoformat(), result.problem
        )
        bad_hour = floor_to_hour(bad_hour)
        self._resync_from = bad_hour if self._resync_from is None else min(self._resync_from, bad_hour)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self.issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="statistics_discontinuity",
            translation_placeholders={
                "statistic_id": self.statistic_id,
                "hour": dt_util.as_local(bad_hour).isoformat(),
                "problem": result.problem or "",
            },
        )
        return result
