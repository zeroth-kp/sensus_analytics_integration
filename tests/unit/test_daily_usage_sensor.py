"""Pure unit test guarding against a state_class regression on the usage sensors.

HA's recorder compiling statistics from either sensor's states would collide
with, or duplicate, what the hourly statistics importer writes (see
statistics.py and the comments in sensor.py).
"""

from types import SimpleNamespace
from unittest.mock import Mock

from custom_components.sensus_analytics.sensor import LastHourUsageSensor, SensusAnalyticsDailyUsageSensor


def test_daily_usage_sensor_has_no_state_class():
    coordinator = Mock()
    coordinator.data = {"dailyUsage": 5, "usageUnit": "CCF"}
    entry = SimpleNamespace(entry_id="test_entry")

    sensor = SensusAnalyticsDailyUsageSensor(coordinator, entry)

    assert sensor.state_class is None


def test_daily_usage_sensor_does_not_override_last_reset():
    """A start-of-day last_reset would fight the coordinator's own imported
    statistics the same way a native state_class did - it should fall back
    to the base class's default (None), not define its own."""
    assert "last_reset" not in SensusAnalyticsDailyUsageSensor.__dict__


def test_last_hour_usage_sensor_has_no_state_class():
    """The statistics importer must stay the only writer of this sensor's history."""
    coordinator = Mock()
    coordinator.data = {"hourly_usage_data": [], "usageUnit": "CCF"}
    entry = SimpleNamespace(entry_id="test_entry")

    sensor = LastHourUsageSensor(coordinator, entry)

    assert sensor.state_class is None
