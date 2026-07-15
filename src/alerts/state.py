"""SQLite state for alert deduplication, delivery retries and run auditing."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator
from uuid import uuid4

from src.utils.io import ensure_dir


SIGNAL_RANK = {
    "CANDIDATE": 1,
    "READY": 2,
    "BREAKOUT": 3,
    "OPENING_RANGE_BREAK": 4,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AlertStateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        ensure_dir(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._session() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS signal_state (
                    session_date TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    max_seen_rank INTEGER NOT NULL DEFAULT 0,
                    max_delivered_rank INTEGER NOT NULL DEFAULT 0,
                    signal_type TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_delivered_at TEXT,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (session_date, ticker)
                );
                CREATE TABLE IF NOT EXISTS alert_runs (
                    id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    session_date TEXT,
                    market_open INTEGER,
                    broad_count INTEGER,
                    strict_count INTEGER,
                    pending_count INTEGER,
                    delivery_status TEXT,
                    error TEXT,
                    snapshot_json TEXT
                );
            """)

    def start_run(self, *, mode: str, market_open: bool | None = None) -> str:
        run_id = str(uuid4())
        with self._session() as db:
            db.execute(
                "INSERT INTO alert_runs (id, started_at, mode, status, market_open) VALUES (?, ?, ?, ?, ?)",
                (run_id, _now(), mode, "running", None if market_open is None else int(market_open)),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        snapshot: dict[str, Any] | None = None,
        delivery_status: str | None = None,
        error: str | None = None,
    ) -> None:
        data = snapshot or {}
        with self._session() as db:
            db.execute("""
                UPDATE alert_runs
                SET completed_at=?, status=?, session_date=?, market_open=?, broad_count=?,
                    strict_count=?, pending_count=?, delivery_status=?, error=?, snapshot_json=?
                WHERE id=?
            """, (
                _now(), status, data.get("session_date"),
                None if not data else int(bool((data.get("market_hours") or {}).get("isMarketOpen"))),
                data.get("broad_count"), data.get("strict_count"),
                data.get("pending_upgrade_count"), delivery_status, error,
                json.dumps(data, ensure_ascii=False, default=str) if snapshot is not None else None,
                run_id,
            ))

    def observe(self, session_date: str, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        now = _now()
        with self._session() as db:
            for original in rows:
                row = dict(original)
                ticker = str(row.get("ticker") or "").upper()
                signal_type = str(row.get("signal_type") or "CANDIDATE").upper()
                rank = SIGNAL_RANK.get(signal_type, 1)
                existing = db.execute(
                    "SELECT max_seen_rank, max_delivered_rank FROM signal_state WHERE session_date=? AND ticker=?",
                    (session_date, ticker),
                ).fetchone()
                payload = json.dumps(row, ensure_ascii=False, default=str)
                if existing is None:
                    db.execute("""
                        INSERT INTO signal_state (
                            session_date, ticker, max_seen_rank, max_delivered_rank, signal_type,
                            first_seen_at, last_seen_at, payload_json
                        ) VALUES (?, ?, ?, 0, ?, ?, ?, ?)
                    """, (session_date, ticker, rank, signal_type, now, now, payload))
                    delivered_rank = 0
                else:
                    delivered_rank = int(existing["max_delivered_rank"])
                    db.execute("""
                        UPDATE signal_state
                        SET max_seen_rank=MAX(max_seen_rank, ?), signal_type=?, last_seen_at=?, payload_json=?
                        WHERE session_date=? AND ticker=?
                    """, (rank, signal_type, now, payload, session_date, ticker))
                if rank > delivered_rank:
                    row["is_upgrade"] = True
                    row["previous_delivered_rank"] = delivered_rank
                    pending.append(row)
        return pending

    def mark_delivered(self, session_date: str, rows: Iterable[dict[str, Any]]) -> None:
        now = _now()
        with self._session() as db:
            for row in rows:
                ticker = str(row.get("ticker") or "").upper()
                rank = SIGNAL_RANK.get(str(row.get("signal_type") or "CANDIDATE").upper(), 1)
                db.execute("""
                    UPDATE signal_state
                    SET max_delivered_rank=MAX(max_delivered_rank, ?), last_delivered_at=?
                    WHERE session_date=? AND ticker=?
                """, (rank, now, session_date, ticker))

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._session() as db:
            rows = db.execute(
                "SELECT * FROM alert_runs ORDER BY started_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]
