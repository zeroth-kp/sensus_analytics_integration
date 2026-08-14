"""Unit tests for the shared progressive tiered-billing calculator.

Regression coverage for the bug where the billing cost and daily fee
sensors treated tier prices as $/gallon instead of $/1000 gallons (up to a
~1000x overcharge), summed tier cutoffs into the wrong absolute threshold,
and had no way to express a free usage allowance or a fourth tier.
"""

from types import MethodType, SimpleNamespace

from custom_components.sensus_analytics.sensor import (
    SensusAnalyticsBillingCostSensor,
    SensusAnalyticsDailyFeeSensor,
    UsageConversionMixin,
)
from custom_components.sensus_analytics.tiered_billing import calculate_tiered_cost


def test_none_usage_returns_zero():
    assert calculate_tiered_cost(None, [(None, 5.0)]) == 0.0


def test_single_unbounded_tier_bills_per_thousand_gallons():
    # Flat-rate utility: one price, no cutoffs at all.
    assert calculate_tiered_cost(100, [(None, 5.0)]) == 0.5


def test_usage_within_free_allowance_is_zero():
    assert calculate_tiered_cost(1000, [(None, 5.0)], free_gallons=1000) == 0.0


def test_usage_above_free_allowance_bills_only_the_excess():
    assert calculate_tiered_cost(1500, [(None, 5.0)], free_gallons=1000) == 2.5


def test_usage_stopping_mid_tier_does_not_reach_next_tier():
    tiers = [(5000, 3.0), (8000, 4.0), (10000, 5.0), (None, 6.0)]
    # 4000 gal at tier1 (1000-5000) + 2000 gal at tier2 (5000-7000)
    assert calculate_tiered_cost(7000, tiers, free_gallons=1000) == 12.0 + 8.0


def test_usage_spanning_all_four_tiers():
    tiers = [(5000, 3.0), (8000, 4.0), (10000, 5.0), (None, 6.0)]
    # 4000@3 + 3000@4 + 2000@5 + 2000@6, each divided by 1000 gal
    expected = (4000 / 1000 * 3.0) + (3000 / 1000 * 4.0) + (2000 / 1000 * 5.0) + (2000 / 1000 * 6.0)
    assert calculate_tiered_cost(12000, tiers, free_gallons=1000) == expected


def test_unset_tier_price_stops_the_schedule():
    # tier2 has no price configured, so tier3/tier4 never get evaluated
    # even though usage is well past their cutoffs.
    tiers = [(5000, 3.0), (8000, None), (10000, 5.0), (None, 6.0)]
    assert calculate_tiered_cost(9000, tiers) == 15.0


def test_no_cutoffs_or_free_allowance_matches_simple_flat_rate_config():
    """Backward compat: an install with only tier1_price set (no gallon fields)."""
    tiers = [(None, 12.80), (None, None), (None, None), (None, None)]
    assert calculate_tiered_cost(2500, tiers) == 2500 / 1000 * 12.80


def _sensor_entity(config_data):
    entity = SimpleNamespace(coordinator=SimpleNamespace(config_entry=SimpleNamespace(data=config_data)))
    entity._get_tier_schedule = MethodType(UsageConversionMixin._get_tier_schedule, entity)
    return entity


def test_billing_cost_sensor_adds_service_fee_to_tiered_cost():
    config_data = {
        "service_fee": 20.0,
        "included_gallons": 1000,
        "tier1_gallons": 5000,
        "tier1_price": 3.0,
        "tier2_gallons": 8000,
        "tier2_price": 4.0,
        "tier3_gallons": 10000,
        "tier3_price": 5.0,
        "tier4_price": 6.0,
    }
    entity = _sensor_entity(config_data)
    cost = SensusAnalyticsBillingCostSensor._calculate_cost(entity, 12000)
    # 4000@tier1(1000-5000) + 3000@tier2(5000-8000) + 2000@tier3(8000-10000) + 2000@tier4(10000-12000)
    tiered = (4000 / 1000 * 3.0) + (3000 / 1000 * 4.0) + (2000 / 1000 * 5.0) + (2000 / 1000 * 6.0)
    assert cost == round(20.0 + tiered, 2)


def test_daily_fee_sensor_has_no_service_fee():
    config_data = {
        "service_fee": 20.0,
        "tier1_price": 5.0,
    }
    entity = _sensor_entity(config_data)
    fee = SensusAnalyticsDailyFeeSensor._calculate_daily_fee(entity, 200)
    assert fee == round(200 / 1000 * 5.0, 2)
