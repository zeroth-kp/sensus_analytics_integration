"""Shared fixtures/helpers for integration tests that need a working config entry."""

import logging
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

CONFIG_ENTRY_DATA_TEMPLATE = {
    "base_url": "https://example.invalid/",
    "username": "user",
    "password": "pass",
    "account_number": "acct",
    "meter_number": "meter",
    "unit_type": "CCF",
    "tier1_price": 0.01,
    "service_fee": 15.0,
}

WIDGET_RESPONSE = {
    "widgetList": [
        {
            "data": {
                "devices": [
                    {
                        "dailyUsage": 5,
                        "usageUnit": "CCF",
                        "meterAddress1": "123 Main",
                        "lastRead": 0,
                        "meterLong": 0.0,
                        "meterId": "m1",
                        "meterLat": 0.0,
                        "latestReadUsage": 100,
                        "billingUsage": 50,
                    }
                ]
            }
        }
    ]
}

# HA's recorder rejects statistics rows whose start isn't top-of-the-hour, so
# every canned timestamp below is deliberately hour/day/month aligned.
_ALIGNED_HOUR_UTC = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
_ALIGNED_HOUR_MS = int(_ALIGNED_HOUR_UTC.timestamp() * 1000)

HOURLY_RESPONSE = {
    "operationSuccess": True,
    "data": {"usage": [["CCF", "INCHES", "FAHRENHEIT"], [_ALIGNED_HOUR_MS, 1, 0, 70]]},
}

_ALIGNED_DAY_UTC = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)
_ALIGNED_DAY_MS = int(_ALIGNED_DAY_UTC.timestamp() * 1000)

DAILY_RESPONSE = {
    "operationSuccess": True,
    "data": {"usage": [["CCF"], [_ALIGNED_DAY_MS, 2]]},
}

_ALIGNED_MONTH_UTC = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
_ALIGNED_MONTH_MS = int(_ALIGNED_MONTH_UTC.timestamp() * 1000)

MONTHLY_RESPONSE = {
    "operationSuccess": True,
    "data": {
        "usage": [["CCF"], [_ALIGNED_MONTH_MS, 30]],
        "hasPrev": False,
        "start": _ALIGNED_MONTH_MS,
    },
}


# A fixed "current time" two days after DAILY_RESPONSE's entry, so the
# recurring refresh's trailing window (measured back from now) always
# contains the canned data. Mid-day UTC keeps it on the same calendar date in
# the test harness's default local time zone.
FIXTURE_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def fixture_now(freezer):
    """Freeze the clock at FIXTURE_NOW.

    Request this *before* `recorder_mock`/`hass` so the whole Home Assistant
    instance (and the refresh that runs once on setup) sees the frozen time
    from the start.
    """
    freezer.move_to(FIXTURE_NOW)
    return FIXTURE_NOW


def baseline_race_errors(caplog):
    """Return the coordinator's "baseline changed" ERROR records.

    Matching on caplog.text alone is not enough: it also contains unrelated
    recorder/event-bus debug output (e.g. `state_changed` events) whose
    presence depends on scheduling, so a substring check could pass without
    the abort ever having happened.
    """
    return [
        record
        for record in caplog.records
        if record.name == "custom_components.sensus_analytics.coordinator"
        and record.levelno == logging.ERROR
        and "changed (was" in record.getMessage()
    ]


def make_mock_response(json_data, status_code=200):
    """Build a requests.Response-like Mock."""
    response = Mock()
    response.status_code = status_code
    response.headers = {}
    response.json.return_value = json_data
    response.raise_for_status = Mock()
    return response


def make_mock_session():
    """Build a requests.Session-like Mock that satisfies the coordinator's fetch flow.

    Branches by the requested `zoom` so the hourly (zoom=day), daily
    (zoom=month), and monthly-aggregate (zoom=year) endpoints each get
    correctly-shaped, correctly-aligned canned data.
    """
    session = Mock()

    def post_side_effect(url, **kwargs):
        if "j_spring_security_check" in url:
            return make_mock_response({}, status_code=302)
        return make_mock_response(WIDGET_RESPONSE)

    def get_side_effect(url, **kwargs):
        zoom = kwargs.get("params", {}).get("zoom")
        if zoom == "month":
            return make_mock_response(DAILY_RESPONSE)
        if zoom == "year":
            return make_mock_response(MONTHLY_RESPONSE)
        return make_mock_response(HOURLY_RESPONSE)

    session.post.side_effect = post_side_effect
    session.get.side_effect = get_side_effect
    return session


def config_entry_data(**overrides):
    data = dict(CONFIG_ENTRY_DATA_TEMPLATE)
    data.update(overrides)
    return data
