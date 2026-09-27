"""Unit tests for Sensus login classification and the daily-data fetch's failure handling."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from custom_components.sensus_analytics.coordinator import (
    SensusAnalyticsDataUpdateCoordinator,
    SensusAuthError,
    SensusFetchError,
    authenticated_session,
    check_login_response,
)


@pytest.mark.parametrize(
    ("status", "location"),
    [(302, "https://example.invalid/water/"), (302, ""), (302, "/dashboard?tab=usage")],
)
def test_successful_login_redirects_pass(status, location):
    check_login_response(status, location)


@pytest.mark.parametrize(
    ("status", "location"),
    [
        (302, "https://example.invalid/login.html#/failed"),  # marker only in the fragment
        (302, "/login?error"),
        (302, "/login?login_error=1"),
        (200, ""),  # login form re-rendered
        (401, ""),
    ],
)
def test_rejected_credentials_are_auth_errors(status, location):
    with pytest.raises(SensusAuthError):
        check_login_response(status, location)


@pytest.mark.parametrize("status", [403, 429, 500, 502, 503])
def test_service_problems_are_not_auth_errors(status):
    with pytest.raises(SensusFetchError) as error:
        check_login_response(status, "")
    assert not isinstance(error.value, SensusAuthError)


def test_login_transport_errors_are_fetch_errors():
    session = Mock()
    session.post.side_effect = requests.exceptions.ConnectionError("down")
    with patch("custom_components.sensus_analytics.coordinator.requests.Session", return_value=session):
        with pytest.raises(SensusFetchError, match="Authentication request failed"):
            authenticated_session("https://example.invalid/", "user", "pass")


def _coordinator():
    coordinator = SensusAnalyticsDataUpdateCoordinator.__new__(SensusAnalyticsDataUpdateCoordinator)
    coordinator.base_url = "https://example.invalid/"
    coordinator.account_number = "acct"
    coordinator.meter_number = "meter"
    return coordinator


def _session_returning(response):
    return SimpleNamespace(post=Mock(return_value=response))


def _response(json_value=None, json_error=None, http_error=None):
    response = Mock()
    response.raise_for_status = Mock(side_effect=http_error)
    response.json = Mock(return_value=json_value, side_effect=json_error)
    return response


def test_daily_fetch_returns_the_first_device():
    payload = {"widgetList": [{"data": {"devices": [{"dailyUsage": 5}]}}]}
    assert _coordinator()._fetch_daily_data(_session_returning(_response(payload))) == {"dailyUsage": 5}


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (_response(json_error=ValueError("<html>maintenance</html>")), "not valid JSON"),
        (_response({"widgetList": [{"data": {"devices": []}}]}), "no meter data"),
        (_response({"widgetList": [{"data": "nodata"}]}), "no meter data"),
        (_response({"widgetList": []}), "did not contain meter data"),
        (_response({}, http_error=requests.exceptions.HTTPError("503")), "Daily data request failed"),
    ],
)
def test_daily_fetch_failures_are_fetch_errors(response, message):
    with pytest.raises(SensusFetchError, match=message):
        _coordinator()._fetch_daily_data(_session_returning(response))
