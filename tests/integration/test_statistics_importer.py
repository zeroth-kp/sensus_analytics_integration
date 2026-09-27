"""Integration tests for the hourly water-statistics importer against a real recorder.

A fake coordinator stands in for the Sensus API: it serves deterministic
hourly usage for every local day from ``DATA_FLOOR`` onward, so tests can
change individual hours, inject failures, and count requests.
"""

import asyncio
from datetime import date, datetime, timedelta, timezone
from functools import partial
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.sensus_analytics import statistics as statistics_module
from custom_components.sensus_analytics.const import DOMAIN
from custom_components.sensus_analytics.coordinator import SensusFetchError, StatisticData
from custom_components.sensus_analytics.statistics import WaterStatisticsImporter, local_day_bounds

from .conftest import config_entry_data

UTC = timezone.utc
CHICAGO = ZoneInfo("America/Chicago")
HOUR = timedelta(hours=1)

# 14:40 local (CDT). With the default 90-minute settle delay, hours before
# 13:00 local are settled.
NOW = datetime(2026, 7, 22, 19, 40, tzinfo=UTC)
SETTLED_END = datetime(2026, 7, 22, 18, 0, tzinfo=UTC)
# The poll window starts at local midnight three days back.
POLL_START = datetime(2026, 7, 19, 5, 0, tzinfo=UTC)
DATA_FLOOR = date(2026, 6, 1)


def default_usage(hour: datetime) -> float:
    return float(hour.hour % 5 + 1)


class FakeCoordinator:
    """Serves hourly entries the way ``fetch_hourly_day`` returns them."""

    def __init__(self, entry):
        self.config_entry = entry
        self.last_update_success = True
        self.data = {"hourly_usage_data": [{"timestamp": 0, "usage": 1}]}
        self.overrides: dict[datetime, float | None] = {}
        self.failing_days: set[date] = set()
        self.extra_entries: list[dict] = []
        self.requested_days: list[date] = []
        self.sessions_opened = 0
        self.data_floor = DATA_FLOOR

    def open_session(self):
        self.sessions_opened += 1
        return object()

    def fetch_hourly_day(self, _session, day: date):
        self.requested_days.append(day)
        if day in self.failing_days:
            raise SensusFetchError(f"simulated failure for {day}")
        if day < self.data_floor:
            return []
        start, end = local_day_bounds(day, CHICAGO)
        now_hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        entries = []
        hour = start
        while hour < min(end, now_hour):
            usage = self.overrides.get(hour, default_usage(hour))
            entries.append({"timestamp": int(hour.timestamp() * 1000), "usage": usage, "usage_unit": "GAL"})
            hour += HOUR
        return entries + self.extra_entries


@pytest.fixture
async def env(freezer, recorder_mock, hass, monkeypatch):
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("America/Chicago")
    monkeypatch.setattr(statistics_module, "FETCH_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(statistics_module, "FETCH_DAY_DELAY_SECONDS", 0)
    entry = MockConfigEntry(domain=DOMAIN, data=config_entry_data(unit_type="gal"))
    entry.add_to_hass(hass)
    fake = FakeCoordinator(entry)
    importer = WaterStatisticsImporter(hass, fake)
    return hass, fake, importer, freezer


async def _rows(hass, importer, start=datetime(2020, 1, 1, tzinfo=UTC)):
    await async_wait_recording_done(hass)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, None, {importer.statistic_id}, "hour", None, {"state", "sum"}
    )
    return [
        (datetime.fromtimestamp(row["start"], UTC), row["state"], row["sum"])
        for row in stats.get(importer.statistic_id, [])
    ]


def _expected_states(start: datetime, end: datetime, overrides=None) -> list[float]:
    overrides = overrides or {}
    states, hour = [], start
    while hour < end:
        states.append(overrides.get(hour, default_usage(hour)))
        hour += HOUR
    return states


# -- core sync behavior ------------------------------------------------------


async def test_sync_writes_every_settled_hour_with_a_continuous_sum(env):
    hass, _fake, importer, _ = env
    result = await importer.async_sync(POLL_START, reason="test")

    assert result.ok, result
    rows = await _rows(hass, importer)
    assert rows[0][0] == POLL_START
    assert rows[-1][0] == SETTLED_END - HOUR
    states = _expected_states(POLL_START, SETTLED_END)
    assert [state for _, state, _ in rows] == states
    assert rows[-1][2] == pytest.approx(sum(states))
    assert result.rows_written == len(states)
    assert result.verify.ok


async def test_sync_is_idempotent(env):
    hass, _fake, importer, _ = env
    await importer.async_sync(POLL_START, reason="first")
    first = await _rows(hass, importer)
    await importer.async_sync(POLL_START, reason="second")
    assert await _rows(hass, importer) == first


