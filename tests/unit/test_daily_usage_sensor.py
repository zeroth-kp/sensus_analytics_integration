"""Pure unit test guarding against the SensusAnalyticsDailyUsageSensor state_class
regression - the same recorder-collision bug already fixed for
LastHourUsageSensor, recurring on a different entity (see coordinator.py's
async_backfill_daily_history / async_refresh_recent_daily_statistics).
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
