from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from src.operations.models import (
    CollectionResult,
    IncidentCandidate,
    IncidentSeverity,
    JobSnapshot,
    JobStatus,
)
from src.operations.registry import OperationsRegistry
from src.operations.store import OperationsReader, OperationsStore
from src.operations.watchdog import OperationsWatchdog
from src.operations.adapters.delivery import collect_delivery_evidence


def test_watchdog_publishes_every_registered_job_without_external_alerts(
    monkeypatch,
    tmp_path: Path,
):
    registry = OperationsRegistry("configs/operations.yaml")
    store = OperationsStore(
        tmp_path / "operations.sqlite3",
        tmp_path / "snapshot.sqlite3",
    )
    monkeypatch.setattr(
        "src.operations.watchdog.collect_market_evidence",
        lambda *args, **kwargs: CollectionResult(),
    )
    monkeypatch.setattr(
        "src.operations.watchdog.collect_research_evidence",
        lambda *args, **kwargs: CollectionResult(),
    )
    monkeypatch.setattr(
        "src.operations.watchdog.collect_delivery_evidence",
        lambda *args, **kwargs: CollectionResult(),
    )
    monkeypatch.setattr(
        "src.operations.watchdog.collect_application_evidence",
        lambda *args, **kwargs: CollectionResult(),
    )
    monkeypatch.setattr(
        "src.operations.watchdog.collect_broad_evidence",
        lambda *args, **kwargs: CollectionResult(),
    )
    report = OperationsWatchdog(
        registry,
        store,
        inspect_systemd=False,
    ).run_once(now=datetime(2026, 8, 12, 20, 0, tzinfo=timezone.utc))

    assert report["status"] == "SUCCESS"
    assert report["external_notifications"] is False
    assert report["jobs_observed"] == len(registry.list())
    payload = OperationsReader(tmp_path / "snapshot.sqlite3").overview()
    assert {row["job_id"] for row in payload["jobs"]} == {
        job.job_id for job in registry.list()
    }


def test_watchdog_isolates_one_collector_failure(monkeypatch, tmp_path: Path):
    registry = OperationsRegistry("configs/operations.yaml")
    store = OperationsStore(
        tmp_path / "operations.sqlite3",
        tmp_path / "snapshot.sqlite3",
    )
    monkeypatch.setattr(
        "src.operations.watchdog.collect_market_evidence",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("secret=hidden")),
    )
    for name in (
        "collect_research_evidence",
        "collect_delivery_evidence",
        "collect_application_evidence",
        "collect_broad_evidence",
    ):
        monkeypatch.setattr(
            f"src.operations.watchdog.{name}",
            lambda *args, **kwargs: CollectionResult(),
        )
    report = OperationsWatchdog(
        registry,
        store,
        inspect_systemd=False,
    ).run_once(now=datetime(2026, 8, 12, 20, 0, tzinfo=timezone.utc))
    assert report["status"] == "DEGRADED"
    assert report["collector_errors"][0]["collector"] == "market"
    assert "hidden" not in report["collector_errors"][0]["error"]
    incidents = OperationsReader(tmp_path / "snapshot.sqlite3").incidents()
    assert any(row["code"] == "EVIDENCE_COLLECTOR_FAILED" for row in incidents)


def test_watchdog_preserves_coherent_evidence_during_collector_failure(
    monkeypatch,
    tmp_path: Path,
):
    registry = OperationsRegistry("configs/operations.yaml")
    store = OperationsStore(
        tmp_path / "operations.sqlite3",
        tmp_path / "snapshot.sqlite3",
    )
    store.initialize()
    store.sync_job_definitions(
        registry.list(),
        observed_at="2026-08-12T19:00:00+00:00",
    )
    store.upsert_snapshots([JobSnapshot(
        job_id="core_market_data",
        status=JobStatus.SUCCESS,
        observed_at="2026-08-12T19:00:00+00:00",
        target_session="2026-08-12",
        output_version="SP500:abc123",
    )])
    store.reconcile_incidents([IncidentCandidate(
        fingerprint="existing:market_warning",
        severity=IncidentSeverity.WARNING,
        code="EXISTING_WARNING",
        title="既有行情异常",
        detail="等待下一次完整采集确认恢复",
        job_id="core_market_data",
    )], observed_at="2026-08-12T19:00:00+00:00")

    monkeypatch.setattr(
        "src.operations.watchdog.collect_market_evidence",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("catalog locked")),
    )
    for name in (
        "collect_research_evidence",
        "collect_delivery_evidence",
        "collect_application_evidence",
        "collect_broad_evidence",
    ):
        monkeypatch.setattr(
            f"src.operations.watchdog.{name}",
            lambda *args, **kwargs: CollectionResult(),
        )

    OperationsWatchdog(
        registry,
        store,
        inspect_systemd=False,
    ).run_once(now=datetime(2026, 8, 12, 20, 0, tzinfo=timezone.utc))

    reader = OperationsReader(tmp_path / "snapshot.sqlite3")
    snapshot = reader.job("core_market_data")["snapshot"]
    assert snapshot["status"] == "SUCCESS"
    assert snapshot["output_version"] == "SP500:abc123"
    incidents = reader.incidents()
    assert any(row["code"] == "EXISTING_WARNING" for row in incidents)
    assert any(row["code"] == "EVIDENCE_COLLECTOR_FAILED" for row in incidents)


