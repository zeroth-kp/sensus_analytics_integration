"""Unit tests for the metadata coordinator._import_statistics passes to the recorder.

Home Assistant requires ``unit_class`` on imported statistics metadata, so every
statistics write must include it alongside the existing sum/mean fields.
"""

from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.util.unit_conversion import VolumeConverter

from custom_components.sensus_analytics.coordinator import SensusAnalyticsDataUpdateCoordinator


def _make_coordinator():
    coordinator = SensusAnalyticsDataUpdateCoordinator.__new__(SensusAnalyticsDataUpdateCoordinator)
    coordinator.hass = SimpleNamespace()
    return coordinator


def _captured_metadata(unit):
    coordinator = _make_coordinator()
    with patch("custom_components.sensus_analytics.coordinator.async_import_statistics") as mock_import:
        count = coordinator._import_statistics("sensor.x", unit, [{"sum": 1.0}], "test")
    assert count == 1
    mock_import.assert_called_once()
    return mock_import.call_args.args[1]


def test_metadata_includes_volume_unit_class():
    metadata = _captured_metadata("gal")
    assert metadata["unit_class"] == VolumeConverter.UNIT_CLASS
    assert metadata["unit_of_measurement"] == "gal"


def test_metadata_keeps_sum_and_source_fields():
    metadata = _captured_metadata("CCF")
    assert metadata["has_sum"] is True
    assert metadata["source"] == "recorder"
    assert metadata["statistic_id"] == "sensor.x"
    assert metadata["unit_class"] == VolumeConverter.UNIT_CLASS
