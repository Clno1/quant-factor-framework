from __future__ import annotations

from datetime import datetime, timezone
import json

from src.operations.adapters.application import (
    _collect_data_requests,
    _collect_paper_notifications,
)
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


def _paper_notification_job(kind: str) -> JobDefinition:
    daily = kind == "DAILY_SUMMARY"
    return JobDefinition(
        job_id="paper_daily_summary" if daily else "paper_fill_notifications",
        display_name="模拟盘通知",
        category="delivery",
        run_type="scheduled_batch" if daily else "interval_worker",
        adapter="paper_notifications",
        order=1,
        enabled_expected=True,
        schedule=(
            {
                "timezone": "Asia/Singapore",
                "weekdays": ["TU", "WE", "TH", "FR", "SA"],
                "time": "11:00",
                "deadline_minutes": 20,
                "target_policy": "latest_publishable_xnys",
            }
            if daily
            else {"interval_minutes": 2, "target_policy": "latest_publishable_xnys"}
        ),
        evidence={"kind": kind},
    )


def test_paper_fill_notification_unknown_is_degraded(monkeypatch, tmp_path):
    database = tmp_path / "state.sqlite3"
    database.touch()
    rows = [{
        "delivery_id": "paper-fill:fill-1",
        "kind": "FILL",
        "account_id": "account-1",
        "target_session": "2026-08-28",
        "source_id": "fill-1",
        "status": "UNKNOWN",
        "attempts": 1,
        "created_at": "2026-08-30T08:00:00Z",
        "updated_at": "2026-08-30T08:00:00Z",
        "last_error": "response timeout",
        "last_error_code": "response_timeout",
    }]
    monkeypatch.setattr(
        "src.operations.adapters.application._paper_notification_db_path",
        lambda: database,
    )
    monkeypatch.setattr(
        "src.operations.adapters.application.sqlite_tables",
        lambda _path: {"paper_notification_outbox"},
    )
    monkeypatch.setattr(
        "src.operations.adapters.application.sqlite_rows",
        lambda *_args, **_kwargs: rows,
    )

    result = _collect_paper_notifications(
        _paper_notification_job("FILL"),
        now=datetime(2026, 8, 30, 8, 30, tzinfo=timezone.utc),
        observed_at="2026-08-30T08:30:00+00:00",
    )

    assert result.snapshots[0].status == JobStatus.DEGRADED
    assert result.snapshots[0].metrics["unknown"] == 1
    assert result.runs[0].delivery_status == "UNKNOWN"


def test_paper_daily_sent_is_current_success(monkeypatch, tmp_path):
    database = tmp_path / "state.sqlite3"
    database.touch()
    rows = [{
        "delivery_id": "paper-daily:2026-08-28",
        "kind": "DAILY_SUMMARY",
        "account_id": None,
        "target_session": "2026-08-28",
        "source_id": "2026-08-28",
        "status": "SENT",
        "attempts": 1,
        "message_id": "discord-1",
        "created_at": "2026-08-30T03:00:00Z",
        "updated_at": "2026-08-30T03:00:01Z",
        "sent_at": "2026-08-30T03:00:01Z",
    }]
    monkeypatch.setattr(
        "src.operations.adapters.application._paper_notification_db_path",
        lambda: database,
    )
    monkeypatch.setattr(
        "src.operations.adapters.application.sqlite_tables",
        lambda _path: {"paper_notification_outbox"},
    )
    monkeypatch.setattr(
        "src.operations.adapters.application.sqlite_rows",
        lambda *_args, **_kwargs: rows,
    )

    result = _collect_paper_notifications(
        _paper_notification_job("DAILY_SUMMARY"),
        now=datetime(2026, 8, 30, 8, 30, tzinfo=timezone.utc),
        observed_at="2026-08-30T08:30:00+00:00",
    )

    assert result.snapshots[0].status == JobStatus.SUCCESS
    assert result.snapshots[0].target_session == "2026-08-28"
