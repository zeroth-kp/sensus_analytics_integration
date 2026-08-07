"""Pure unit tests for coordinator._verify_baseline_unchanged - the
compare-before-write guard shared by all three statistics-writing entry
points (hourly backfill, daily backfill, and the recurring daily refresh).

Exercises the comparison logic directly (baseline vs. a re-read of the same
anchor) without needing the full recorder harness - _get_existing_sum_before
is overridden per-test on the instance instead.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from custom_components.sensus_analytics.coordinator import SensusAnalyticsDataUpdateCoordinator

UTC = timezone.utc
ANCHOR = datetime(2026, 7, 6, 4, 0, tzinfo=UTC)


def _make_coordinator(re_read_returns):
    coordinator = SensusAnalyticsDataUpdateCoordinator.__new__(SensusAnalyticsDataUpdateCoordinator)
    coordinator.config_entry = SimpleNamespace(data={"unit_type": "gal"})

    async def fake_get_existing_sum_before(statistic_id, window_start):
        return re_read_returns

    coordinator._get_existing_sum_before = fake_get_existing_sum_before
    return coordinator


@pytest.mark.asyncio
async def test_unchanged_sum_passes():
    coordinator = _make_coordinator(re_read_returns=342979.0999999996)
    assert await coordinator._verify_baseline_unchanged(
        "sensor.x", ANCHOR, 342979.0999999996, log_label="test"
    )


@pytest.mark.asyncio
async def test_changed_sum_fails(caplog):
    coordinator = _make_coordinator(re_read_returns=305518.0999999996)
    with caplog.at_level("ERROR"):
        result = await coordinator._verify_baseline_unchanged(
            "sensor.x", ANCHOR, 342979.0999999996, log_label="test"
        )
    assert result is False
    assert "changed" in caplog.text
    assert "sensor.x" in caplog.text


@pytest.mark.asyncio
async def test_both_none_passes():
    """Neither read found anything at the anchor - no race, nothing to compare."""
    coordinator = _make_coordinator(re_read_returns=None)
    assert await coordinator._verify_baseline_unchanged("sensor.x", ANCHOR, None, log_label="test")


@pytest.mark.asyncio
async def test_none_expected_coerced_to_zero_matches_none_re_read():
    """_get_baseline_sum callers pass an already-0.0-coerced expected value even
    when the original read found nothing - that must not be treated as a
    mismatch against a re-read that (correctly, still) returns None.
    """
    coordinator = _make_coordinator(re_read_returns=None)
    assert await coordinator._verify_baseline_unchanged("sensor.x", ANCHOR, 0.0, log_label="test")


@pytest.mark.asyncio
async def test_none_becoming_real_value_fails():
    """Nothing existed at the anchor originally; something now does - a write
    landed there in between, exactly as much a race as two disagreeing sums.
    """
    coordinator = _make_coordinator(re_read_returns=88.0)
    result = await coordinator._verify_baseline_unchanged("sensor.x", ANCHOR, None, log_label="test")
    assert result is False


@pytest.mark.asyncio
async def test_tiny_float_drift_within_tolerance_passes():
    coordinator = _make_coordinator(re_read_returns=342979.10000000003)
    assert await coordinator._verify_baseline_unchanged(
        "sensor.x", ANCHOR, 342979.0999999996, log_label="test"
    )
