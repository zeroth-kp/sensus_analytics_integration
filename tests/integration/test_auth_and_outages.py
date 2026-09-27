"""Integration tests: rejected credentials trigger reauth, while outages only retry."""

from unittest.mock import Mock, patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sensus_analytics.const import DOMAIN

from .conftest import config_entry_data, make_mock_response, make_mock_session

SESSION = "custom_components.sensus_analytics.coordinator.requests.Session"
REJECTED = (302, "https://example.invalid/login.html#/failed")


def _session_with_login(status, location="", widget_json_error=None):
    """A mock session whose login returns ``status``/``location``; everything else as usual."""
    session = make_mock_session()
    normal_post = session.post.side_effect

    def post(url, **kwargs):
        if "j_spring_security_check" in url:
            response = make_mock_response({}, status_code=status)
            response.headers = {"Location": location}
            return response
        response = normal_post(url, **kwargs)
        if widget_json_error is not None:
            response.json = Mock(side_effect=widget_json_error)
        return response

    session.post.side_effect = post
    return session


async def _setup(hass, session):
    entry = MockConfigEntry(domain=DOMAIN, data=config_entry_data(unit_type="gal"))
    entry.add_to_hass(hass)
    with patch(SESSION, return_value=session):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _reauth_flows(hass):
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == config_entries.SOURCE_REAUTH
    ]


@pytest.mark.asyncio
async def test_rejected_login_starts_reauth(fixture_now, recorder_mock, enable_custom_integrations, hass):
    entry = await _setup(hass, _session_with_login(*REJECTED))
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert len(_reauth_flows(hass)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 503])
async def test_login_outage_retries_without_reauth(
    fixture_now, recorder_mock, enable_custom_integrations, hass, status
):
    entry = await _setup(hass, _session_with_login(status))
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert _reauth_flows(hass) == []


@pytest.mark.asyncio
async def test_maintenance_page_retries_without_reauth(fixture_now, recorder_mock, enable_custom_integrations, hass):
    entry = await _setup(hass, _session_with_login(302, widget_json_error=ValueError("<html>")))
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert _reauth_flows(hass) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("login", "expected"),
    [((302, ""), None), (REJECTED, "auth"), ((200, ""), "auth"), ((503, ""), "cannot_connect")],
)
async def test_user_step_checks_credentials(
    fixture_now, recorder_mock, enable_custom_integrations, hass, login, expected
):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch(SESSION, return_value=_session_with_login(*login)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=config_entry_data(unit_type="gal")
        )
        await hass.async_block_till_done()
    if expected is None:
        assert result["type"] == FlowResultType.CREATE_ENTRY
    else:
        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {"base": expected}


@pytest.mark.asyncio
async def test_reauth_updates_credentials_and_reloads(fixture_now, recorder_mock, enable_custom_integrations, hass):
    entry = await _setup(hass, _session_with_login(*REJECTED))
    (flow,) = _reauth_flows(hass)

    with patch(SESSION, return_value=_session_with_login(*REJECTED)):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], user_input={"username": "user", "password": "still-wrong"}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "auth"}

    with patch(SESSION, return_value=_session_with_login(302)):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], user_input={"username": "user", "password": "new-password"}
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new-password"
    assert entry.data["meter_number"] == config_entry_data()["meter_number"]
    assert entry.state is ConfigEntryState.LOADED
