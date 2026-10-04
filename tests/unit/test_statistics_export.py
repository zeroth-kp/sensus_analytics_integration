"""Unit tests for the statistics export helpers."""

import json
from datetime import datetime, timezone

from custom_components.sensus_analytics.statistics_export import export_rows, summarize, write_export_file

HOUR_TS = datetime(2026, 7, 20, 5, 0, tzinfo=timezone.utc).timestamp()


def test_export_rows_use_iso_utc_starts_and_keep_last_reset_only_when_set():
    rows = export_rows(
        [
            {"start": HOUR_TS, "state": 2.0, "sum": 10.0, "last_reset": None},
            {"start": HOUR_TS + 3600, "state": 3.0, "sum": 13.0, "last_reset": HOUR_TS},
        ]
    )
    assert rows == [
        {"start": "2026-07-20T05:00:00+00:00", "state": 2.0, "sum": 10.0},
        {
            "start": "2026-07-20T06:00:00+00:00",
            "state": 3.0,
            "sum": 13.0,
            "last_reset": "2026-07-20T05:00:00+00:00",
        },
    ]


def test_summarize_reports_count_bounds_and_final_sum():
    stats = export_rows(
        [{"start": HOUR_TS, "state": 2.0, "sum": 10.0}, {"start": HOUR_TS + 3600, "state": 3.0, "sum": 13.0}]
    )
    assert summarize(stats) == {
        "rows": 2,
        "first_hour": "2026-07-20T05:00:00+00:00",
        "last_hour": "2026-07-20T06:00:00+00:00",
        "last_sum": 13.0,
    }
    assert summarize([]) == {"rows": 0, "first_hour": None, "last_hour": None, "last_sum": None}


def test_write_export_file_creates_the_directory_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "exports" / "out.json"
    write_export_file(str(path), {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}
    assert [item.name for item in path.parent.iterdir()] == ["out.json"]