async def test_late_correction_shifts_that_hour_and_every_later_sum_only(env):
    hass, fake, importer, _ = env
    await importer.async_sync(POLL_START, reason="first")
    before = await _rows(hass, importer)

    corrected_hour = POLL_START + 30 * HOUR
    fake.overrides[corrected_hour] = default_usage(corrected_hour) + 7
    await importer.async_sync(POLL_START, reason="correction")
    after = await _rows(hass, importer)

    for (hour, state_b, sum_b), (_, state_a, sum_a) in zip(before, after):
        if hour < corrected_hour:
            assert (state_a, sum_a) == (state_b, sum_b)
        else:
            assert sum_a == pytest.approx(sum_b + 7)
    assert (await importer.async_verify(POLL_START)).ok


async def test_a_later_sync_continues_from_the_existing_sum(env):
    hass, _fake, importer, _ = env
    await importer.async_sync(POLL_START, reason="first")
    before = {hour: total for hour, _, total in await _rows(hass, importer)}

    start = POLL_START + 48 * HOUR
    await importer.async_sync(start, reason="partial")
    rows = await _rows(hass, importer)
    assert rows[0][0] == POLL_START
    assert {hour: total for hour, _, total in rows} == pytest.approx(before)


async def test_rows_after_the_last_written_hour_keep_their_values_and_get_rebuilt_sums(env):
    hass, fake, importer, freezer = env
    # A run later in the day writes more hours than a run at NOW would.
    freezer.move_to(NOW + 5 * HOUR)
    await importer.async_sync(POLL_START, reason="later run")
    later_rows = await _rows(hass, importer)
    assert later_rows[-1][0] == SETTLED_END + 4 * HOUR

    freezer.move_to(NOW)
    fake.overrides[POLL_START] = default_usage(POLL_START) + 3
    result = await importer.async_sync(POLL_START, reason="earlier run")

    assert result.ok, result
    rows = await _rows(hass, importer)
    assert [hour for hour, _, _ in rows] == [hour for hour, _, _ in later_rows]
    assert rows[-1][2] == pytest.approx(later_rows[-1][2] + 3)


async def test_partial_today_stops_at_the_last_hour_sensus_has(env):
    hass, fake, importer, _ = env
    last_available = SETTLED_END - 3 * HOUR
    for offset in range(3):
        fake.overrides[last_available + (offset + 1) * HOUR] = None
    result = await importer.async_sync(POLL_START, reason="test")

    assert result.ok, result
    rows = await _rows(hass, importer)
    assert rows[-1][0] == last_available


# -- validation: all or nothing ---------------------------------------------


async def test_an_invalid_hour_anywhere_writes_nothing(env):
    hass, fake, importer, _ = env
    fake.overrides[POLL_START + 40 * HOUR] = -5
    result = await importer.async_sync(POLL_START, reason="test")

    assert not result.ok
    assert "negative usage" in result.error
    assert await _rows(hass, importer) == []


async def test_entries_outside_the_requested_day_are_ignored(env):
    hass, fake, importer, _ = env
    # Sensus sometimes returns entries far outside the requested range.
    fake.extra_entries = [{"timestamp": int(datetime(2026, 6, 25, tzinfo=UTC).timestamp() * 1000), "usage": 999}]
    fake.extra_entries[0]["usage_unit"] = "GAL"
    result = await importer.async_sync(POLL_START, reason="test")

    assert result.ok, result
    rows = await _rows(hass, importer)
    assert rows[0][0] == POLL_START
    assert 999 not in [state for _, state, _ in rows]


async def test_a_day_that_keeps_failing_writes_nothing_and_retries(env):
    hass, fake, importer, _ = env
    fake.failing_days.add(date(2026, 7, 20))
    result = await importer.async_sync(POLL_START, reason="test")

    assert not result.ok
    assert "simulated failure" in result.error
    assert fake.requested_days.count(date(2026, 7, 20)) == statistics_module.FETCH_RETRIES + 1
    assert await _rows(hass, importer) == []


async def test_unsupported_unit_writes_nothing(env):
    hass, fake, importer, _ = env
    hass.config_entries.async_update_entry(fake.config_entry, data=config_entry_data(unit_type="liters"))
    result = await importer.async_sync(POLL_START, reason="test")
    assert not result.ok
    assert await _rows(hass, importer) == []


# -- retention floor ------------------------------------------------------


async def test_probe_finds_the_oldest_day_with_hourly_data(env):
    _hass, fake, importer, _ = env
    result = await importer.async_probe_retention()

    assert result["ok"]
    assert result["oldest_hourly_day"] == DATA_FLOOR.isoformat()
    assert result["requests"] <= 12
    assert fake.sessions_opened == 1


async def test_probe_reports_a_transport_failure(env):
    _hass, fake, importer, _ = env
    fake.failing_days.add(date(2026, 7, 21))
    result = await importer.async_probe_retention()
    assert not result["ok"]
    assert "simulated failure" in result["error"]


