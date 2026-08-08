"""Dedicated SQLite state for heartbeat, recovery and signal idempotency."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator

from src.breakouts.live.models import BreakoutSignal, MonitorSymbolState
from src.utils.io import ensure_dir


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class IntradayMonitorState:
    def __init__(self, path: str | Path) -> None:
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
                CREATE TABLE IF NOT EXISTS candidate_snapshots (
                    session_date TEXT NOT NULL,
                    algorithm_version TEXT NOT NULL,
                    parameter_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (session_date, algorithm_version, parameter_version)
                );
                CREATE TABLE IF NOT EXISTS symbol_state (
                    session_date TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    algorithm_version TEXT NOT NULL,
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (session_date, ticker, algorithm_version)
                );
                CREATE TABLE IF NOT EXISTS signals (
                    session_date TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    algorithm_version TEXT NOT NULL,
                    trigger_family TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    delivery_state TEXT NOT NULL,
                    delivered_at TEXT,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (
                        session_date, ticker, algorithm_version, trigger_family
                    )
                );
                CREATE TABLE IF NOT EXISTS heartbeat (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
            """)

    def save_candidate_snapshot(self, snapshot: dict[str, Any]) -> None:
        with self._session() as db:
            db.execute("""
                INSERT INTO candidate_snapshots (
                    session_date, algorithm_version, parameter_version,
                    created_at, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_date, algorithm_version, parameter_version)
                DO UPDATE SET created_at=excluded.created_at,
                              payload_json=excluded.payload_json
            """, (
                snapshot["session_date"],
                snapshot["algorithm_version"],
                snapshot["parameter_version"],
                _now(),
                json.dumps(snapshot, ensure_ascii=False, default=str),
            ))

    def load_candidate_snapshot(
        self,
        session_date: str,
        algorithm_version: str,
        parameter_version: str,
    ) -> dict[str, Any] | None:
        with self._session() as db:
            row = db.execute("""
                SELECT payload_json FROM candidate_snapshots
                WHERE session_date=? AND algorithm_version=? AND parameter_version=?
            """, (session_date, algorithm_version, parameter_version)).fetchone()
        return json.loads(row["payload_json"]) if row is not None else None

    def set_symbol_state(
        self,
        *,
        session_date: str,
        ticker: str,
        algorithm_version: str,
        state: MonitorSymbolState,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.set_symbol_states(
            session_date=session_date,
            algorithm_version=algorithm_version,
            rows=[(ticker, state, payload or {})],
        )

    def set_symbol_states(
        self,
        *,
        session_date: str,
        algorithm_version: str,
        rows: Iterable[
            tuple[str, MonitorSymbolState, dict[str, Any]]
        ],
    ) -> None:
        normalized = [
            (
                session_date,
                str(ticker).upper(),
                algorithm_version,
                state.value,
                _now(),
                json.dumps(payload, ensure_ascii=False, default=str),
            )
            for ticker, state, payload in rows
        ]
        if not normalized:
            return
        with self._session() as db:
            db.executemany("""
                INSERT INTO symbol_state (
                    session_date, ticker, algorithm_version, state,
                    updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_date, ticker, algorithm_version)
                DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at,
                              payload_json=excluded.payload_json
            """, normalized)

    def record_signal(
        self,
        signal: BreakoutSignal,
        *,
        delivery_state: str = "SHADOW",
    ) -> bool:
        with self._session() as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO signals (
                    session_date, ticker, algorithm_version, trigger_family,
                    signal_type, first_seen_at, delivery_state, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal.session_date,
                signal.ticker,
                signal.algorithm_version,
                signal.trigger_family,
                signal.signal_type,
                _now(),
                delivery_state,
                json.dumps(signal.to_dict(), ensure_ascii=False, default=str),
            ))
            return cursor.rowcount == 1

    def heartbeat(self, payload: dict[str, Any]) -> None:
        normalized = dict(payload)
        normalized["updated_at"] = _now()
        with self._session() as db:
            db.execute("""
                INSERT INTO heartbeat (singleton, updated_at, payload_json)
                VALUES (1, ?, ?)
                ON CONFLICT(singleton)
                DO UPDATE SET updated_at=excluded.updated_at,
                              payload_json=excluded.payload_json
            """, (
                normalized["updated_at"],
                json.dumps(normalized, ensure_ascii=False, default=str),
            ))

    def status(self) -> dict[str, Any]:
        with self._session() as db:
            heartbeat = db.execute(
                "SELECT payload_json FROM heartbeat WHERE singleton=1"
            ).fetchone()
            signals = db.execute("""
                SELECT session_date, ticker, signal_type, first_seen_at,
                       delivery_state
                FROM signals ORDER BY first_seen_at DESC LIMIT 20
            """).fetchall()
            counts = db.execute("""
                SELECT delivery_state, COUNT(*) AS count
                FROM signals GROUP BY delivery_state
            """).fetchall()
        return {
            "heartbeat": (
                json.loads(heartbeat["payload_json"])
                if heartbeat is not None
                else None
            ),
            "signal_counts": {
                str(row["delivery_state"]): int(row["count"])
                for row in counts
            },
            "recent_signals": [dict(row) for row in signals],
        }
