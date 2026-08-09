"""Dedicated SQLite state for heartbeat, recovery and signal idempotency."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator

from src.breakouts.live.models import BreakoutSignal, MonitorSymbolState
from src.utils.io import ensure_dir


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _payload_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = _payload_json(payload).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


@dataclass(frozen=True, slots=True)
class SignalDeliveryClaim:
    session_date: str
    ticker: str
    algorithm_version: str
    trigger_family: str
    payload: dict[str, Any]
    payload_hash: str
    attempts: int


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
                CREATE TABLE IF NOT EXISTS signal_outbox (
                    session_date TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    algorithm_version TEXT NOT NULL,
                    trigger_family TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    message_id TEXT,
                    last_error_code TEXT,
                    last_error TEXT,
                    retryable INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sent_at TEXT,
                    PRIMARY KEY (
                        session_date, ticker, algorithm_version, trigger_family
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_signal_outbox_pending
                    ON signal_outbox (session_date, status, created_at);
                CREATE INDEX IF NOT EXISTS idx_signal_outbox_ticker_sent
                    ON signal_outbox (ticker, sent_at);
                CREATE TABLE IF NOT EXISTS monitor_cycles (
                    session_date TEXT NOT NULL,
                    cycle_minute TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    market_open INTEGER NOT NULL,
                    cycle_seconds REAL,
                    error_count INTEGER NOT NULL,
                    candidate_count INTEGER NOT NULL,
                    active_count INTEGER NOT NULL,
                    data_universe TEXT,
                    dataset_version_id TEXT,
                    bars_sha256 TEXT,
                    PRIMARY KEY (session_date, cycle_minute)
                );
                CREATE TABLE IF NOT EXISTS session_observations (
                    session_date TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    expected_open_cycles INTEGER NOT NULL,
                    observed_open_cycles INTEGER NOT NULL,
                    cycle_coverage REAL NOT NULL,
                    error_cycles INTEGER NOT NULL,
                    error_cycle_ratio REAL NOT NULL,
                    cycle_p95_seconds REAL NOT NULL,
                    candidate_count INTEGER NOT NULL,
                    failure_reasons_json TEXT NOT NULL,
                    finalized_at TEXT NOT NULL
                );
            """)
            existing = {
                str(row["name"])
                for row in db.execute(
                    "PRAGMA table_info(candidate_snapshots)"
                ).fetchall()
            }
            for column in (
                "data_universe",
                "dataset_version_id",
                "bars_sha256",
            ):
                if column not in existing:
                    db.execute(
                        f"ALTER TABLE candidate_snapshots ADD COLUMN {column} TEXT"
                    )

    def save_candidate_snapshot(self, snapshot: dict[str, Any]) -> None:
        contract = snapshot.get("data_contract") or {}
        with self._session() as db:
            db.execute("""
                INSERT INTO candidate_snapshots (
                    session_date, algorithm_version, parameter_version,
                    data_universe, dataset_version_id, bars_sha256,
                    created_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_date, algorithm_version, parameter_version)
                DO UPDATE SET data_universe=excluded.data_universe,
                              dataset_version_id=excluded.dataset_version_id,
                              bars_sha256=excluded.bars_sha256,
                              created_at=excluded.created_at,
                              payload_json=excluded.payload_json
            """, (
                snapshot["session_date"],
                snapshot["algorithm_version"],
                snapshot["parameter_version"],
                str(contract.get("data_universe") or snapshot.get("data_universe") or ""),
                str(contract.get("dataset_version_id") or snapshot.get("dataset_version_id") or ""),
                str(contract.get("bars_sha256") or ""),
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

    def stage_signal_delivery(
        self,
        signal: BreakoutSignal,
        payload: dict[str, Any],
        *,
        shadow: bool,
    ) -> bool:
        """Persist one immutable delivery payload next to its canonical signal."""
        timestamp = _now()
        status = "SHADOW" if shadow else "PENDING"
        encoded = _payload_json(payload)
        digest = _payload_hash(payload)
        with self._session() as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO signal_outbox (
                    session_date, ticker, algorithm_version, trigger_family,
                    payload_hash, payload_json, status, attempts,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """, (
                signal.session_date,
                signal.ticker,
                signal.algorithm_version,
                signal.trigger_family,
                digest,
                encoded,
                status,
                timestamp,
                timestamp,
            ))
            if cursor.rowcount == 1:
                db.execute("""
                    UPDATE signals SET delivery_state=?
                    WHERE session_date=? AND ticker=? AND algorithm_version=?
                      AND trigger_family=?
                """, (
                    status,
                    signal.session_date,
                    signal.ticker,
                    signal.algorithm_version,
                    signal.trigger_family,
                ))
            return cursor.rowcount == 1

    def claim_next_delivery(
        self,
        *,
        session_date: str,
        now: datetime,
        cooldown_minutes: int,
        max_attempts: int,
    ) -> SignalDeliveryClaim | None:
        """Claim one safe-to-send row, suppressing ticker duplicates in cooldown."""
        aware_now = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        now_utc = aware_now.astimezone(timezone.utc)
        cutoff = (now_utc - timedelta(minutes=max(0, cooldown_minutes))).isoformat(
            timespec="seconds"
        )
        timestamp = now_utc.isoformat(timespec="seconds")
        with self._session() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                UPDATE signal_outbox
                SET status='UNKNOWN', last_error_code='INTERRUPTED_SENDING',
                    last_error='A previous process ended without a confirmed Discord response.',
                    retryable=NULL, updated_at=?
                WHERE session_date=? AND status='SENDING'
            """, (timestamp, session_date))
            while True:
                row = db.execute("""
                    SELECT * FROM signal_outbox
                    WHERE session_date=?
                      AND (
                        status='PENDING'
                        OR (status='FAILED' AND retryable=1 AND attempts < ?)
                      )
                    ORDER BY created_at, ticker
                    LIMIT 1
                """, (session_date, max_attempts)).fetchone()
                if row is None:
                    return None
                sent = db.execute("""
                    SELECT sent_at FROM signal_outbox
                    WHERE ticker=? AND status='SENT' AND sent_at>=?
                    ORDER BY sent_at DESC LIMIT 1
                """, (row["ticker"], cutoff)).fetchone()
                key = (
                    row["session_date"],
                    row["ticker"],
                    row["algorithm_version"],
                    row["trigger_family"],
                )
                if sent is not None:
                    db.execute("""
                        UPDATE signal_outbox
                        SET status='SUPPRESSED_COOLDOWN', updated_at=?, retryable=0,
                            last_error_code='TICKER_COOLDOWN',
                            last_error='A confirmed message for this ticker is still in cooldown.'
                        WHERE session_date=? AND ticker=? AND algorithm_version=?
                          AND trigger_family=?
                    """, (timestamp, *key))
                    db.execute("""
                        UPDATE signals SET delivery_state='SUPPRESSED_COOLDOWN'
                        WHERE session_date=? AND ticker=? AND algorithm_version=?
                          AND trigger_family=?
                    """, key)
                    continue
                attempts = int(row["attempts"]) + 1
                cursor = db.execute("""
                    UPDATE signal_outbox
                    SET status='SENDING', attempts=?, updated_at=?,
                        last_error_code=NULL, last_error=NULL, retryable=NULL
                    WHERE session_date=? AND ticker=? AND algorithm_version=?
                      AND trigger_family=? AND status IN ('PENDING', 'FAILED')
                """, (attempts, timestamp, *key))
                if cursor.rowcount != 1:
                    continue
                db.execute("""
                    UPDATE signals SET delivery_state='SENDING'
                    WHERE session_date=? AND ticker=? AND algorithm_version=?
                      AND trigger_family=?
                """, key)
                return SignalDeliveryClaim(
                    session_date=str(row["session_date"]),
                    ticker=str(row["ticker"]),
                    algorithm_version=str(row["algorithm_version"]),
                    trigger_family=str(row["trigger_family"]),
                    payload=json.loads(str(row["payload_json"])),
                    payload_hash=str(row["payload_hash"]),
                    attempts=attempts,
                )

    def mark_delivery_sent(
        self,
        claim: SignalDeliveryClaim,
        *,
        message_id: str,
    ) -> None:
        timestamp = _now()
        key = (
            claim.session_date,
            claim.ticker,
            claim.algorithm_version,
            claim.trigger_family,
        )
        with self._session() as db:
            cursor = db.execute("""
                UPDATE signal_outbox
                SET status='SENT', message_id=?, sent_at=?, updated_at=?,
                    last_error_code=NULL, last_error=NULL, retryable=NULL
                WHERE session_date=? AND ticker=? AND algorithm_version=?
                  AND trigger_family=? AND status='SENDING'
            """, (str(message_id), timestamp, timestamp, *key))
            if cursor.rowcount != 1:
                row = db.execute("""
                    SELECT status, message_id FROM signal_outbox
                    WHERE session_date=? AND ticker=? AND algorithm_version=?
                      AND trigger_family=?
                """, key).fetchone()
                if row is None or not (
                    str(row["status"]) == "SENT"
                    and str(row["message_id"] or "") == str(message_id)
                ):
                    raise RuntimeError("signal outbox changed before SENT could be committed")
            db.execute("""
                UPDATE signals SET delivery_state='SENT', delivered_at=?
                WHERE session_date=? AND ticker=? AND algorithm_version=?
                  AND trigger_family=?
            """, (timestamp, *key))

    def mark_delivery_failed(
        self,
        claim: SignalDeliveryClaim,
        *,
        error_code: str,
        uncertain: bool,
        retryable: bool,
    ) -> None:
        status = "UNKNOWN" if uncertain else "FAILED"
        retryable_value = None if uncertain else int(bool(retryable))
        timestamp = _now()
        key = (
            claim.session_date,
            claim.ticker,
            claim.algorithm_version,
            claim.trigger_family,
        )
        with self._session() as db:
            db.execute("""
                UPDATE signal_outbox
                SET status=?, last_error_code=?,
                    last_error='Discord delivery did not produce a confirmed message ID.',
                    retryable=?, updated_at=?
                WHERE session_date=? AND ticker=? AND algorithm_version=?
                  AND trigger_family=? AND status='SENDING'
            """, (
                status,
                str(error_code)[:80],
                retryable_value,
                timestamp,
                *key,
            ))
            db.execute("""
                UPDATE signals SET delivery_state=?
                WHERE session_date=? AND ticker=? AND algorithm_version=?
                  AND trigger_family=?
            """, (status, *key))

    def record_monitor_cycle(self, payload: dict[str, Any]) -> None:
        observed_at = datetime.fromisoformat(str(payload["observed_at"]))
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        cycle_minute = observed_at.astimezone(timezone.utc).replace(
            second=0,
            microsecond=0,
        ).isoformat(timespec="minutes")
        with self._session() as db:
            db.execute("""
                INSERT INTO monitor_cycles (
                    session_date, cycle_minute, mode, phase, observed_at,
                    market_open, cycle_seconds, error_count, candidate_count,
                    active_count, data_universe, dataset_version_id, bars_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_date, cycle_minute)
                DO UPDATE SET mode=excluded.mode, phase=excluded.phase,
                              observed_at=excluded.observed_at,
                              market_open=excluded.market_open,
                              cycle_seconds=excluded.cycle_seconds,
                              error_count=excluded.error_count,
                              candidate_count=excluded.candidate_count,
                              active_count=excluded.active_count,
                              data_universe=excluded.data_universe,
                              dataset_version_id=excluded.dataset_version_id,
                              bars_sha256=excluded.bars_sha256
            """, (
                str(payload["session_date"]),
                cycle_minute,
                str(payload.get("mode") or "shadow"),
                str(payload.get("phase") or "unknown"),
                observed_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
                int(bool(payload.get("market_open"))),
                payload.get("cycle_seconds"),
                len(payload.get("errors") or []),
                int(payload.get("candidate_count") or 0),
                int(payload.get("active_count") or 0),
                payload.get("data_universe"),
                payload.get("dataset_version_id"),
                payload.get("bars_sha256"),
            ))

    def finalize_session_observation(
        self,
        *,
        session_date: str,
        expected_open_cycles: int,
        min_cycle_coverage: float,
        max_error_cycle_ratio: float,
        max_cycle_p95_seconds: float,
    ) -> dict[str, Any]:
        with self._session() as db:
            rows = db.execute("""
                SELECT * FROM monitor_cycles
                WHERE session_date=? AND market_open=1 AND phase='completed'
                ORDER BY cycle_minute
            """, (session_date,)).fetchall()
            observed = len(rows)
            error_cycles = sum(int(row["error_count"] or 0) > 0 for row in rows)
            durations = [
                float(row["cycle_seconds"])
                for row in rows
                if row["cycle_seconds"] is not None
            ]
            coverage = observed / max(1, int(expected_open_cycles))
            error_ratio = error_cycles / max(1, observed)
            cycle_p95 = _percentile(durations, 0.95)
            candidate_count = max(
                (int(row["candidate_count"] or 0) for row in rows),
                default=0,
            )
            contract_complete = any(
                row["data_universe"]
                and row["dataset_version_id"]
                and row["bars_sha256"]
                for row in rows
            )
            reasons: list[str] = []
            if coverage < min_cycle_coverage:
                reasons.append("INSUFFICIENT_CYCLE_COVERAGE")
            if error_ratio > max_error_cycle_ratio:
                reasons.append("EXCESSIVE_ERROR_CYCLES")
            if cycle_p95 > max_cycle_p95_seconds:
                reasons.append("CYCLE_P95_TOO_SLOW")
            if candidate_count <= 0:
                reasons.append("NO_DAILY_CANDIDATES")
            if not contract_complete:
                reasons.append("MISSING_DATA_CONTRACT")
            status = "PASS" if not reasons else "FAIL"
            summary = {
                "session_date": session_date,
                "status": status,
                "expected_open_cycles": int(expected_open_cycles),
                "observed_open_cycles": observed,
                "cycle_coverage": round(coverage, 6),
                "error_cycles": error_cycles,
                "error_cycle_ratio": round(error_ratio, 6),
                "cycle_p95_seconds": round(cycle_p95, 6),
                "candidate_count": candidate_count,
                "failure_reasons": reasons,
                "finalized_at": _now(),
            }
            db.execute("""
                INSERT INTO session_observations (
                    session_date, status, expected_open_cycles,
                    observed_open_cycles, cycle_coverage, error_cycles,
                    error_cycle_ratio, cycle_p95_seconds, candidate_count,
                    failure_reasons_json, finalized_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_date)
                DO UPDATE SET status=excluded.status,
                              expected_open_cycles=excluded.expected_open_cycles,
                              observed_open_cycles=excluded.observed_open_cycles,
                              cycle_coverage=excluded.cycle_coverage,
                              error_cycles=excluded.error_cycles,
                              error_cycle_ratio=excluded.error_cycle_ratio,
                              cycle_p95_seconds=excluded.cycle_p95_seconds,
                              candidate_count=excluded.candidate_count,
                              failure_reasons_json=excluded.failure_reasons_json,
                              finalized_at=excluded.finalized_at
            """, (
                session_date,
                status,
                int(expected_open_cycles),
                observed,
                coverage,
                error_cycles,
                error_ratio,
                cycle_p95,
                candidate_count,
                json.dumps(reasons, ensure_ascii=False),
                summary["finalized_at"],
            ))
        return summary

    def promotion_status(self, expected_sessions: Iterable[str]) -> dict[str, Any]:
        expected = [str(value) for value in expected_sessions]
        with self._session() as db:
            placeholders = ",".join("?" for _ in expected)
            rows = (
                db.execute(
                    f"SELECT * FROM session_observations WHERE session_date IN ({placeholders})",
                    expected,
                ).fetchall()
                if expected
                else []
            )
        by_session = {str(row["session_date"]): dict(row) for row in rows}
        missing = [session for session in expected if session not in by_session]
        failed = [
            session
            for session in expected
            if session in by_session and str(by_session[session]["status"]) != "PASS"
        ]
        return {
            "eligible": bool(expected) and not missing and not failed,
            "required_sessions": len(expected),
            "passed_sessions": sum(
                str(by_session[session]["status"]) == "PASS"
                for session in expected
                if session in by_session
            ),
            "expected_sessions": expected,
            "missing_sessions": missing,
            "failed_sessions": failed,
        }

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
            outbox_counts = db.execute("""
                SELECT status, COUNT(*) AS count
                FROM signal_outbox GROUP BY status
            """).fetchall()
            recent_outbox = db.execute("""
                SELECT session_date, ticker, trigger_family, status, attempts,
                       message_id, last_error_code, created_at, sent_at
                FROM signal_outbox ORDER BY created_at DESC LIMIT 20
            """).fetchall()
            observations = db.execute("""
                SELECT session_date, status, expected_open_cycles,
                       observed_open_cycles, cycle_coverage, error_cycles,
                       error_cycle_ratio, cycle_p95_seconds, candidate_count,
                       failure_reasons_json, finalized_at
                FROM session_observations
                ORDER BY session_date DESC LIMIT 20
            """).fetchall()
        observation_rows = []
        for row in observations:
            value = dict(row)
            value["failure_reasons"] = json.loads(
                str(value.pop("failure_reasons_json") or "[]")
            )
            observation_rows.append(value)
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
            "outbox_counts": {
                str(row["status"]): int(row["count"])
                for row in outbox_counts
            },
            "recent_outbox": [dict(row) for row in recent_outbox],
            "session_observations": observation_rows,
        }


__all__ = ["IntradayMonitorState", "SignalDeliveryClaim"]