async def test_long_sync_is_clamped_to_the_floor_and_written_in_chunks(env):
    hass, _fake, importer, _ = env
    result = await importer.async_sync(datetime(2025, 1, 1, tzinfo=UTC), reason="rebuild")

    assert result.ok, result
    rows = await _rows(hass, importer)
    floor_start = local_day_bounds(DATA_FLOOR, CHICAGO)[0]
    assert rows[0][0] == floor_start
    assert len(rows) > statistics_module.IMPORT_CHUNK_HOURS
    assert result.verify.ok


async def test_long_sync_is_refused_when_the_floor_is_unknown(env):
    hass, fake, importer, _ = env
    fake.failing_days.add(date(2026, 7, 21))  # the probe's first request
    result = await importer.async_sync(datetime(2026, 6, 10, tzinfo=UTC), reason="rebuild")
    assert not result.ok
    assert "floor unknown" in result.error
    assert await _rows(hass, importer) == []


# -- time handling -----------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "first_day", "dst_day", "hours"),
    [
        (datetime(2026, 11, 3, 18, 0, tzinfo=UTC), date(2026, 10, 31), date(2026, 11, 1), 25),  # fall back
        (datetime(2026, 3, 10, 18, 0, tzinfo=UTC), date(2026, 3, 7), date(2026, 3, 8), 23),  # spring forward
    ],
)
async def test_dst_days_get_every_local_hour(env, now, first_day, dst_day, hours):
    hass, fake, importer, freezer = env
    fake.data_floor = date(2026, 1, 1)
    freezer.move_to(now)
    result = await importer.async_sync(local_day_bounds(first_day, CHICAGO)[0], reason="dst")

    assert result.ok, result
    day_start, day_end = local_day_bounds(dst_day, CHICAGO)
    rows = await _rows(hass, importer)
    assert len([hour for hour, _, _ in rows if day_start <= hour < day_end]) == hours


# -- verify and Repairs ------------------------------------------------------


async def test_verify_detects_a_broken_sum_and_a_resync_clears_the_issue(env):
    hass, _fake, importer, _ = env
    await importer.async_sync(POLL_START, reason="first")
    bad_hour = POLL_START + 20 * HOUR
    async_add_external_statistics(
        hass, importer.build_metadata("gal"), [StatisticData(start=bad_hour, state=1.0, sum=123456.0)]
    )
    await async_wait_recording_done(hass)

    result = await importer.async_verify(POLL_START)
    assert not result.ok
    assert result.first_bad_hour == bad_hour
    assert ir.async_get(hass).async_get_issue(DOMAIN, importer.issue_id) is not None

    assert (await importer.async_sync(POLL_START, reason="repair")).ok
    assert ir.async_get(hass).async_get_issue(DOMAIN, importer.issue_id) is None


async def test_statistic_metadata(env):
    hass, _fake, importer, _ = env
    await importer.async_sync(POLL_START, reason="test")
    await async_wait_recording_done(hass)
    metadata = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={importer.statistic_id})
    )
    _, meta = metadata[importer.statistic_id]
    assert meta["source"] == DOMAIN
    assert meta["has_sum"] is True
    assert meta["unit_of_measurement"] == "gal"
    assert meta["unit_class"] == "volume"
    assert importer.statistic_id == f"{DOMAIN}:{importer.entry.entry_id.lower()}_water_shadow"


# -- concurrency and the poll hook ------------------------------------------


async def test_concurrent_syncs_are_serialized(env):
    hass, _fake, importer, _ = env
    results = await asyncio.gather(
        importer.async_sync(POLL_START, reason="a"), importer.async_sync(POLL_START + 24 * HOUR, reason="b")
    )
    assert all(result.ok for result in results)
    assert (await importer.async_verify(POLL_START)).ok
    assert (await _rows(hass, importer))[-1][2] == pytest.approx(sum(_expected_states(POLL_START, SETTLED_END)))


@pytest.mark.statistics_poll
async def test_poll_hook_syncs_the_trailing_window_once_per_change(env):
    hass, fake, importer, _ = env
    importer.async_handle_coordinator_update()
    await importer._poll_task  # pylint: disable=protected-access
    rows = await _rows(hass, importer)
    assert rows[0][0] == POLL_START
    requests_after_first = len(fake.requested_days)

    # Same data, same settled cutoff: nothing to do.
    importer.async_handle_coordinator_update()
    await importer._poll_task  # pylint: disable=protected-access
    assert len(fake.requested_days) == requests_after_first

    # New data: sync again.
    fake.data = {"hourly_usage_data": [{"timestamp": 1, "usage": 2}]}
    importer.async_handle_coordinator_update()
    await importer._poll_task  # pylint: disable=protected-access
    assert len(fake.requested_days) > requests_after_first


@pytest.mark.statistics_poll
async def test_poll_hook_skips_failed_refreshes(env):
    _hass, fake, importer, _ = env
    fake.last_update_success = False
    importer.async_handle_coordinator_update()
    assert importer._poll_task is None  # pylint: disable=protected-access
    assert fake.requested_days == []
