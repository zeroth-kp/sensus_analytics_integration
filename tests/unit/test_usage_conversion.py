"""Pure unit tests for the shared usage conversion function and the sensor wrapper.

These guard against custom_components/sensus_analytics/sensor.py's
UsageConversionMixin._convert_usage drifting apart from
usage_conversion.convert_usage_value, which the statistics importer uses.
"""

from types import SimpleNamespace

from custom_components.sensus_analytics.sensor import UsageConversionMixin
from custom_components.sensus_analytics.usage_conversion import convert_usage_value


def test_cf_to_gal_rounds_to_int():
    assert convert_usage_value(10, "CF", "gal") == round(10 * 7.48052)


def test_cf_to_ccf_rounds_to_two_decimals():
    assert convert_usage_value(250, "CF", "CCF") == round(250 / 100, 2)


def test_gal_to_gal_returns_float():
    result = convert_usage_value(5, "GAL", "gal")
    assert result == 5.0
    assert isinstance(result, float)


def test_gal_to_ccf_math():
    assert convert_usage_value(748.052, "GAL", "CCF") == round(748.052 / 7.48052 / 100, 2)


def test_none_usage_returns_none():
    assert convert_usage_value(None, "CF", "gal") is None


def test_non_numeric_usage_returns_none():
    assert convert_usage_value("not-a-number", "CF", "gal") is None


def test_unknown_unit_falls_through_to_float():
    result = convert_usage_value("7", "WEIRD_UNIT", "gal")
    assert result == 7.0
    assert isinstance(result, float)


def test_sensor_mixin_delegates_to_shared_function():
    entity = SimpleNamespace(
        coordinator=SimpleNamespace(
            data={"usageUnit": "CF"},
            config_entry=SimpleNamespace(data={"unit_type": "gal"}),
        )
    )
    result = UsageConversionMixin._convert_usage(entity, 10)
    assert result == round(10 * 7.48052)


def test_sensor_mixin_uses_explicit_usage_unit_over_coordinator_data():
    entity = SimpleNamespace(
        coordinator=SimpleNamespace(
            data={"usageUnit": "CF"},
            config_entry=SimpleNamespace(data={"unit_type": "gal"}),
        )
    )
    result = UsageConversionMixin._convert_usage(entity, 5, usage_unit="GAL")
    assert result == 5.0


def test_sensor_and_shared_function_agree_on_gal_to_gal():
    """Regression test for the exact case that used to diverge (raw usage vs. float)."""
    entity = SimpleNamespace(
        coordinator=SimpleNamespace(
            data={"usageUnit": "GAL"},
            config_entry=SimpleNamespace(data={"unit_type": "gal"}),
        )
    )
    sensor_result = UsageConversionMixin._convert_usage(entity, "5")

    shared_result = convert_usage_value("5", "GAL", "gal")

    assert sensor_result == shared_result == 5.0
