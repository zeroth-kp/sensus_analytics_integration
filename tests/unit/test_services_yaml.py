"""Pure unit test guarding services.yaml against drifting from __init__.py's schemas."""

from pathlib import Path

import yaml

SERVICES_YAML = Path(__file__).resolve().parents[2] / "custom_components" / "sensus_analytics" / "services.yaml"


def _load_services():
    return yaml.safe_load(SERVICES_YAML.read_text())


def test_both_services_expose_optional_config_entry_selector():
    services = _load_services()
    for service_name in ("backfill_hourly_statistics", "backfill_daily_history"):
        field = services[service_name]["fields"]["config_entry_id"]
        assert field["required"] is False
        assert field["selector"]["config_entry"]["integration"] == "sensus_analytics"
