from __future__ import annotations

from datetime import datetime, timezone
import json

from src.operations.adapters.application import _collect_data_requests
from src.operations.models import JobDefinition, JobStatus


def _job() -> JobDefinition:
    return JobDefinition(
        job_id="data_requests",
        display_name="缺数队列",
        category="application",
        run_type="worker",
        adapter="data_requests",
        order=1,
        enabled_expected=True,
        schedule={
            "oldest_pending_minutes": 20,
            "stale_running_minutes": 30,
        },
    )


def _row(
    request_id: str,
    status: str,
    *,
    universe: str = "WATCHLIST_ABC",
    schema_version: int = 1,
    initial_start: str = "2020-01-01",
    end: str = "2026-08-11",
    finished_at: str,
) -> dict[str, object]:
    return {
        "request_id": request_id,
        "request_key": request_id,
        "data_universe": universe,
        "status": status,
        "payload_json": json.dumps(
            {
                "schema_version": schema_version,
                "initial_start": initial_start,
                "start": "2021-01-01",
                "end": end,
                "tickers": ["AAA", "BBB"],
            }
        ),
        "result_json": "{}" if status == "success" else None,
        "error": "old failure" if status == "failed" else None,
        "attempts": 1,
        "created_at": finished_at,
        "updated_at": finished_at,
        "started_at": finished_at,
        "finished_at": finished_at,
    }


def test_later_covering_success_resolves_current_queue_health(monkeypatch):
    rows = [
        _row(
            "new-success",
            "success",
            schema_version=2,
            finished_at="2026-08-11T15:18:00+00:00",
        ),
        _row(
            "old-failure",
            "failed",
            finished_at="2026-08-11T15:03:00+00:00",
        ),
    ]
    monkeypatch.setattr(
        "src.operations.adapters.application.sqlite_tables",
        lambda _path: {"data_requests"},
    )
    monkeypatch.setattr(
        "src.operations.adapters.application.sqlite_rows",
        lambda *_args, **_kwargs: rows,
    )

    result = _collect_data_requests(
        _job(),
        now=datetime(2026, 8, 11, 16, 0, tzinfo=timezone.utc),
        observed_at="2026-08-11T16:00:00+00:00",
    )

    snapshot = result.snapshots[0]
    assert snapshot.status == JobStatus.SUCCESS
    assert snapshot.metrics["status_counts"]["FAILED"] == 1
    assert snapshot.metrics["unresolved_failed_count"] == 0
    assert snapshot.metrics["superseded_failed_count"] == 1
    assert any(run.status == JobStatus.FAILED for run in result.runs)


def test_narrower_success_does_not_hide_unrepaired_failure(monkeypatch):
    rows = [
        _row(
            "narrow-success",
            "success",
            schema_version=2,
            initial_start="2021-01-01",
            finished_at="2026-08-11T15:18:00+00:00",
        ),
        _row(
            "old-failure",
            "failed",
            finished_at="2026-08-11T15:03:00+00:00",
        ),
    ]
    monkeypatch.setattr(
        "src.operations.adapters.application.sqlite_tables",
        lambda _path: {"data_requests"},
    )
    monkeypatch.setattr(
        "src.operations.adapters.application.sqlite_rows",
        lambda *_args, **_kwargs: rows,
    )

    result = _collect_data_requests(
        _job(),
        now=datetime(2026, 8, 11, 16, 0, tzinfo=timezone.utc),
        observed_at="2026-08-11T16:00:00+00:00",
    )

    snapshot = result.snapshots[0]
    assert snapshot.status == JobStatus.DEGRADED
    assert snapshot.metrics["unresolved_failed_count"] == 1
    assert snapshot.metrics["superseded_failed_count"] == 0
