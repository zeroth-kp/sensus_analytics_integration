"""Unit tests for the hourly-response parser shared by both fetch paths."""

import pytest

from custom_components.sensus_analytics.coordinator import SensusFetchError, parse_hourly_rows


def test_parses_rows_with_weather_columns():
    entries = parse_hourly_rows([["CF", "INCHES", "FAHRENHEIT"], [1000, 2, 0.1, 70]])
    assert entries == [
        {
            "timestamp": 1000,
            "usage": 2,
            "rain": 0.1,
            "temp": 70,
            "usage_unit": "CF",
            "rain_unit": "INCHES",
            "temp_unit": "FAHRENHEIT",
        }
    ]


def test_missing_weather_columns_become_none():
    (entry,) = parse_hourly_rows([["CF"], [1000, 2]])
    assert (entry["rain"], entry["temp"], entry["rain_unit"], entry["temp_unit"]) == (None, None, None, None)


@pytest.mark.parametrize("usage_list", [[[], [1000, 2]], ["CF", [1000, 2]], [["CF"], [1000]], [["CF"], "row"]])
def test_malformed_responses_raise(usage_list):
    with pytest.raises(SensusFetchError):
        parse_hourly_rows(usage_list)
