"""Transactional operations ledger plus an atomic read-only Web snapshot."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterable, Iterator
from urllib.parse import quote

from src.operations.models import (
    DeliveryObservation,
    FreshnessObservation,
    IncidentCandidate,
    JobDefinition,
    JobSnapshot,
    OperationRun,
    ProjectObservation,
)


SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _load_json(value: str | None, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _incident_id(fingerprint: str) -> str:
    return "incident_" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]


class OperationsStore:
    """Single-writer SQLite ledger used by the watchdog and instrumented jobs."""

    def __init__(self, path: str | Path, snapshot_path: str | Path):
        self.path = Path(path).resolve()
        self.snapshot_path = Path(snapshot_path).resolve()
        self._lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        if self._initialized and self.path.exists():
            return
        with self._lock:
            if self._initialized and self.path.exists():
                return
            db = self._connect()
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS job_definitions (
                        job_id TEXT PRIMARY KEY,
                        display_name TEXT NOT NULL,
                        category TEXT NOT NULL,
                        run_type TEXT NOT NULL,
                        adapter TEXT NOT NULL,
                        display_order INTEGER NOT NULL,
                        enabled_expected INTEGER NOT NULL,
                        service_unit TEXT,
                        timer_unit TEXT,
                        schedule_json TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        description TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS job_runs (
                        run_id TEXT PRIMARY KEY,
                        source_run_id TEXT NOT NULL,
                        job_id TEXT NOT NULL,
                        target_session TEXT,
                        attempt INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        stage TEXT,
                        source TEXT NOT NULL,
                        started_at TEXT,
                        heartbeat_at TEXT,
                        completed_at TEXT,
                        duration_seconds REAL,
                        progress_current REAL,
                        progress_total REAL,
                        rows_processed INTEGER,
                        delivery_status TEXT,
                        error_code TEXT,
                        error_summary TEXT,
                        input_versions_json TEXT NOT NULL,
                        output_versions_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        FOREIGN KEY(job_id) REFERENCES job_definitions(job_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_job_runs_job_started
                    ON job_runs(job_id, started_at DESC, observed_at DESC);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_job_runs_source
                    ON job_runs(job_id, source, source_run_id);

                    CREATE TABLE IF NOT EXISTS job_stages (
                        run_id TEXT NOT NULL,
                        stage_name TEXT NOT NULL,
                        stage_order INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        started_at TEXT,
                        completed_at TEXT,
                        duration_seconds REAL,
                        progress_current REAL,
                        progress_total REAL,
                        rows_processed INTEGER,
                        detail TEXT,
                        metadata_json TEXT NOT NULL,
                        PRIMARY KEY(run_id, stage_name),
                        FOREIGN KEY(run_id) REFERENCES job_runs(run_id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS job_snapshots (
                        job_id TEXT PRIMARY KEY,
                        run_id TEXT,
                        target_session TEXT,
                        status TEXT NOT NULL,
                        stage TEXT,
                        status_reason TEXT,
                        scheduled_for TEXT,
                        deadline_at TEXT,
                        last_success_at TEXT,
                        heartbeat_at TEXT,
                        progress_current REAL,
                        progress_total REAL,
                        output_version TEXT,
                        systemd_json TEXT NOT NULL,
                        metrics_json TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        FOREIGN KEY(job_id) REFERENCES job_definitions(job_id)
                    );

                    CREATE TABLE IF NOT EXISTS freshness_snapshots (
                        object_id TEXT PRIMARY KEY,
                        display_name TEXT NOT NULL,
                        category TEXT NOT NULL,
                        status TEXT NOT NULL,
                        expected_session TEXT,
                        actual_session TEXT,
                        delay_sessions INTEGER,
                        version_id TEXT,
                        row_count INTEGER,
                        item_count INTEGER,
                        quality_json TEXT NOT NULL,
                        source TEXT,
                        observed_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS delivery_attempts (
                        delivery_id TEXT PRIMARY KEY,
                        run_id TEXT,
                        job_id TEXT NOT NULL,
                        target_session TEXT,
                        channel TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempts INTEGER NOT NULL,
                        sent_at TEXT,
                        message_id TEXT,
                        error_code TEXT,
                        error_summary TEXT,
                        metadata_json TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        FOREIGN KEY(job_id) REFERENCES job_definitions(job_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_delivery_recent
                    ON delivery_attempts(observed_at DESC, sent_at DESC);

                    CREATE TABLE IF NOT EXISTS project_snapshots (
                        project_id TEXT PRIMARY KEY,
                        display_name TEXT NOT NULL,
                        status TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        stages_json TEXT NOT NULL,
                        blockers_json TEXT NOT NULL,
                        metrics_json TEXT NOT NULL,
                        observed_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS incidents (
                        incident_id TEXT PRIMARY KEY,
                        fingerprint TEXT NOT NULL UNIQUE,
                        severity TEXT NOT NULL,
                        status TEXT NOT NULL,
                        code TEXT NOT NULL,
                        title TEXT NOT NULL,
                        detail TEXT NOT NULL,
                        job_id TEXT,
                        target_session TEXT,
                        run_id TEXT,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        resolved_at TEXT,
                        metadata_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_incidents_status_severity
                    ON incidents(status, severity, last_seen_at DESC);

                    CREATE TABLE IF NOT EXISTS watchdog_runs (
                        watchdog_run_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        duration_seconds REAL NOT NULL,
                        jobs_observed INTEGER NOT NULL,
                        incidents_open INTEGER NOT NULL,
                        error_summary TEXT,
                        metadata_json TEXT NOT NULL
                    );
                """)
                db.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES (?, ?)",
                    [SCHEMA_VERSION, utc_now_iso()],
                )
            finally:
                db.close()
            self._initialized = True

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.initialize()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except Exception:
            try:
                db.execute("ROLLBACK")
            finally:
                db.close()
            raise
        else:
            db.close()

    def sync_job_definitions(
        self,
        jobs: Iterable[JobDefinition],
        *,
        observed_at: str,
    ) -> None:
        with self.transaction() as db:
            for job in jobs:
                db.execute("""
                    INSERT INTO job_definitions VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT(job_id) DO UPDATE SET
                        display_name=excluded.display_name,
                        category=excluded.category,
                        run_type=excluded.run_type,
                        adapter=excluded.adapter,
                        display_order=excluded.display_order,
                        enabled_expected=excluded.enabled_expected,
                        service_unit=excluded.service_unit,
                        timer_unit=excluded.timer_unit,
                        schedule_json=excluded.schedule_json,
                        evidence_json=excluded.evidence_json,
                        description=excluded.description,
                        updated_at=excluded.updated_at
                """, [
                    job.job_id,
                    job.display_name,
                    job.category,
                    job.run_type,
                    job.adapter,
                    job.order,
                    int(job.enabled_expected),
                    job.service_unit,
                    job.timer_unit,
                    _json(job.schedule),
                    _json(job.evidence),
                    job.description,
                    observed_at,
                ])

    def upsert_runs(self, runs: Iterable[OperationRun]) -> None:
        with self.transaction() as db:
            for run in runs:
                db.execute("""
                    INSERT INTO job_runs VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?
                    )
                    ON CONFLICT(run_id) DO UPDATE SET
                        target_session=excluded.target_session,
                        attempt=excluded.attempt,
                        status=excluded.status,
                        stage=excluded.stage,
                        started_at=excluded.started_at,
                        heartbeat_at=excluded.heartbeat_at,
                        completed_at=excluded.completed_at,
                        duration_seconds=excluded.duration_seconds,
                        progress_current=excluded.progress_current,
                        progress_total=excluded.progress_total,
                        rows_processed=excluded.rows_processed,
                        delivery_status=excluded.delivery_status,
                        error_code=excluded.error_code,
                        error_summary=excluded.error_summary,
                        input_versions_json=excluded.input_versions_json,
                        output_versions_json=excluded.output_versions_json,
                        metadata_json=excluded.metadata_json,
                        observed_at=excluded.observed_at
                """, [
                    run.run_id,
                    run.source_run_id,
                    run.job_id,
                    run.target_session,
                    run.attempt,
                    run.status.value,
                    run.stage,
                    run.source,
                    run.started_at,
                    run.heartbeat_at,
                    run.completed_at,
                    run.duration_seconds,
                    run.progress_current,
                    run.progress_total,
                    run.rows_processed,
                    run.delivery_status,
                    run.error_code,
                    run.error_summary,
                    _json(run.input_versions),
                    _json(run.output_versions),
                    _json(run.metadata),
                    run.observed_at,
                ])
                for stage in run.stages:
                    db.execute("""
                        INSERT INTO job_stages VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        ON CONFLICT(run_id, stage_name) DO UPDATE SET
                            stage_order=excluded.stage_order,
                            status=excluded.status,
                            started_at=excluded.started_at,
                            completed_at=excluded.completed_at,
                            duration_seconds=excluded.duration_seconds,
                            progress_current=excluded.progress_current,
                            progress_total=excluded.progress_total,
                            rows_processed=excluded.rows_processed,
                            detail=excluded.detail,
                            metadata_json=excluded.metadata_json
                    """, [
                        run.run_id,
                        stage.stage_name,
                        stage.stage_order,
                        stage.status.value,
                        stage.started_at,
                        stage.completed_at,
                        stage.duration_seconds,
                        stage.progress_current,
                        stage.progress_total,
                        stage.rows_processed,
                        stage.detail,
                        _json(stage.metadata),
                    ])

    def upsert_snapshots(self, snapshots: Iterable[JobSnapshot]) -> None:
        with self.transaction() as db:
            for item in snapshots:
                db.execute("""
                    INSERT INTO job_snapshots VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT(job_id) DO UPDATE SET
                        run_id=excluded.run_id,
                        target_session=excluded.target_session,
                        status=excluded.status,
                        stage=excluded.stage,
                        status_reason=excluded.status_reason,
                        scheduled_for=excluded.scheduled_for,
                        deadline_at=excluded.deadline_at,
                        last_success_at=excluded.last_success_at,
                        heartbeat_at=excluded.heartbeat_at,
                        progress_current=excluded.progress_current,
                        progress_total=excluded.progress_total,
                        output_version=excluded.output_version,
                        systemd_json=excluded.systemd_json,
                        metrics_json=excluded.metrics_json,
                        observed_at=excluded.observed_at
                """, [
                    item.job_id,
                    item.run_id,
                    item.target_session,
                    item.status.value,
                    item.stage,
                    item.status_reason,
                    item.scheduled_for,
                    item.deadline_at,
                    item.last_success_at,
                    item.heartbeat_at,
                    item.progress_current,
                    item.progress_total,
                    item.output_version,
                    _json(item.systemd),
                    _json(item.metrics),
                    item.observed_at,
                ])

    def upsert_freshness(self, rows: Iterable[FreshnessObservation]) -> None:
        with self.transaction() as db:
            for item in rows:
                db.execute("""
                    INSERT INTO freshness_snapshots VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT(object_id) DO UPDATE SET
                        display_name=excluded.display_name,
                        category=excluded.category,
                        status=excluded.status,
                        expected_session=excluded.expected_session,
                        actual_session=excluded.actual_session,
                        delay_sessions=excluded.delay_sessions,
                        version_id=excluded.version_id,
                        row_count=excluded.row_count,
                        item_count=excluded.item_count,
                        quality_json=excluded.quality_json,
                        source=excluded.source,
                        observed_at=excluded.observed_at
                """, [
                    item.object_id,
                    item.display_name,
                    item.category,
                    item.status.value,
                    item.expected_session,
                    item.actual_session,
                    item.delay_sessions,
                    item.version_id,
                    item.row_count,
                    item.item_count,
                    _json(item.quality),
                    item.source,
                    item.observed_at,
                ])

    def upsert_deliveries(self, rows: Iterable[DeliveryObservation]) -> None:
        with self.transaction() as db:
            for item in rows:
                db.execute("""
                    INSERT INTO delivery_attempts VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT(delivery_id) DO UPDATE SET
                        run_id=excluded.run_id,
                        job_id=excluded.job_id,
                        target_session=excluded.target_session,
                        channel=excluded.channel,
                        status=excluded.status,
                        attempts=excluded.attempts,
                        sent_at=excluded.sent_at,
                        message_id=excluded.message_id,
                        error_code=excluded.error_code,
                        error_summary=excluded.error_summary,
                        metadata_json=excluded.metadata_json,
                        observed_at=excluded.observed_at
                """, [
                    item.delivery_id,
                    item.run_id,
                    item.job_id,
                    item.target_session,
                    item.channel,
                    item.status,
                    item.attempts,
                    item.sent_at,
                    item.message_id,
                    item.error_code,
                    item.error_summary,
                    _json(item.metadata),
                    item.observed_at,
                ])

    def upsert_projects(self, rows: Iterable[ProjectObservation]) -> None:
        with self.transaction() as db:
            for item in rows:
                db.execute("""
                    INSERT INTO project_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_id) DO UPDATE SET
                        display_name=excluded.display_name,
                        status=excluded.status,
                        summary=excluded.summary,
                        stages_json=excluded.stages_json,
                        blockers_json=excluded.blockers_json,
                        metrics_json=excluded.metrics_json,
                        observed_at=excluded.observed_at
                """, [
                    item.project_id,
                    item.display_name,
                    item.status.value,
                    item.summary,
                    _json(list(item.stages)),
                    _json(list(item.blockers)),
                    _json(item.metrics),
                    item.observed_at,
                ])

    def reconcile_incidents(
        self,
        candidates: Iterable[IncidentCandidate],
        *,
        observed_at: str,
        resolve_missing: bool = True,
    ) -> int:
        values = list(candidates)
        active = {item.fingerprint for item in values}
        with self.transaction() as db:
            for item in values:
                db.execute("""
                    INSERT INTO incidents VALUES (
                        ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?
                    )
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        severity=excluded.severity,
                        status='OPEN',
                        code=excluded.code,
                        title=excluded.title,
                        detail=excluded.detail,
                        job_id=excluded.job_id,
                        target_session=excluded.target_session,
                        run_id=excluded.run_id,
                        last_seen_at=excluded.last_seen_at,
                        resolved_at=NULL,
                        metadata_json=excluded.metadata_json
                """, [
                    _incident_id(item.fingerprint),
                    item.fingerprint,
                    item.severity.value,
                    item.code,
                    item.title,
                    item.detail,
                    item.job_id,
                    item.target_session,
                    item.run_id,
                    observed_at,
                    observed_at,
                    _json(item.metadata),
                ])
            open_rows = db.execute(
                "SELECT fingerprint FROM incidents WHERE status IN ('OPEN','ACKNOWLEDGED')"
            ).fetchall()
            if resolve_missing:
                for row in open_rows:
                    fingerprint = str(row["fingerprint"])
                    if fingerprint not in active:
                        db.execute("""
                            UPDATE incidents
                            SET status='RESOLVED', resolved_at=?, last_seen_at=?
                            WHERE fingerprint=?
                        """, [observed_at, observed_at, fingerprint])
            return int(db.execute(
                "SELECT COUNT(*) FROM incidents WHERE status IN ('OPEN','ACKNOWLEDGED')"
            ).fetchone()[0])

    def record_watchdog_run(
        self,
        *,
        watchdog_run_id: str,
        status: str,
        started_at: str,
        completed_at: str,
        duration_seconds: float,
        jobs_observed: int,
        incidents_open: int,
        error_summary: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.transaction() as db:
            db.execute("""
                INSERT OR REPLACE INTO watchdog_runs VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, [
                watchdog_run_id,
                status,
                started_at,
                completed_at,
                duration_seconds,
                jobs_observed,
                incidents_open,
                error_summary,
                _json(metadata or {}),
            ])

    def prune(self, *, retention_days: int) -> None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))
        ).isoformat(timespec="seconds")
        with self.transaction() as db:
            db.execute(
                "DELETE FROM job_runs WHERE observed_at < ?",
                [cutoff],
            )
            db.execute(
                "DELETE FROM delivery_attempts WHERE observed_at < ?",
                [cutoff],
            )
            db.execute(
                "DELETE FROM watchdog_runs WHERE completed_at < ?",
                [cutoff],
            )
            db.execute("""
                DELETE FROM incidents
                WHERE status='RESOLVED' AND resolved_at < ?
            """, [cutoff])

    def publish_snapshot(self) -> Path:
        """Publish a coherent immutable copy for the Web process."""
        self.initialize()
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.snapshot_path.with_suffix(
            self.snapshot_path.suffix + ".tmp"
        )
        temporary.unlink(missing_ok=True)
        source = self._connect()
        destination = sqlite3.connect(temporary)
        try:
            source.execute("PRAGMA wal_checkpoint(PASSIVE)")
            source.backup(destination)
            destination.execute("PRAGMA journal_mode=DELETE")
            destination.commit()
        finally:
            destination.close()
            source.close()
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.snapshot_path)
        return self.snapshot_path

    def integrity_report(self) -> dict[str, Any]:
        self.initialize()
        db = self._connect()
        try:
            integrity = [str(row[0]) for row in db.execute("PRAGMA integrity_check")]
            counts = {
                table: int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "job_definitions",
                    "job_runs",
                    "job_snapshots",
                    "freshness_snapshots",
                    "delivery_attempts",
                    "project_snapshots",
                    "incidents",
                )
            }
        finally:
            db.close()
        return {
            "schema_version": SCHEMA_VERSION,
            "database_path": str(self.path),
            "snapshot_path": str(self.snapshot_path),
            "integrity": integrity,
            "counts": counts,
            "passed": integrity == ["ok"],
        }


class OperationsReader:
    """Read-only access to the watchdog's atomically published snapshot."""

    def __init__(self, snapshot_path: str | Path):
        self.snapshot_path = Path(snapshot_path).resolve()

    def available(self) -> bool:
        return self.snapshot_path.is_file()

    def _connect(self) -> sqlite3.Connection:
        if not self.available():
            raise FileNotFoundError(
                f"operations snapshot does not exist: {self.snapshot_path}"
            )
        uri = "file:" + quote(str(self.snapshot_path)) + "?mode=ro&immutable=1"
        db = sqlite3.connect(uri, uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db

    @staticmethod
    def _definition(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["enabled_expected"] = bool(value["enabled_expected"])
        value["schedule"] = _load_json(value.pop("schedule_json", None), {})
        value["evidence"] = _load_json(value.pop("evidence_json", None), {})
        return value

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["systemd"] = _load_json(value.pop("systemd_json", None), {})
        value["metrics"] = _load_json(value.pop("metrics_json", None), {})
        return value

    @staticmethod
    def _run(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["input_versions"] = _load_json(
            value.pop("input_versions_json", None), {}
        )
        value["output_versions"] = _load_json(
            value.pop("output_versions_json", None), {}
        )
        value["metadata"] = _load_json(value.pop("metadata_json", None), {})
        return value

    def overview(self) -> dict[str, Any]:
        if not self.available():
            return {
                "available": False,
                "snapshot_at": None,
                "jobs": [],
                "freshness": [],
                "projects": [],
                "incidents": [],
                "summary": {},
            }
        db = self._connect()
        try:
            jobs = [self._snapshot(row) for row in db.execute("""
                SELECT d.*, s.run_id, s.target_session, s.status, s.stage,
                       s.status_reason, s.scheduled_for, s.deadline_at,
                       s.last_success_at, s.heartbeat_at, s.progress_current,
                       s.progress_total, s.output_version, s.systemd_json,
                       s.metrics_json, s.observed_at
                FROM job_definitions d
                LEFT JOIN job_snapshots s USING(job_id)
                ORDER BY d.display_order, d.job_id
            """).fetchall()]
            for item in jobs:
                item["enabled_expected"] = bool(item["enabled_expected"])
                item["schedule"] = _load_json(item.pop("schedule_json", None), {})
                item["evidence"] = _load_json(item.pop("evidence_json", None), {})
            freshness = [self._freshness(row) for row in db.execute(
                "SELECT * FROM freshness_snapshots ORDER BY category, display_name"
            ).fetchall()]
            projects = [self._project(row) for row in db.execute(
                "SELECT * FROM project_snapshots ORDER BY display_name"
            ).fetchall()]
            incidents = [self._incident(row) for row in db.execute("""
                SELECT * FROM incidents
                WHERE status IN ('OPEN','ACKNOWLEDGED')
                ORDER BY CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END,
                         last_seen_at DESC
            """).fetchall()]
            snapshot_at = max(
                [str(item.get("observed_at") or "") for item in jobs]
                + [str(item.get("observed_at") or "") for item in freshness]
                + [""],
            ) or None
            counts: dict[str, int] = {}
            for item in jobs:
                status = str(item.get("status") or "UNKNOWN")
                counts[status] = counts.get(status, 0) + 1
            return {
                "available": True,
                "snapshot_at": snapshot_at,
                "jobs": jobs,
                "freshness": freshness,
                "projects": projects,
                "incidents": incidents,
                "summary": {
                    "jobs_total": len(jobs),
                    "status_counts": counts,
                    "open_incidents": len(incidents),
                    "freshness_issues": sum(
                        str(item.get("status")) not in {"SUCCESS", "SKIPPED"}
                        for item in freshness
                    ),
                },
            }
        finally:
            db.close()

    @staticmethod
    def _freshness(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["quality"] = _load_json(value.pop("quality_json", None), {})
        return value

    @staticmethod
    def _project(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["stages"] = _load_json(value.pop("stages_json", None), [])
        value["blockers"] = _load_json(value.pop("blockers_json", None), [])
        value["metrics"] = _load_json(value.pop("metrics_json", None), {})
        return value

    @staticmethod
    def _incident(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["metadata"] = _load_json(value.pop("metadata_json", None), {})
        return value

    @staticmethod
    def _delivery(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["metadata"] = _load_json(value.pop("metadata_json", None), {})
        return value

    def jobs(self) -> list[dict[str, Any]]:
        return self.overview()["jobs"]

    def job(self, job_id: str, *, run_limit: int = 50) -> dict[str, Any] | None:
        if not self.available():
            return None
        db = self._connect()
        try:
            row = db.execute("SELECT * FROM job_definitions WHERE job_id=?", [job_id]).fetchone()
            if row is None:
                return None
            definition = self._definition(row)
            snapshot_row = db.execute(
                "SELECT * FROM job_snapshots WHERE job_id=?", [job_id]
            ).fetchone()
            runs = [self._run(value) for value in db.execute("""
                SELECT * FROM job_runs WHERE job_id=?
                ORDER BY COALESCE(started_at, observed_at) DESC LIMIT ?
            """, [job_id, max(1, int(run_limit))]).fetchall()]
            return {
                "definition": definition,
                "snapshot": self._snapshot(snapshot_row) if snapshot_row else None,
                "runs": runs,
            }
        finally:
            db.close()

    def run(self, run_id: str) -> dict[str, Any] | None:
        if not self.available():
            return None
        db = self._connect()
        try:
            row = db.execute("SELECT * FROM job_runs WHERE run_id=?", [run_id]).fetchone()
            if row is None:
                return None
            run = self._run(row)
            run["stages"] = [
                {
                    **dict(value),
                    "metadata": _load_json(dict(value).get("metadata_json"), {}),
                }
                for value in db.execute("""
                    SELECT * FROM job_stages WHERE run_id=?
                    ORDER BY stage_order, stage_name
                """, [run_id]).fetchall()
            ]
            for stage in run["stages"]:
                stage.pop("metadata_json", None)
            run["deliveries"] = [self._delivery(value) for value in db.execute(
                "SELECT * FROM delivery_attempts WHERE run_id=? ORDER BY channel",
                [run_id],
            ).fetchall()]
            return run
        finally:
            db.close()

    def freshness(self) -> list[dict[str, Any]]:
        if not self.available():
            return []
        db = self._connect()
        try:
            return [self._freshness(row) for row in db.execute(
                "SELECT * FROM freshness_snapshots ORDER BY category, display_name"
            ).fetchall()]
        finally:
            db.close()

    def deliveries(self, *, limit: int = 200) -> list[dict[str, Any]]:
        if not self.available():
            return []
        db = self._connect()
        try:
            return [self._delivery(row) for row in db.execute("""
                SELECT * FROM delivery_attempts
                ORDER BY COALESCE(sent_at, observed_at) DESC LIMIT ?
            """, [max(1, int(limit))]).fetchall()]
        finally:
            db.close()

    def projects(self) -> list[dict[str, Any]]:
        return self.overview()["projects"]

    def incidents(self, *, include_resolved: bool = False) -> list[dict[str, Any]]:
        if not self.available():
            return []
        db = self._connect()
        try:
            clause = "" if include_resolved else "WHERE status IN ('OPEN','ACKNOWLEDGED')"
            return [self._incident(row) for row in db.execute(f"""
                SELECT * FROM incidents {clause}
                ORDER BY CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END,
                         last_seen_at DESC LIMIT 500
            """).fetchall()]
        finally:
            db.close()


__all__ = [
    "OperationsReader",
    "OperationsStore",
    "SCHEMA_VERSION",
    "utc_now_iso",
]
