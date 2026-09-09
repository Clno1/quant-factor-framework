"""SQLite persistence for mutable application state.

The market-data catalog is intentionally read-mostly and stays in DuckDB.
Strategies, watchlists, jobs, paper accounts, and data requests are mutable
OLTP-style records, so they share this small SQLite database.  Large analytical
artifacts remain Parquet files and are referenced by their owning records.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator, Mapping
from uuid import uuid4

import pandas as pd

from src.config import CONFIG, PROJECT_ROOT


APP_DB_SCHEMA_VERSION = 1
DATA_REQUEST_PENDING = "pending"
DATA_REQUEST_RUNNING = "running"
DATA_REQUEST_SUCCESS = "success"
DATA_REQUEST_FAILED = "failed"
DATA_REQUEST_STATUSES = {
    DATA_REQUEST_PENDING,
    DATA_REQUEST_RUNNING,
    DATA_REQUEST_SUCCESS,
    DATA_REQUEST_FAILED,
}

_DATABASES: dict[str, "AppDatabase"] = {}
_DATABASES_LOCK = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _checksum_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _configured_output_dir() -> Path:
    configured = Path(str(CONFIG.webapp.output_dir))
    return configured if configured.is_absolute() else PROJECT_ROOT / configured


def configured_app_db_path(*, output_dir: Path | None = None) -> Path:
    """Resolve the application DB, keeping monkeypatched test roots isolated."""
    env_path = str(os.environ.get("QUANT_APP_DB_PATH") or "").strip()
    if env_path:
        return Path(env_path).expanduser()

    configured_output = _configured_output_dir()
    selected_output = Path(output_dir) if output_dir is not None else configured_output
    try:
        is_default_output = selected_output.resolve() == configured_output.resolve()
    except OSError:
        is_default_output = selected_output == configured_output
    if not is_default_output:
        return selected_output / "quant_app.sqlite3"

    raw = Path(str(CONFIG.storage.sqlite_path))
    return raw if raw.is_absolute() else PROJECT_ROOT / raw


@dataclass(frozen=True)
class DataRequest:
    request_id: str
    request_key: str
    data_universe: str
    status: str
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    attempts: int
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "DataRequest":
        return cls(
            request_id=str(row["request_id"]),
            request_key=str(row["request_key"]),
            data_universe=str(row["data_universe"]),
            status=str(row["status"]),
            payload=json.loads(row["payload_json"]),
            result=(
                json.loads(row["result_json"])
                if row["result_json"] is not None
                else None
            ),
            error=str(row["error"]) if row["error"] is not None else None,
            attempts=int(row["attempts"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            started_at=(
                str(row["started_at"]) if row["started_at"] is not None else None
            ),
            finished_at=(
                str(row["finished_at"]) if row["finished_at"] is not None else None
            ),
        )


class AppDatabase:
    """Small transactional repository shared by Web and worker processes."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._initialize_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        if self._initialized and self.path.exists():
            return
        with self._initialize_lock:
            if self._initialized and self.path.exists():
                return
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS app_records (
                        kind TEXT NOT NULL,
                        record_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        summary_json TEXT NOT NULL,
                        checksum_sha256 TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (kind, record_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_app_records_kind_updated
                    ON app_records(kind, updated_at DESC, created_at DESC);

                    CREATE TABLE IF NOT EXISTS app_frame_rows (
                        owner_kind TEXT NOT NULL,
                        owner_id TEXT NOT NULL,
                        frame_name TEXT NOT NULL,
                        ordinal INTEGER NOT NULL,
                        row_json TEXT NOT NULL,
                        PRIMARY KEY (owner_kind, owner_id, frame_name, ordinal)
                    );
                    CREATE TABLE IF NOT EXISTS app_frames (
                        owner_kind TEXT NOT NULL,
                        owner_id TEXT NOT NULL,
                        frame_name TEXT NOT NULL,
                        columns_json TEXT NOT NULL,
                        row_count INTEGER NOT NULL,
                        checksum_sha256 TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (owner_kind, owner_id, frame_name)
                    );

                    CREATE TABLE IF NOT EXISTS data_requests (
                        request_id TEXT PRIMARY KEY,
                        request_key TEXT NOT NULL UNIQUE,
                        data_universe TEXT NOT NULL,
                        status TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        result_json TEXT,
                        error TEXT,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_data_requests_status_created
                    ON data_requests(status, created_at);

                    CREATE TABLE IF NOT EXISTS data_request_consumers (
                        request_id TEXT NOT NULL,
                        consumer_kind TEXT NOT NULL,
                        consumer_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (request_id, consumer_kind, consumer_id),
                        FOREIGN KEY (request_id) REFERENCES data_requests(request_id)
                            ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_data_request_consumers_owner
                    ON data_request_consumers(consumer_kind, consumer_id);
                    """
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (?, ?)
                    """,
                    [APP_DB_SCHEMA_VERSION, _utc_now_iso()],
                )
            finally:
                connection.close()
            self._initialized = True

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            finally:
                connection.close()
            raise
        else:
            connection.close()

    def put_record(
        self,
        kind: str,
        record_id: str,
        payload: Mapping[str, Any],
        summary: Mapping[str, Any],
        *,
        create_only: bool = False,
    ) -> None:
        now = _utc_now_iso()
        payload_dict = dict(payload)
        summary_dict = dict(summary)
        payload_json = _canonical_json(payload_dict)
        summary_json = _canonical_json(summary_dict)
        created_at = str(payload_dict.get("created_at") or now)
        updated_at = str(
            payload_dict.get("updated_at")
            or payload_dict.get("finished_at")
            or created_at
        )
        with self.transaction(immediate=True) as connection:
            if create_only:
                connection.execute(
                    """
                    INSERT INTO app_records
                    (kind, record_id, payload_json, summary_json, checksum_sha256,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        kind,
                        record_id,
                        payload_json,
                        summary_json,
                        _checksum_json(payload_dict),
                        created_at,
                        updated_at,
                    ],
                )
            else:
                connection.execute(
                    """
                    INSERT INTO app_records
                    (kind, record_id, payload_json, summary_json, checksum_sha256,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(kind, record_id) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        summary_json = excluded.summary_json,
                        checksum_sha256 = excluded.checksum_sha256,
                        updated_at = excluded.updated_at
                    """,
                    [
                        kind,
                        record_id,
                        payload_json,
                        summary_json,
                        _checksum_json(payload_dict),
                        created_at,
                        updated_at,
                    ],
                )

    def get_record(self, kind: str, record_id: str) -> dict[str, Any] | None:
        self.initialize()
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT payload_json, checksum_sha256
                FROM app_records
                WHERE kind = ? AND record_id = ?
                """,
                [kind, record_id],
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        if _checksum_json(payload) != row["checksum_sha256"]:
            raise ValueError(f"SQLite record checksum mismatch: {kind}/{record_id}")
        return payload

    def list_summaries(self, kind: str) -> list[dict[str, Any]]:
        self.initialize()
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT summary_json
                FROM app_records
                WHERE kind = ?
                ORDER BY updated_at DESC, created_at DESC, record_id DESC
                """,
                [kind],
            ).fetchall()
        finally:
            connection.close()
        return [json.loads(row["summary_json"]) for row in rows]

    def list_records(self, kind: str) -> list[dict[str, Any]]:
        self.initialize()
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT payload_json, checksum_sha256
                FROM app_records
                WHERE kind = ?
                ORDER BY updated_at DESC, created_at DESC, record_id DESC
                """,
                [kind],
            ).fetchall()
        finally:
            connection.close()
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if _checksum_json(payload) != row["checksum_sha256"]:
                raise ValueError(f"SQLite record checksum mismatch in kind={kind}")
            out.append(payload)
        return out

    def delete_record(self, kind: str, record_id: str) -> bool:
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM app_records WHERE kind = ? AND record_id = ?",
                [kind, record_id],
            )
            connection.execute(
                "DELETE FROM app_frame_rows WHERE owner_kind = ? AND owner_id = ?",
                [kind, record_id],
            )
            connection.execute(
                "DELETE FROM app_frames WHERE owner_kind = ? AND owner_id = ?",
                [kind, record_id],
            )
        return bool(cursor.rowcount)

    def put_frame(
        self,
        owner_kind: str,
        owner_id: str,
        frame_name: str,
        frame: pd.DataFrame,
    ) -> None:
        columns = [str(column) for column in frame.columns]
        records = json.loads(
            frame.reset_index(drop=True).to_json(
                orient="records",
                date_format="iso",
                date_unit="ns",
                double_precision=15,
            )
        )
        payload = {"columns": columns, "records": records}
        checksum = _checksum_json(payload)
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                DELETE FROM app_frame_rows
                WHERE owner_kind = ? AND owner_id = ? AND frame_name = ?
                """,
                [owner_kind, owner_id, frame_name],
            )
            connection.executemany(
                """
                INSERT INTO app_frame_rows
                (owner_kind, owner_id, frame_name, ordinal, row_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    [
                        owner_kind,
                        owner_id,
                        frame_name,
                        ordinal,
                        _canonical_json(row),
                    ]
                    for ordinal, row in enumerate(records)
                ],
            )
            connection.execute(
                """
                INSERT INTO app_frames
                (owner_kind, owner_id, frame_name, columns_json, row_count,
                 checksum_sha256, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_kind, owner_id, frame_name) DO UPDATE SET
                    columns_json = excluded.columns_json,
                    row_count = excluded.row_count,
                    checksum_sha256 = excluded.checksum_sha256,
                    updated_at = excluded.updated_at
                """,
                [
                    owner_kind,
                    owner_id,
                    frame_name,
                    _canonical_json(columns),
                    len(records),
                    checksum,
                    _utc_now_iso(),
                ],
            )

    def get_frame(
        self,
        owner_kind: str,
        owner_id: str,
        frame_name: str,
    ) -> pd.DataFrame | None:
        self.initialize()
        connection = self._connect()
        try:
            # Metadata and rows must be read from one WAL snapshot even when
            # another connection commits a complete replacement between SELECTs.
            connection.execute("BEGIN")
            meta = connection.execute(
                """
                SELECT columns_json, row_count, checksum_sha256
                FROM app_frames
                WHERE owner_kind = ? AND owner_id = ? AND frame_name = ?
                """,
                [owner_kind, owner_id, frame_name],
            ).fetchone()
            if meta is None:
                return None
            rows = connection.execute(
                """
                SELECT row_json
                FROM app_frame_rows
                WHERE owner_kind = ? AND owner_id = ? AND frame_name = ?
                ORDER BY ordinal
                """,
                [owner_kind, owner_id, frame_name],
            ).fetchall()
        finally:
            connection.close()
        columns = json.loads(meta["columns_json"])
        records = [json.loads(row["row_json"]) for row in rows]
        payload = {"columns": columns, "records": records}
        if len(records) != int(meta["row_count"]):
            raise ValueError(
                f"SQLite frame row count mismatch: "
                f"{owner_kind}/{owner_id}/{frame_name}"
            )
        if _checksum_json(payload) != meta["checksum_sha256"]:
            raise ValueError(
                f"SQLite frame checksum mismatch: "
                f"{owner_kind}/{owner_id}/{frame_name}"
            )
        return pd.DataFrame.from_records(records, columns=columns)

    def verify_integrity(self) -> dict[str, Any]:
        """Verify SQLite structure plus every application checksum."""
        self.initialize()
        issues: list[str] = []
        record_counts: dict[str, int] = {}
        frames: list[tuple[str, str, str]] = []
        connection = self._connect()
        try:
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            integrity = [str(row[0]) for row in integrity_rows]
            if integrity != ["ok"]:
                issues.extend(f"sqlite_integrity:{value}" for value in integrity)

            records = connection.execute(
                """
                SELECT kind, record_id, payload_json, summary_json,
                       checksum_sha256
                FROM app_records
                ORDER BY kind, record_id
                """
            ).fetchall()
            for row in records:
                kind = str(row["kind"])
                record_id = str(row["record_id"])
                record_counts[kind] = record_counts.get(kind, 0) + 1
                try:
                    payload = json.loads(row["payload_json"])
                    json.loads(row["summary_json"])
                    if _checksum_json(payload) != row["checksum_sha256"]:
                        issues.append(f"record_checksum:{kind}/{record_id}")
                except (TypeError, json.JSONDecodeError):
                    issues.append(f"record_json:{kind}/{record_id}")

            frames = [
                (str(row[0]), str(row[1]), str(row[2]))
                for row in connection.execute(
                    """
                    SELECT owner_kind, owner_id, frame_name
                    FROM app_frames
                    ORDER BY owner_kind, owner_id, frame_name
                    """
                ).fetchall()
            ]
            orphan_rows = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM app_frame_rows AS row
                    LEFT JOIN app_frames AS frame
                      ON frame.owner_kind = row.owner_kind
                     AND frame.owner_id = row.owner_id
                     AND frame.frame_name = row.frame_name
                    WHERE frame.owner_kind IS NULL
                    """
                ).fetchone()[0]
            )
            if orphan_rows:
                issues.append(f"orphan_frame_rows:{orphan_rows}")

            request_count = int(
                connection.execute("SELECT COUNT(*) FROM data_requests").fetchone()[0]
            )
            invalid_request_statuses = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM data_requests
                    WHERE status NOT IN (?, ?, ?, ?)
                    """,
                    sorted(DATA_REQUEST_STATUSES),
                ).fetchone()[0]
            )
            if invalid_request_statuses:
                issues.append(
                    f"invalid_data_request_statuses:{invalid_request_statuses}"
                )
        finally:
            connection.close()

        for owner_kind, owner_id, frame_name in frames:
            try:
                self.get_frame(owner_kind, owner_id, frame_name)
            except Exception as exc:  # noqa: BLE001
                issues.append(
                    f"frame:{owner_kind}/{owner_id}/{frame_name}:"
                    f"{type(exc).__name__}"
                )
        return {
            "path": str(self.path),
            "sqlite_integrity": integrity,
            "records": record_counts,
            "frames": len(frames),
            "data_requests": request_count,
            "issues": issues,
            "passed": not issues,
        }

    def enqueue_data_request(
        self,
        *,
        data_universe: str,
        payload: Mapping[str, Any],
        consumer_kind: str,
        consumer_id: str,
    ) -> DataRequest:
        payload_dict = dict(payload)
        request_key = _checksum_json(
            {"data_universe": data_universe, "payload": payload_dict}
        )
        now = _utc_now_iso()
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM data_requests WHERE request_key = ?",
                [request_key],
            ).fetchone()
            if row is None:
                request_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO data_requests
                    (request_id, request_key, data_universe, status, payload_json,
                     attempts, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    [
                        request_id,
                        request_key,
                        data_universe,
                        DATA_REQUEST_PENDING,
                        _canonical_json(payload_dict),
                        now,
                        now,
                    ],
                )
            else:
                request_id = str(row["request_id"])
                if (
                    row["status"] == DATA_REQUEST_FAILED
                    and int(row["attempts"]) < 3
                ):
                    connection.execute(
                        """
                        UPDATE data_requests
                        SET status = ?, result_json = NULL, error = NULL,
                            updated_at = ?, started_at = NULL, finished_at = NULL
                        WHERE request_id = ?
                        """,
                        [DATA_REQUEST_PENDING, now, request_id],
                    )
            connection.execute(
                """
                INSERT OR IGNORE INTO data_request_consumers
                (request_id, consumer_kind, consumer_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                [request_id, consumer_kind, consumer_id, now],
            )
            result = connection.execute(
                "SELECT * FROM data_requests WHERE request_id = ?",
                [request_id],
            ).fetchone()
        assert result is not None
        return DataRequest.from_row(result)

    def claim_data_requests(self, *, limit: int = 10) -> list[DataRequest]:
        now = _utc_now_iso()
        with self.transaction(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM data_requests
                WHERE status = ?
                ORDER BY created_at
                LIMIT ?
                """,
                [DATA_REQUEST_PENDING, max(1, int(limit))],
            ).fetchall()
            request_ids = [str(row["request_id"]) for row in rows]
            if request_ids:
                connection.executemany(
                    """
                    UPDATE data_requests
                    SET status = ?, attempts = attempts + 1,
                        started_at = ?, updated_at = ?
                    WHERE request_id = ? AND status = ?
                    """,
                    [
                        [
                            DATA_REQUEST_RUNNING,
                            now,
                            now,
                            request_id,
                            DATA_REQUEST_PENDING,
                        ]
                        for request_id in request_ids
                    ],
                )
                placeholders = ",".join("?" for _ in request_ids)
                rows = connection.execute(
                    f"""
                    SELECT * FROM data_requests
                    WHERE request_id IN ({placeholders})
                      AND status = ?
                    ORDER BY created_at
                    """,
                    [*request_ids, DATA_REQUEST_RUNNING],
                ).fetchall()
        return [DataRequest.from_row(row) for row in rows]

    def recover_stale_data_requests(
        self,
        *,
        stale_after_seconds: int = 1800,
        max_attempts: int = 3,
    ) -> dict[str, int]:
        """Requeue requests abandoned by a crashed worker, with a retry cap."""
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=max(1, int(stale_after_seconds)))
        ).isoformat(timespec="seconds")
        now = _utc_now_iso()
        recovered = 0
        failed = 0
        with self.transaction(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT request_id, attempts
                FROM data_requests
                WHERE status = ?
                  AND COALESCE(started_at, updated_at) <= ?
                """,
                [DATA_REQUEST_RUNNING, cutoff],
            ).fetchall()
            for row in rows:
                request_id = str(row["request_id"])
                attempts = int(row["attempts"])
                if attempts >= max(1, int(max_attempts)):
                    connection.execute(
                        """
                        UPDATE data_requests
                        SET status = ?, error = ?, updated_at = ?,
                            finished_at = ?
                        WHERE request_id = ? AND status = ?
                        """,
                        [
                            DATA_REQUEST_FAILED,
                            "Data request worker timed out after "
                            f"{attempts} attempts",
                            now,
                            now,
                            request_id,
                            DATA_REQUEST_RUNNING,
                        ],
                    )
                    failed += 1
                else:
                    connection.execute(
                        """
                        UPDATE data_requests
                        SET status = ?, error = ?, updated_at = ?,
                            started_at = NULL, finished_at = NULL
                        WHERE request_id = ? AND status = ?
                        """,
                        [
                            DATA_REQUEST_PENDING,
                            "Previous data request worker timed out; requeued",
                            now,
                            request_id,
                            DATA_REQUEST_RUNNING,
                        ],
                    )
                    recovered += 1
        return {"requeued": recovered, "failed": failed}

    def finish_data_request(
        self,
        request_id: str,
        *,
        status: str,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if status not in {DATA_REQUEST_SUCCESS, DATA_REQUEST_FAILED}:
            raise ValueError(f"Invalid terminal data request status: {status}")
        now = _utc_now_iso()
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE data_requests
                SET status = ?, result_json = ?, error = ?,
                    updated_at = ?, finished_at = ?
                WHERE request_id = ?
                """,
                [
                    status,
                    _canonical_json(dict(result or {})) if result is not None else None,
                    error,
                    now,
                    now,
                    request_id,
                ],
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown data request: {request_id}")

    def retry_or_fail_data_request(
        self,
        request_id: str,
        *,
        error: str,
        max_attempts: int = 3,
    ) -> str:
        """Requeue a failed attempt, or make it terminal at the retry limit."""
        now = _utc_now_iso()
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT attempts FROM data_requests WHERE request_id = ?",
                [request_id],
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown data request: {request_id}")
            attempts = int(row["attempts"])
            terminal = attempts >= max(1, int(max_attempts))
            status = DATA_REQUEST_FAILED if terminal else DATA_REQUEST_PENDING
            connection.execute(
                """
                UPDATE data_requests
                SET status = ?, error = ?, result_json = NULL,
                    updated_at = ?, finished_at = ?, started_at = NULL
                WHERE request_id = ?
                """,
                [
                    status,
                    error,
                    now,
                    now if terminal else None,
                    request_id,
                ],
            )
        return status

    def get_data_request(self, request_id: str) -> DataRequest | None:
        self.initialize()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM data_requests WHERE request_id = ?",
                [request_id],
            ).fetchone()
        finally:
            connection.close()
        return DataRequest.from_row(row) if row is not None else None

    def data_requests_for_consumer(
        self,
        consumer_kind: str,
        consumer_id: str,
    ) -> list[DataRequest]:
        self.initialize()
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT request.*
                FROM data_requests AS request
                INNER JOIN data_request_consumers AS consumer
                    ON consumer.request_id = request.request_id
                WHERE consumer.consumer_kind = ? AND consumer.consumer_id = ?
                ORDER BY request.created_at
                """,
                [consumer_kind, consumer_id],
            ).fetchall()
        finally:
            connection.close()
        return [DataRequest.from_row(row) for row in rows]


def app_database(*, output_dir: Path | None = None) -> AppDatabase:
    path = configured_app_db_path(output_dir=output_dir)
    key = str(path.resolve())
    with _DATABASES_LOCK:
        database = _DATABASES.get(key)
        if database is None:
            database = AppDatabase(path)
            _DATABASES[key] = database
    return database


__all__ = [
    "APP_DB_SCHEMA_VERSION",
    "DATA_REQUEST_FAILED",
    "DATA_REQUEST_PENDING",
    "DATA_REQUEST_RUNNING",
    "DATA_REQUEST_STATUSES",
    "DATA_REQUEST_SUCCESS",
    "AppDatabase",
    "DataRequest",
    "app_database",
    "configured_app_db_path",
]
