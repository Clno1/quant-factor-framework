from __future__ import annotations

from pathlib import Path
import sqlite3

from src.operations.models import (
    DeliveryObservation,
    FreshnessObservation,
    IncidentCandidate,
    IncidentSeverity,
    JobSnapshot,
    JobStatus,
    OperationRun,
    ProjectObservation,
    RunStage,
)
from src.operations.registry import OperationsRegistry
from src.operations.store import OperationsReader, OperationsStore
from src.operations.evidence import sqlite_rows


def test_operations_store_round_trip_and_incident_recovery(tmp_path: Path):
    registry = OperationsRegistry("configs/operations.yaml")
    database = tmp_path / "operations.sqlite3"
    snapshot = tmp_path / "operations_snapshot.sqlite3"
    store = OperationsStore(database, snapshot)
    observed_at = "2026-08-12T16:00:00+00:00"
    store.initialize()
    store.sync_job_definitions(registry.list(), observed_at=observed_at)
    store.upsert_runs([OperationRun(
        run_id="run_1",
        source_run_id="source_1",
        job_id="core_market_data",
        status=JobStatus.SUCCESS,
        source="test",
        observed_at=observed_at,
        target_session="2026-08-12",
        started_at="2026-08-12T15:00:00+00:00",
        completed_at="2026-08-12T15:01:00+00:00",
        duration_seconds=60.0,
        output_versions={"SP500": "version-1"},
        stages=(RunStage(
            stage_name="SP500",
            stage_order=1,
            status=JobStatus.SUCCESS,
            rows_processed=100,
        ),),
    )])
    store.upsert_snapshots([JobSnapshot(
        job_id="core_market_data",
        status=JobStatus.SUCCESS,
        observed_at=observed_at,
        run_id="run_1",
        target_session="2026-08-12",
        metrics={"universes_current": 3},
    )])
    store.upsert_freshness([FreshnessObservation(
        object_id="market:SP500",
        display_name="SP500 行情",
        category="MARKET_DATA",
        status=JobStatus.SUCCESS,
        observed_at=observed_at,
        expected_session="2026-08-12",
        actual_session="2026-08-12",
    )])
    store.upsert_deliveries([DeliveryObservation(
        delivery_id="delivery_1",
        job_id="premarket_digest",
        channel="momentum",
        status="SENT",
        observed_at=observed_at,
        run_id="run_1",
    )])
    store.upsert_projects([ProjectObservation(
        project_id="project_1",
        display_name="专项",
        status=JobStatus.RUNNING,
        observed_at=observed_at,
        summary="观察中",
    )])
    count = store.reconcile_incidents([IncidentCandidate(
        fingerprint="test:incident",
        severity=IncidentSeverity.WARNING,
        code="TEST_WARNING",
        title="测试异常",
        detail="用于核验异常恢复",
    )], observed_at=observed_at)
    assert count == 1
    store.publish_snapshot()

    reader = OperationsReader(snapshot)
    overview = reader.overview()
    assert overview["available"] is True
    assert overview["summary"]["jobs_total"] == len(registry.list())
    assert reader.run("run_1")["stages"][0]["rows_processed"] == 100
    assert reader.deliveries()[0]["status"] == "SENT"
    assert reader.projects()[0]["summary"] == "观察中"

    store.reconcile_incidents([], observed_at="2026-08-12T16:01:00+00:00")
    store.publish_snapshot()
    resolved = OperationsReader(snapshot).incidents(include_resolved=True)
    assert resolved[0]["status"] == "RESOLVED"


def test_operations_snapshot_remains_coherent_after_source_changes(tmp_path: Path):
    registry = OperationsRegistry("configs/operations.yaml")
    store = OperationsStore(
        tmp_path / "operations.sqlite3",
        tmp_path / "snapshot.sqlite3",
    )
    store.initialize()
    store.sync_job_definitions(
        registry.list(),
        observed_at="2026-08-12T16:00:00+00:00",
    )
    store.publish_snapshot()
    reader = OperationsReader(tmp_path / "snapshot.sqlite3")
    before = reader.overview()
    store.upsert_snapshots([JobSnapshot(
        job_id="core_market_data",
        status=JobStatus.FAILED,
        observed_at="2026-08-12T16:01:00+00:00",
    )])
    assert reader.overview() == before
    store.publish_snapshot()
    assert reader.overview()["summary"]["status_counts"]["FAILED"] == 1


def test_sqlite_read_only_sandbox_uses_stable_immutable_fallback(
    monkeypatch,
    tmp_path: Path,
):
    path = tmp_path / "source.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE evidence (value TEXT)")
        connection.execute("INSERT INTO evidence VALUES ('ready')")
    real_connect = sqlite3.connect

    def sandboxed_connect(database, *args, **kwargs):
        if "immutable=1" not in str(database):
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr("src.operations.evidence.sqlite3.connect", sandboxed_connect)
    assert sqlite_rows(path, "SELECT value FROM evidence") == [{"value": "ready"}]


def test_sqlite_immutable_fallback_rejects_active_wal(
    monkeypatch,
    tmp_path: Path,
):
    path = tmp_path / "source.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE evidence (value TEXT)")
    Path(str(path) + "-wal").write_bytes(b"active-transaction")

    def sandboxed_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr("src.operations.evidence.sqlite3.connect", sandboxed_connect)
    try:
        sqlite_rows(path, "SELECT value FROM evidence")
    except sqlite3.OperationalError as exc:
        assert "active transaction log" in str(exc)
    else:
        raise AssertionError("active WAL must reject immutable evidence reads")
