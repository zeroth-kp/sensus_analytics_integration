"""Unit tests for the fields apply_sum_statistic_fields sets on statistics metadata.

Home Assistant requires ``unit_class`` on imported statistics metadata, so every
statistics write must include it alongside the existing sum/mean fields.
"""

from homeassistant.components.recorder.models import StatisticMeanType, StatisticMetaData
from homeassistant.util.unit_conversion import VolumeConverter

from custom_components.sensus_analytics.coordinator import apply_sum_statistic_fields


def _metadata(unit):
    return StatisticMetaData(
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id="sensor.x",
        unit_of_measurement=unit,
    )


def test_metadata_includes_volume_unit_class():
    metadata = apply_sum_statistic_fields(_metadata("gal"))
    assert metadata["unit_class"] == VolumeConverter.UNIT_CLASS
    assert metadata["mean_type"] == StatisticMeanType.NONE
    assert metadata["unit_of_measurement"] == "gal"


def test_metadata_keeps_sum_and_source_fields():
    metadata = apply_sum_statistic_fields(_metadata("CCF"))
    assert metadata["has_sum"] is True
    assert metadata["source"] == "recorder"
    assert metadata["statistic_id"] == "sensor.x"
    assert metadata["unit_class"] == VolumeConverter.UNIT_CLASS
