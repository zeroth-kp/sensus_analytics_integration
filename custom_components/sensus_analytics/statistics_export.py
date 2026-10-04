"""Export water statistics to a JSON file under the config directory.

The file keeps every hourly row (start, state, sum) plus the stored metadata
of each statistic, in the shape ``recorder/import_statistics`` accepts, so a
statistic can be restored exactly from it. Nothing in the recorder is changed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

EXPORT_DIRECTORY = "sensus_analytics_exports"
EXPORT_FORMAT = "sensus_analytics_statistics_export"
EXPORT_FORMAT_VERSION = 1
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class ExportResult:
    """Outcome of one export run."""

    ok: bool
    path: str | None = None
    statistics: dict[str, dict[str, Any]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a service-response friendly representation."""
        return {
            "ok": self.ok,
            "path": self.path,
            "statistics": self.statistics,
            "missing": self.missing,
            "error": self.error,
        }


def _iso(timestamp: float | None) -> str | None:
    return None if timestamp is None else dt_util.utc_from_timestamp(timestamp).isoformat()


def export_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert recorder rows to import-ready rows with ISO UTC start times."""
    exported = []
    for row in rows:
        item = {"start": _iso(row["start"]), "state": row.get("state"), "sum": row.get("sum")}
        if row.get("last_reset") is not None:
            item["last_reset"] = _iso(row["last_reset"])
        exported.append(item)
    return exported


def summarize(stats: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the row count, first/last hour and final sum of exported rows."""
    return {
        "rows": len(stats),
        "first_hour": stats[0]["start"] if stats else None,
        "last_hour": stats[-1]["start"] if stats else None,
        "last_sum": stats[-1]["sum"] if stats else None,
    }


def write_export_file(path: str, payload: dict[str, Any]) -> None:
    """Write the payload atomically, so a partial file is never left behind (blocking)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, default=str)
    os.replace(temp_path, path)


async def _async_read_statistics(
    hass: HomeAssistant, statistic_ids: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``{metadata, stats}`` for each statistic that exists, plus the ids that don't."""
    recorder = get_instance(hass)
    wanted = list(dict.fromkeys(statistic_ids))
    stored = await recorder.async_add_executor_job(partial(get_metadata, hass, statistic_ids=set(wanted)))
    present = [statistic_id for statistic_id in wanted if statistic_id in stored]
    missing = [statistic_id for statistic_id in wanted if statistic_id not in stored]
    if not present:
        return [], missing
    rows = await recorder.async_add_executor_job(
        statistics_during_period, hass, _EPOCH, None, set(present), "hour", None, {"state", "sum", "last_reset"}
    )
    statistics = [
        {"metadata": dict(stored[statistic_id][1]), "stats": export_rows(list(rows.get(statistic_id, [])))}
        for statistic_id in present
    ]
    return statistics, missing


async def async_export_statistics(
    hass: HomeAssistant, statistic_ids: list[str], *, config_entry_id: str
) -> ExportResult:
    """Write every hourly row and the metadata of each existing statistic to one file."""
    statistics, missing = await _async_read_statistics(hass, statistic_ids)
    summaries = {item["metadata"]["statistic_id"]: summarize(item["stats"]) for item in statistics}
    exported_at = dt_util.utcnow().replace(microsecond=0)
    payload = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_FORMAT_VERSION,
        "exported_at": exported_at.isoformat(),
        "config_entry_id": config_entry_id,
        "statistics": statistics,
    }
    filename = f"statistics_{config_entry_id.lower()}_{exported_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    path = hass.config.path(EXPORT_DIRECTORY, filename)
    try:
        await hass.async_add_executor_job(write_export_file, path, payload)
    except OSError as err:
        return ExportResult(ok=False, statistics=summaries, missing=missing, error=f"could not write {path}: {err}")
    return ExportResult(ok=True, path=path, statistics=summaries, missing=missing)
