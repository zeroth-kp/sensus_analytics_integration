"""Integration tests for the options flow's handling of optional pricing fields."""

from unittest.mock import patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sensus_analytics.const import DOMAIN

from .conftest import CONFIG_ENTRY_DATA_TEMPLATE, config_entry_data, make_mock_session

SESSION = "custom_components.sensus_analytics.coordinator.requests.Session"

OPTIONAL_PRICING_FIELDS = (
    "included_gallons",
    "tier1_gallons",
    "tier2_gallons",
    "tier2_price",
    "tier3_gallons",
    "tier3_price",
    "tier4_price",
)

FULL_PRICING = {
    "included_gallons": 2000.0,
    "tier1_gallons": 10000.0,
    "tier1_price": 4.5,
    "tier2_gallons": 15000.0,
    "tier2_price": 5.5,
    "tier3_gallons": 20000.0,
    "tier3_price": 6.5,
    "tier4_price": 9.5,
    "service_fee": 20.0,
}


async def _setup_entry(hass, **overrides):
    entry = MockConfigEntry(domain=DOMAIN, data=config_entry_data(**overrides))
    entry.add_to_hass(hass)
    with patch(SESSION, return_value=make_mock_session()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _submit_options(hass, form, user_input):
    with patch(SESSION, return_value=make_mock_session()):
        result = await hass.config_entries.options.async_configure(form["flow_id"], user_input=user_input)
        await hass.async_block_till_done()
    return result


def _schema_field(form, name):
    return next(key for key in form["data_schema"].schema if key == name)


def _billing_cost_state(hass, entry):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{DOMAIN}_{entry.entry_id}_billing_cost")
    return hass.states.get(entity_id).state


@pytest.mark.asyncio
async def test_options_form_submits_with_unset_optional_pricing_fields(
    fixture_now, recorder_mock, enable_custom_integrations, hass
):
    entry = await _setup_entry(hass)
    cost_before = _billing_cost_state(hass, entry)
    form = await hass.config_entries.options.async_init(entry.entry_id)
    assert form["type"] == FlowResultType.FORM

    # The frontend leaves empty optional fields out of the submitted data.
    result = await _submit_options(hass, form, dict(CONFIG_ENTRY_DATA_TEMPLATE))

    assert result["type"] == FlowResultType.CREATE_ENTRY
    for field in OPTIONAL_PRICING_FIELDS:
        assert field not in entry.data
    assert _billing_cost_state(hass, entry) == cost_before


@pytest.mark.asyncio
async def test_options_form_prefills_and_keeps_set_pricing_fields(
    fixture_now, recorder_mock, enable_custom_integrations, hass
):
    entry = await _setup_entry(hass, **FULL_PRICING)
    cost_before = _billing_cost_state(hass, entry)
    form = await hass.config_entries.options.async_init(entry.entry_id)
    for field in OPTIONAL_PRICING_FIELDS:
        assert _schema_field(form, field).description == {"suggested_value": FULL_PRICING[field]}

    result = await _submit_options(hass, form, dict(entry.data))

    assert result["type"] == FlowResultType.CREATE_ENTRY
    for field, value in FULL_PRICING.items():
        assert entry.data[field] == value
    assert _billing_cost_state(hass, entry) == cost_before