def test_missing_premarket_channels_are_visible_as_expected_deliveries(
    monkeypatch,
    tmp_path: Path,
):
    registry = OperationsRegistry("configs/operations.yaml")
    job = registry.get("premarket_digest")
    monkeypatch.setattr(
        "src.operations.adapters.delivery.PREMARKET_DB",
        tmp_path / "missing.sqlite3",
    )

    result = collect_delivery_evidence(
        [job],
        now=datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc),
        observed_at="2026-08-12T14:00:00+00:00",
    )

    assert result.snapshots[0].status == JobStatus.MISSED
    assert {row.channel for row in result.deliveries} == {
        "盘前动量",
        "盘前板块轮动",
    }
    assert {row.status for row in result.deliveries} == {"MISSED"}


def _write_premarket_deliveries(
    path: Path,
    *,
    created_at: str,
    status: str = "PENDING",
) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE deliveries (
                target_session TEXT NOT NULL,
                channel TEXT NOT NULL,
                destination TEXT NOT NULL,
                source_session TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                message_id TEXT,
                last_error_code TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT,
                retryable INTEGER,
                PRIMARY KEY (target_session, channel)
            );
        """)
        for channel in ("momentum", "sector-rotation"):
            connection.execute(
                """INSERT INTO deliveries VALUES
                   (?, ?, 'discord:test', '2026-08-11', 'sha256:test', '{}',
                    ?, 0, NULL, NULL, NULL, ?, ?, NULL, 1)""",
                ("2026-08-12", channel, status, created_at, created_at),
            )


def test_pending_premarket_rows_become_missed_after_delivery_deadline(
    monkeypatch,
    tmp_path: Path,
):
    database = tmp_path / "premarket.sqlite3"
    _write_premarket_deliveries(
        database,
        created_at="2026-08-12T14:25:00+00:00",
    )
    monkeypatch.setattr("src.operations.adapters.delivery.PREMARKET_DB", database)
    registry = OperationsRegistry("configs/operations.yaml")

    result = collect_delivery_evidence(
        [registry.get("premarket_digest")],
        now=datetime(2026, 8, 12, 14, 35, tzinfo=timezone.utc),
        observed_at="2026-08-12T14:35:00+00:00",
    )

    snapshot = result.snapshots[0]
    assert snapshot.status == JobStatus.MISSED
    assert snapshot.metrics["past_deadline"] is True
    assert {row.status for row in result.deliveries} == {"MISSED"}
    assert {row.metadata["source_status"] for row in result.deliveries} == {"PENDING"}
    assert result.runs[0].status == JobStatus.MISSED


def test_complete_premarket_payloads_are_degraded_when_prepared_late(
    monkeypatch,
    tmp_path: Path,
):
    database = tmp_path / "premarket.sqlite3"
    _write_premarket_deliveries(
        database,
        created_at="2026-08-12T13:25:00+00:00",
    )
    monkeypatch.setattr("src.operations.adapters.delivery.PREMARKET_DB", database)
    registry = OperationsRegistry("configs/operations.yaml")

    result = collect_delivery_evidence(
        [registry.get("premarket_digest_prepare")],
        now=datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc),
        observed_at="2026-08-12T14:00:00+00:00",
    )

    snapshot = result.snapshots[0]
    assert snapshot.status == JobStatus.DEGRADED
    assert snapshot.metrics["prepared_late"] is True
    assert snapshot.last_success_at == "2026-08-12T13:25:00+00:00"
    assert result.runs[0].status == JobStatus.DEGRADED
    assert result.runs[0].metadata["prepared_late"] is True


def test_hourly_retries_do_not_count_as_distinct_scheduled_hours(
    monkeypatch,
    tmp_path: Path,
):
    database = tmp_path / "hourly.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE alert_runs (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                completed_at TEXT, mode TEXT NOT NULL, status TEXT NOT NULL,
                session_date TEXT, market_open INTEGER, broad_count INTEGER,
                strict_count INTEGER, pending_count INTEGER,
                delivery_status TEXT, error TEXT, snapshot_json TEXT
            );
            INSERT INTO alert_runs VALUES
              ('retry-1','2026-08-12T14:36:00+00:00','2026-08-12T14:37:00+00:00','send','completed','2026-08-12',1,100,4,0,'skipped_empty',NULL,NULL),
              ('retry-2','2026-08-12T14:40:00+00:00','2026-08-12T14:41:00+00:00','send','completed','2026-08-12',1,100,4,0,'skipped_empty',NULL,NULL);
        """)
    monkeypatch.setattr(
        "src.operations.adapters.delivery.HOURLY_DB",
        database,
    )
    registry = OperationsRegistry("configs/operations.yaml")
    result = collect_delivery_evidence(
        [registry.get("hourly_momentum")],
        now=datetime(2026, 8, 12, 16, 0, tzinfo=timezone.utc),
        observed_at="2026-08-12T16:00:00+00:00",
    )

    snapshot = result.snapshots[0]
    assert snapshot.status == JobStatus.DEGRADED
    assert snapshot.metrics["runs_expected_so_far"] == 2
    assert snapshot.metrics["runs_completed"] == 1
    assert snapshot.metrics["run_attempts_completed"] == 2


