"""Shared usage-unit conversion logic for the coordinator and sensor entities."""

from .const import CF_PER_CCF, CF_TO_GALLON


# pylint: disable=too-many-return-statements
def convert_usage_value(usage, usage_unit, config_unit_type):
    """Convert a raw usage reading to the user's configured display unit.

    Always returns a float (or None) - never a raw passthrough - since
    long-term statistics rows require a numeric StatisticData.state.
    """
    if usage is None:
        return None
    try:
        usage_float = float(usage)
    except (ValueError, TypeError):
        return None

    if usage_unit == "CF" and config_unit_type == "gal":
        return round(usage_float * CF_TO_GALLON)
    if usage_unit == "CF" and config_unit_type == "CCF":
        return round(usage_float / CF_PER_CCF, 2)
    if usage_unit == "GAL" and config_unit_type == "gal":
        return usage_float
    if usage_unit == "GAL" and config_unit_type == "CCF":
        return round(usage_float / CF_TO_GALLON / CF_PER_CCF, 2)

    return usage_float
