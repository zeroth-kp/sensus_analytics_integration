"""Shared fixtures/helpers for integration tests that need a working config entry."""

from datetime import datetime, timezone
from unittest.mock import Mock

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


def make_mock_response(json_data, status_code=200):
    """Build a requests.Response-like Mock."""
    response = Mock()
    response.status_code = status_code
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