def test_intraday_requires_an_eod_observation_after_market_close(
    monkeypatch,
    tmp_path: Path,
):
    database = tmp_path / "intraday.sqlite3"
    heartbeat = {
        "session_date": "2026-08-12",
        "updated_at": "2026-08-12T18:00:00+00:00",
        "phase": "open",
        "mode": "shadow",
        "errors": [],
    }
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE heartbeat (
                singleton INTEGER PRIMARY KEY, updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE session_observations (
                session_date TEXT PRIMARY KEY, status TEXT NOT NULL,
                expected_open_cycles INTEGER NOT NULL,
                observed_open_cycles INTEGER NOT NULL,
                cycle_coverage REAL NOT NULL, error_cycles INTEGER NOT NULL,
                error_cycle_ratio REAL NOT NULL, cycle_p95_seconds REAL NOT NULL,
                candidate_count INTEGER NOT NULL,
                failure_reasons_json TEXT NOT NULL, finalized_at TEXT NOT NULL
            );
        """)
        connection.execute(
            "INSERT INTO heartbeat VALUES (1, ?, ?)",
            (heartbeat["updated_at"], json.dumps(heartbeat)),
        )
    monkeypatch.setattr(
        "src.operations.adapters.delivery.INTRADAY_DB",
        database,
    )
    registry = OperationsRegistry("configs/operations.yaml")
    job = registry.get("intraday_momentum")
    now = datetime(2026, 8, 12, 21, 30, tzinfo=timezone.utc)

    missing = collect_delivery_evidence(
        [job], now=now, observed_at="2026-08-12T21:30:00+00:00"
    )
    assert missing.snapshots[0].status == JobStatus.STALE

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO session_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "2026-08-12", "PASS", 390, 389, 0.997, 0, 0.0,
                1.2, 25, "[]", "2026-08-12T20:06:00+00:00",
            ),
        )
    complete = collect_delivery_evidence(
        [job], now=now, observed_at="2026-08-12T21:31:00+00:00"
    )
    assert complete.snapshots[0].status == JobStatus.SUCCESS


def test_intraday_exposes_cup_handle_shadow_metrics(monkeypatch, tmp_path: Path):
    database = tmp_path / "intraday-cup.sqlite3"
    heartbeat = {
        "session_date": "2026-08-12",
        "updated_at": "2026-08-12T16:00:00+00:00",
        "phase": "completed",
        "mode": "shadow",
        "errors": [],
        "cup_handle": {
            "mode": "shadow",
            "algorithm_version": "daily-cup-5m-handle-shadow-v1",
            "error_count": 0,
        },
    }
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE heartbeat (
                singleton INTEGER PRIMARY KEY, updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE cup_handle_cycles (
                session_date TEXT, observed_at TEXT
            );
            CREATE TABLE cup_handle_evaluations (
                session_date TEXT, outcome TEXT, rejection_reason TEXT,
                latency_ms REAL, bar_count INTEGER
            );
            CREATE TABLE cup_handle_session_observations (
                session_date TEXT, algorithm_version TEXT, status TEXT
            );
        """)
        connection.execute(
            "INSERT INTO heartbeat VALUES (1, ?, ?)",
            (heartbeat["updated_at"], json.dumps(heartbeat)),
        )
        connection.execute(
            "INSERT INTO cup_handle_cycles VALUES (?, ?)",
            ("2026-08-12", "2026-08-12T16:00:00+00:00"),
        )
        connection.executemany(
            "INSERT INTO cup_handle_evaluations VALUES (?, ?, ?, ?, ?)",
            [
                ("2026-08-12", "MATCH", "MATCH", 1.0, 20),
                ("2026-08-12", "REJECTED", "RIM_NOT_BROKEN", 2.0, 20),
                ("2026-08-12", "REJECTED", "RIM_NOT_BROKEN", 3.0, 20),
            ],
        )
    monkeypatch.setattr("src.operations.adapters.delivery.INTRADAY_DB", database)
    registry = OperationsRegistry("configs/operations.yaml")

    result = collect_delivery_evidence(
        [registry.get("intraday_momentum")],
        now=datetime(2026, 8, 12, 16, 0, tzinfo=timezone.utc),
        observed_at="2026-08-12T16:00:00+00:00",
    )

    metrics = result.snapshots[0].metrics
    assert metrics["茶杯柄命中数"] == 1
    assert metrics["茶杯柄拒绝数"] == 2
    assert metrics["茶杯柄主要拒绝原因"] == {"RIM_NOT_BROKEN": 2}
    assert metrics["茶杯柄检测延迟P95毫秒"] == 2.9
    assert metrics["茶杯柄最大序列长度"] == 20


