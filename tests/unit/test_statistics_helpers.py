"""Unit tests for the pure helpers behind the hourly water-statistics importer."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.sensus_analytics.const import MAX_PLAUSIBLE_HOURLY_USAGE_GAL
from custom_components.sensus_analytics.statistics import (
    StatisticsValidationError,
    build_sum_rows,
    check_sum_chain,
    has_hourly_data,
    is_supported_unit,
    local_day_bounds,
    local_days_between,
    parse_day_entries,
    process_day,
    settled_end,
)

UTC = timezone.utc
CHICAGO = ZoneInfo("America/Chicago")
HOUR = timedelta(hours=1)


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _entry(hour: datetime, usage, unit="GAL") -> dict:
    return {"timestamp": _ms(hour), "usage": usage, "usage_unit": unit}


def _day_entries(day: date, usage=1.0, unit="GAL") -> list[dict]:
    start, end = local_day_bounds(day, CHICAGO)
    entries = []
    hour = start
    while hour < end:
        entries.append(_entry(hour, usage, unit))
        hour += HOUR
    return entries


# -- time handling -------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "hours"),
    [
        (date(2026, 7, 20), 24),
        (date(2026, 3, 8), 23),  # spring forward
        (date(2026, 11, 1), 25),  # fall back
    ],
)
def test_local_day_bounds_cover_dst_days(day, hours):
    start, end = local_day_bounds(day, CHICAGO)
    assert (end - start) / HOUR == hours
    assert start.astimezone(CHICAGO).hour == 0


def test_settled_end_excludes_hours_inside_the_settle_delay():
    now = datetime(2026, 7, 22, 19, 40, tzinfo=UTC)
    # 19:40 - 90 min = 18:10 -> hours before 18:00 are settled
    assert settled_end(now, 90) == datetime(2026, 7, 22, 18, 0, tzinfo=UTC)


def test_local_days_between_spans_local_midnight():
    start = datetime(2026, 7, 21, 4, 0, tzinfo=UTC)  # 23:00 on 07-20 local
    end = datetime(2026, 7, 21, 7, 0, tzinfo=UTC)  # 02:00 on 07-21 local
    assert local_days_between(start, end, CHICAGO) == [date(2026, 7, 20), date(2026, 7, 21)]


# -- units ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "target", "supported"),
    [
        ("CF", "gal", True),
        ("cf", "CCF", True),
        ("GAL", "gal", True),
        ("gal", "CCF", True),
        ("CCF", "CCF", True),
        ("CCF", "gal", False),
        ("INCHES", "gal", False),
        (None, "gal", False),
        ("", "gal", False),
    ],
)
def test_is_supported_unit(source, target, supported):
    assert is_supported_unit(source, target) is supported


# -- parse_day_entries -------------------------------------------------------

WINDOW_START = datetime(2026, 7, 20, 5, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)


def test_parse_drops_entries_outside_the_window_and_converts_units():
    entries = [
        _entry(WINDOW_START - timedelta(days=20), 3, unit="CF"),  # far outside, dropped
        _entry(WINDOW_START, 2, unit="CF"),
        _entry(WINDOW_START + HOUR, 0, unit="CF"),
        _entry(WINDOW_END, 5, unit="CF"),  # end is exclusive, dropped
    ]
    values = parse_day_entries(entries, WINDOW_START, WINDOW_END, "gal", allow_missing_values=False)
    assert values == {WINDOW_START: 15.0, WINDOW_START + HOUR: 0.0}  # 2 CF -> round(14.96) gal


def test_parse_floors_timestamps_to_the_hour():
    entry = {"timestamp": _ms(WINDOW_START) + 90_000, "usage": 4, "usage_unit": "GAL"}
    assert parse_day_entries([entry], WINDOW_START, WINDOW_END, "gal", allow_missing_values=False) == {
        WINDOW_START: 4.0
    }


def test_parse_skips_missing_values_only_when_allowed():
    entries = [_entry(WINDOW_START, 1), _entry(WINDOW_START + HOUR, None)]
    assert parse_day_entries(entries, WINDOW_START, WINDOW_END, "gal", allow_missing_values=True) == {WINDOW_START: 1.0}
    with pytest.raises(StatisticsValidationError, match="no usage value"):
        parse_day_entries(entries, WINDOW_START, WINDOW_END, "gal", allow_missing_values=False)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (_entry(WINDOW_START, -1), "negative usage"),
        (_entry(WINDOW_START, MAX_PLAUSIBLE_HOURLY_USAGE_GAL + 1), "plausibility ceiling"),
        (_entry(WINDOW_START, 1, unit="INCHES"), "cannot convert usage unit"),
        (_entry(WINDOW_START, "abc"), "non-numeric usage"),
        ({"timestamp": "yesterday", "usage": 1, "usage_unit": "GAL"}, "numeric timestamp"),
        ({"timestamp": True, "usage": 1, "usage_unit": "GAL"}, "numeric timestamp"),
    ],
)
def test_parse_rejects_implausible_entries(entry, message):
    with pytest.raises(StatisticsValidationError, match=message):
        parse_day_entries([entry], WINDOW_START, WINDOW_END, "gal", allow_missing_values=False)


def test_parse_rejects_two_entries_for_the_same_hour():
    entries = [_entry(WINDOW_START, 1), {"timestamp": _ms(WINDOW_START) + 60_000, "usage": 2, "usage_unit": "GAL"}]
    with pytest.raises(StatisticsValidationError, match="more than one entry"):
        parse_day_entries(entries, WINDOW_START, WINDOW_END, "gal", allow_missing_values=False)


def test_ceiling_is_converted_to_the_configured_unit():
    # 5000 gal is about 6.68 CCF, so 7 CCF in one hour is rejected...
    with pytest.raises(StatisticsValidationError, match="plausibility ceiling"):
        parse_day_entries(
            [_entry(WINDOW_START, 7, unit="CCF")], WINDOW_START, WINDOW_END, "CCF", allow_missing_values=False
        )
    # ...while 6 CCF is accepted.
    assert parse_day_entries(
        [_entry(WINDOW_START, 6, unit="CCF")], WINDOW_START, WINDOW_END, "CCF", allow_missing_values=False
    ) == {WINDOW_START: 6.0}


# -- process_day ------------------------------------------------------------

DAY = date(2026, 7, 20)
DAY_START, DAY_END = local_day_bounds(DAY, CHICAGO)


def test_fully_settled_day_zero_fills_missing_hours():
    entries = _day_entries(DAY)
    del entries[5]
    values, last_hour, zero_filled = process_day(DAY, entries, DAY_START, DAY_END + HOUR, "gal", CHICAGO)
    assert len(values) == 23
    assert last_hour == DAY_END - HOUR
    assert zero_filled == 1


def test_fully_settled_day_without_data_fails():
    for empty in (None, [], [_entry(DAY_START, None)]):
        with pytest.raises(StatisticsValidationError, match="no hourly data for settled day"):
            process_day(DAY, empty, DAY_START, DAY_END, "gal", CHICAGO)


def test_partial_day_stops_at_the_last_value_and_counts_gaps():
    cutoff = DAY_START + 10 * HOUR
    entries = [_entry(DAY_START + offset * HOUR, 1) for offset in (0, 1, 3)]  # hour 2 missing
    entries.append(_entry(DAY_START + 4 * HOUR, None))  # not settled at Sensus yet
    values, last_hour, zero_filled = process_day(DAY, entries, DAY_START, cutoff, "gal", CHICAGO)
    assert last_hour == DAY_START + 3 * HOUR
    assert zero_filled == 1
    assert len(values) == 3


def test_partial_day_with_no_values_contributes_nothing():
    assert process_day(DAY, [], DAY_START, DAY_START + 5 * HOUR, "gal", CHICAGO) == ({}, None, 0)


def test_window_start_inside_the_day_is_respected():
    start = DAY_START + 20 * HOUR
    values, last_hour, zero_filled = process_day(DAY, _day_entries(DAY), start, DAY_END, "gal", CHICAGO)
    assert min(values) == start
    assert len(values) == 4
    assert last_hour == DAY_END - HOUR
    assert zero_filled == 0


# -- sums and chain checks ------------------------------------------------


def test_build_sum_rows_continues_from_the_anchor_and_zero_fills():
    hours = [WINDOW_START + offset * HOUR for offset in range(3)]
    rows = build_sum_rows(hours, {hours[0]: 1.5, hours[2]: 2.0}, anchor_sum=100.0)
    assert rows == [(hours[0], 1.5, 101.5), (hours[1], 0.0, 101.5), (hours[2], 2.0, 103.5)]


def _rows(start, states, anchor=0.0):
    rows, total = [], anchor
    for offset, state in enumerate(states):
        total += state
        rows.append({"start": (start + offset * HOUR).timestamp(), "state": state, "sum": total})
    return rows


def test_check_sum_chain_accepts_a_consistent_chain():
    result = check_sum_chain(_rows(WINDOW_START, [1, 0, 2], anchor=10), 10, WINDOW_START)
    assert result.ok and result.rows_checked == 3


def test_check_sum_chain_reports_a_broken_sum():
    rows = _rows(WINDOW_START, [1, 1, 1], anchor=10)
    rows[1]["sum"] += 5
    result = check_sum_chain(rows, 10, WINDOW_START)
    assert not result.ok
    assert result.first_bad_hour == WINDOW_START + HOUR


def test_check_sum_chain_reports_a_missing_hour():
    rows = _rows(WINDOW_START, [1, 1, 1], anchor=0)
    del rows[1]
    result = check_sum_chain(rows, 0, WINDOW_START)
    assert not result.ok
    assert result.first_bad_hour == WINDOW_START + HOUR
    assert "expected a row" in result.problem


def test_check_sum_chain_reports_a_negative_state():
    rows = _rows(WINDOW_START, [1, -1], anchor=0)
    assert not check_sum_chain(rows, 0, WINDOW_START).ok


def test_check_sum_chain_without_an_anchor_starts_at_the_first_row():
    later = WINDOW_START + 5 * HOUR
    assert check_sum_chain(_rows(later, [1, 2]), None, WINDOW_START).ok


def test_has_hourly_data():
    assert not has_hourly_data(None)
    assert not has_hourly_data([])
    assert not has_hourly_data([_entry(WINDOW_START, None)])
    assert has_hourly_data([_entry(WINDOW_START, 0)])