def test_intraday_keeps_latest_failed_cup_session_visible(monkeypatch, tmp_path: Path):
    database = tmp_path / "intraday-cup-failed.sqlite3"
    heartbeat = {
        "session_date": "2026-08-31",
        "updated_at": "2026-09-01T00:05:00+00:00",
        "phase": "completed_session",
        "mode": "shadow",
        "errors": [],
        "cup_handle": {
            "mode": "shadow",
            "algorithm_version": "daily-cup-5m-handle-shadow-v2",
            "error_count": 0,
        },
    }
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE heartbeat (
                singleton INTEGER PRIMARY KEY, updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE cup_handle_session_observations (
                session_date TEXT, algorithm_version TEXT, status TEXT,
                failure_reasons_json TEXT, evaluable_ticker_coverage REAL,
                gap_ticker_count INTEGER, gap_ticker_ratio REAL,
                gap_event_count INTEGER, gap_classification_counts_json TEXT
            );
        """)
        connection.execute(
            "INSERT INTO heartbeat VALUES (1, ?, ?)",
            (heartbeat["updated_at"], json.dumps(heartbeat)),
        )
        connection.execute(
            "INSERT INTO cup_handle_session_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-08-31",
                "daily-cup-5m-handle-shadow-v2",
                "FAIL",
                json.dumps(["EXCESSIVE_MINUTE_DATA_GAPS"]),
                0.9,
                4,
                0.1,
                5,
                json.dumps({"UNRESOLVED_SOURCE_GAP": 5}),
            ),
        )
    monkeypatch.setattr("src.operations.adapters.delivery.INTRADAY_DB", database)
    registry = OperationsRegistry("configs/operations.yaml")

    result = collect_delivery_evidence(
        [registry.get("intraday_momentum")],
        now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        observed_at="2026-09-01T12:00:00+00:00",
    )

    snapshot = result.snapshots[0]
    assert snapshot.status == JobStatus.DEGRADED
    assert "2026-08-31" in str(snapshot.status_reason)
    assert snapshot.metrics["茶杯柄最近完整交易日状态"] == "FAIL"
    assert snapshot.metrics["茶杯柄证据交易日"] == "2026-08-31"
    assert snapshot.metrics["茶杯柄数据缺口股票数"] == 4
    assert any(
        incident.code == "CUP_HANDLE_SHADOW_SESSION_FAILED"
        for incident in result.incidents
    )

    stale = collect_delivery_evidence(
        [registry.get("intraday_momentum")],
        now=datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc),
        observed_at="2026-09-01T14:00:00+00:00",
    )
    assert stale.snapshots[0].status == JobStatus.STALE
    assert any(
        incident.code == "CUP_HANDLE_SHADOW_SESSION_FAILED"
        for incident in stale.incidents
    )
