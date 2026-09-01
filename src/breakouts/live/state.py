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
                CREATE TABLE IF NOT EXISTS cup_handle_evaluations (
                    session_date TEXT NOT NULL,
                    cycle_minute TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    algorithm_version TEXT NOT NULL,
                    parameter_version TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    rejection_reason TEXT NOT NULL,
                    latency_ms REAL NOT NULL,
                    bar_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (
                        session_date, cycle_minute, ticker, algorithm_version
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_cup_handle_evaluations_session
                    ON cup_handle_evaluations (
                        session_date, algorithm_version, outcome, rejection_reason
                    );
                CREATE TABLE IF NOT EXISTS cup_handle_cycles (
                    session_date TEXT NOT NULL,
                    cycle_minute TEXT NOT NULL,
                    algorithm_version TEXT NOT NULL,
                    parameter_version TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    evaluation_count INTEGER NOT NULL,
                    match_count INTEGER NOT NULL,
                    rejected_count INTEGER NOT NULL,
                    not_ready_count INTEGER NOT NULL,
                    unevaluable_count INTEGER NOT NULL,
                    error_count INTEGER NOT NULL,
                    p95_latency_ms REAL NOT NULL,
                    max_bar_count INTEGER NOT NULL,
                    daily_evaluated_count INTEGER NOT NULL,
                    daily_candidate_count INTEGER NOT NULL,
                    data_contract_complete INTEGER NOT NULL,
                    PRIMARY KEY (
                        session_date, cycle_minute, algorithm_version
                    )
                );
                CREATE TABLE IF NOT EXISTS cup_handle_session_observations (
                    session_date TEXT NOT NULL,
                    algorithm_version TEXT NOT NULL,
                    parameter_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expected_open_cycles INTEGER NOT NULL,
                    observed_open_cycles INTEGER NOT NULL,
                    cycle_coverage REAL NOT NULL,
                    evaluation_count INTEGER NOT NULL,
                    match_count INTEGER NOT NULL,
                    rejected_count INTEGER NOT NULL,
                    not_ready_count INTEGER NOT NULL,
                    unevaluable_count INTEGER NOT NULL,
                    error_count INTEGER NOT NULL,
                    error_cycle_ratio REAL NOT NULL,
                    detection_p95_ms REAL NOT NULL,
                    max_bar_count INTEGER NOT NULL,
                    unique_ticker_count INTEGER NOT NULL,
                    evaluable_ticker_count INTEGER NOT NULL,
                    evaluable_ticker_coverage REAL NOT NULL,
                    gap_ticker_count INTEGER NOT NULL,
                    gap_ticker_ratio REAL NOT NULL,
                    gap_event_count INTEGER NOT NULL,
                    gap_classification_counts_json TEXT NOT NULL,
                    rejection_counts_json TEXT NOT NULL,
                    failure_reasons_json TEXT NOT NULL,
                    finalized_at TEXT NOT NULL,
                    PRIMARY KEY (session_date, algorithm_version)
                );
                CREATE TABLE IF NOT EXISTS cup_handle_data_gaps (
                    session_date TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    algorithm_version TEXT NOT NULL,
                    parameter_version TEXT NOT NULL,
                    gap_start TEXT NOT NULL,
                    gap_end TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    observation_count INTEGER NOT NULL,
                    PRIMARY KEY (
                        session_date, ticker, algorithm_version, gap_start
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_cup_handle_data_gaps_session
                    ON cup_handle_data_gaps (
                        session_date, algorithm_version, classification, ticker
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
            migrations = {
                "cup_handle_cycles": (
                    ("unevaluable_count", "INTEGER NOT NULL DEFAULT 0"),
                ),
                "cup_handle_session_observations": (
                    ("unevaluable_count", "INTEGER NOT NULL DEFAULT 0"),
                    ("unique_ticker_count", "INTEGER NOT NULL DEFAULT 0"),
                    ("evaluable_ticker_count", "INTEGER NOT NULL DEFAULT 0"),
                    ("evaluable_ticker_coverage", "REAL NOT NULL DEFAULT 0"),
                    ("gap_ticker_count", "INTEGER NOT NULL DEFAULT 0"),
                    ("gap_ticker_ratio", "REAL NOT NULL DEFAULT 0"),
                    ("gap_event_count", "INTEGER NOT NULL DEFAULT 0"),
                    (
                        "gap_classification_counts_json",
                        "TEXT NOT NULL DEFAULT '{}'",
                    ),
                ),
            }
            for table, columns in migrations.items():
                table_columns = {
                    str(row["name"])
                    for row in db.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for column, definition in columns:
                    if column not in table_columns:
                        db.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
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

    def record_cup_handle_cycle(
        self,
        payload: dict[str, Any],
        evaluations: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        """Persist one bounded detector cycle and its per-symbol decisions."""
        observed_at = datetime.fromisoformat(str(payload["observed_at"]))
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        observed_utc = observed_at.astimezone(timezone.utc)
        cycle_minute = observed_utc.replace(second=0, microsecond=0).isoformat(
            timespec="minutes"
        )
        rows = [dict(value) for value in evaluations]
        latencies = [float(value.get("latency_ms") or 0.0) for value in rows]
        outcomes = [str(value.get("outcome") or "ERROR").upper() for value in rows]
        algorithm_version = str(payload["algorithm_version"])
        parameter_version = str(payload["parameter_version"])
        session_date = str(payload["session_date"])
        summary = {
            "session_date": session_date,
            "cycle_minute": cycle_minute,
            "algorithm_version": algorithm_version,
            "parameter_version": parameter_version,
            "evaluation_count": len(rows),
            "match_count": outcomes.count("MATCH"),
            "rejected_count": outcomes.count("REJECTED"),
            "not_ready_count": outcomes.count("NOT_READY"),
            "unevaluable_count": outcomes.count("UNEVALUABLE"),
            "error_count": outcomes.count("ERROR"),
            "p95_latency_ms": _percentile(latencies, 0.95),
            "max_bar_count": max(
                (int(value.get("bar_count") or 0) for value in rows),
                default=0,
            ),
            "daily_evaluated_count": int(
                payload.get("daily_evaluated_count") or 0
            ),
            "daily_candidate_count": int(
                payload.get("daily_candidate_count") or 0
            ),
            "data_contract_complete": int(
                bool(payload.get("data_contract_complete"))
            ),
            "observed_at": observed_utc.isoformat(timespec="seconds"),
        }
        with self._session() as db:
            db.execute("""
                INSERT INTO cup_handle_cycles (
                    session_date, cycle_minute, algorithm_version,
                    parameter_version, observed_at, evaluation_count,
                    match_count, rejected_count, not_ready_count,
                    unevaluable_count, error_count,
                    p95_latency_ms, max_bar_count, daily_evaluated_count,
                    daily_candidate_count, data_contract_complete
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_date, cycle_minute, algorithm_version)
                DO UPDATE SET parameter_version=excluded.parameter_version,
                              observed_at=excluded.observed_at,
                              evaluation_count=excluded.evaluation_count,
                              match_count=excluded.match_count,
                              rejected_count=excluded.rejected_count,
                              not_ready_count=excluded.not_ready_count,
                              unevaluable_count=excluded.unevaluable_count,
                              error_count=excluded.error_count,
                              p95_latency_ms=excluded.p95_latency_ms,
                              max_bar_count=excluded.max_bar_count,
                              daily_evaluated_count=excluded.daily_evaluated_count,
                              daily_candidate_count=excluded.daily_candidate_count,
                              data_contract_complete=excluded.data_contract_complete
            """, (
                session_date,
                cycle_minute,
                algorithm_version,
                parameter_version,
                summary["observed_at"],
                summary["evaluation_count"],
                summary["match_count"],
                summary["rejected_count"],
                summary["not_ready_count"],
                summary["unevaluable_count"],
                summary["error_count"],
                summary["p95_latency_ms"],
                summary["max_bar_count"],
                summary["daily_evaluated_count"],
                summary["daily_candidate_count"],
                summary["data_contract_complete"],
            ))
            db.executemany("""
                INSERT INTO cup_handle_evaluations (
                    session_date, cycle_minute, ticker, algorithm_version,
                    parameter_version, evaluated_at, outcome,
                    rejection_reason, latency_ms, bar_count, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_date, cycle_minute, ticker, algorithm_version)
                DO UPDATE SET parameter_version=excluded.parameter_version,
                              evaluated_at=excluded.evaluated_at,
                              outcome=excluded.outcome,
                              rejection_reason=excluded.rejection_reason,
                              latency_ms=excluded.latency_ms,
                              bar_count=excluded.bar_count,
                              payload_json=excluded.payload_json
            """, [
                (
                    session_date,
                    cycle_minute,
                    str(value.get("ticker") or "").upper(),
                    algorithm_version,
                    parameter_version,
                    str(value.get("evaluated_at") or summary["observed_at"]),
                    str(value.get("outcome") or "ERROR").upper(),
                    str(value.get("rejection_reason") or "UNKNOWN")[:120],
                    float(value.get("latency_ms") or 0.0),
                    int(value.get("bar_count") or 0),
                    _payload_json(value),
                )
                for value in rows
                if str(value.get("ticker") or "").strip()
            ])
            for value in rows:
                ticker = str(value.get("ticker") or "").strip().upper()
                data_quality = (value.get("details") or {}).get(
                    "data_quality"
                ) or {}
                for gap in data_quality.get("gaps") or []:
                    gap_start = str(gap.get("gap_start") or "")
                    gap_end = str(gap.get("gap_end") or "")
                    if not ticker or not gap_start or not gap_end:
                        continue
                    classification = str(
                        gap.get("classification") or "UNRESOLVED_SOURCE_GAP"
                    )
                    db.execute("""
                        INSERT INTO cup_handle_data_gaps (
                            session_date, ticker, algorithm_version,
                            parameter_version, gap_start, gap_end,
                            classification, evidence_json, first_seen_at,
                            last_seen_at, observation_count
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        ON CONFLICT (
                            session_date, ticker, algorithm_version, gap_start
                        ) DO UPDATE SET
                            parameter_version=excluded.parameter_version,
                            gap_end=excluded.gap_end,
                            classification=excluded.classification,
                            evidence_json=excluded.evidence_json,
                            last_seen_at=excluded.last_seen_at,
                            observation_count=(
                                cup_handle_data_gaps.observation_count + 1
                            )
                    """, (
                        session_date,
                        ticker,
                        algorithm_version,
                        parameter_version,
                        gap_start,
                        gap_end,
                        classification,
                        _payload_json(gap.get("evidence") or {}),
                        summary["observed_at"],
                        summary["observed_at"],
                    ))
        return summary

    def finalize_cup_handle_observation(
        self,
        *,
        session_date: str,
        algorithm_version: str,
        parameter_version: str,
        expected_open_cycles: int,
        min_cycle_coverage: float,
        max_error_cycle_ratio: float,
        max_detection_p95_ms: float,
        min_evaluable_ticker_coverage: float,
        max_gap_ticker_ratio: float,
        max_bar_count: int,
    ) -> dict[str, Any]:
        """Finalize a detector-specific session without reusing legacy promotion."""
        with self._session() as db:
            cycles = db.execute("""
                SELECT * FROM cup_handle_cycles
                WHERE session_date=? AND algorithm_version=?
                ORDER BY cycle_minute
            """, (session_date, algorithm_version)).fetchall()
            evaluations = db.execute("""
                SELECT ticker, outcome, rejection_reason, latency_ms, bar_count
                FROM cup_handle_evaluations
                WHERE session_date=? AND algorithm_version=?
            """, (session_date, algorithm_version)).fetchall()
            gap_rows = db.execute("""
                SELECT ticker, classification, COUNT(*) AS event_count
                FROM cup_handle_data_gaps
                WHERE session_date=? AND algorithm_version=?
                GROUP BY ticker, classification
            """, (session_date, algorithm_version)).fetchall()
            observed = len(cycles)
            coverage = observed / max(1, int(expected_open_cycles))
            error_cycles = sum(int(row["error_count"] or 0) > 0 for row in cycles)
            error_ratio = error_cycles / max(1, observed)
            latencies = [float(row["latency_ms"] or 0.0) for row in evaluations]
            detection_p95 = _percentile(latencies, 0.95)
            outcomes = [str(row["outcome"] or "ERROR") for row in evaluations]
            unique_tickers = {
                str(row["ticker"] or "").upper()
                for row in evaluations
                if str(row["ticker"] or "").strip()
            }
            unevaluable_tickers = {
                str(row["ticker"] or "").upper()
                for row in evaluations
                if str(row["outcome"] or "") == "UNEVALUABLE"
            }
            gap_tickers = {
                str(row["ticker"] or "").upper()
                for row in gap_rows
                if str(row["ticker"] or "").strip()
            }
            unevaluable_tickers.update(gap_tickers)
            evaluable_tickers = unique_tickers - unevaluable_tickers
            unique_ticker_count = len(unique_tickers)
            evaluable_coverage = (
                len(evaluable_tickers) / unique_ticker_count
                if unique_ticker_count else 0.0
            )
            gap_ratio = (
                len(gap_tickers) / unique_ticker_count
                if unique_ticker_count else 0.0
            )
            gap_classification_counts: dict[str, int] = {}
            for row in gap_rows:
                classification = str(
                    row["classification"] or "UNRESOLVED_SOURCE_GAP"
                )
                gap_classification_counts[classification] = (
                    gap_classification_counts.get(classification, 0)
                    + int(row["event_count"] or 0)
                )
            gap_event_count = sum(gap_classification_counts.values())
            rejection_counts: dict[str, int] = {}
            for row in evaluations:
                if str(row["outcome"] or "") == "MATCH":
                    continue
                reason = str(row["rejection_reason"] or "UNKNOWN")
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            observed_max_bars = max(
                (int(row["bar_count"] or 0) for row in evaluations),
                default=0,
            )
            daily_evaluated = max(
                (int(row["daily_evaluated_count"] or 0) for row in cycles),
                default=0,
            )
            contract_complete = any(
                int(row["data_contract_complete"] or 0) == 1 for row in cycles
            )
            reasons: list[str] = []
            if coverage < min_cycle_coverage:
                reasons.append("INSUFFICIENT_CYCLE_COVERAGE")
            if not evaluations:
                reasons.append("NO_CUP_HANDLE_EVALUATIONS")
            if daily_evaluated <= 0:
                reasons.append("NO_DAILY_CUP_SCREEN")
            if not contract_complete:
                reasons.append("MISSING_DATA_CONTRACT")
            if error_ratio > max_error_cycle_ratio:
                reasons.append("EXCESSIVE_DETECTOR_ERRORS")
            if evaluable_coverage < min_evaluable_ticker_coverage:
                reasons.append("INSUFFICIENT_EVALUABLE_TICKER_COVERAGE")
            if gap_ratio > max_gap_ticker_ratio:
                reasons.append("EXCESSIVE_MINUTE_DATA_GAPS")
            if detection_p95 > max_detection_p95_ms:
                reasons.append("DETECTION_P95_TOO_SLOW")
            if observed_max_bars > max_bar_count:
                reasons.append("UNBOUNDED_5M_SEQUENCE")
            status = "PASS" if not reasons else "FAIL"
            summary = {
                "session_date": session_date,
                "algorithm_version": algorithm_version,
                "parameter_version": parameter_version,
                "status": status,
                "expected_open_cycles": int(expected_open_cycles),
                "observed_open_cycles": observed,
                "cycle_coverage": round(coverage, 6),
                "evaluation_count": len(evaluations),
                "match_count": outcomes.count("MATCH"),
                "rejected_count": outcomes.count("REJECTED"),
                "not_ready_count": outcomes.count("NOT_READY"),
                "unevaluable_count": outcomes.count("UNEVALUABLE"),
                "error_count": outcomes.count("ERROR"),
                "error_cycle_ratio": round(error_ratio, 6),
                "detection_p95_ms": round(detection_p95, 6),
                "max_bar_count": observed_max_bars,
                "unique_ticker_count": unique_ticker_count,
                "evaluable_ticker_count": len(evaluable_tickers),
                "evaluable_ticker_coverage": round(evaluable_coverage, 6),
                "gap_ticker_count": len(gap_tickers),
                "gap_ticker_ratio": round(gap_ratio, 6),
                "gap_event_count": gap_event_count,
                "gap_classification_counts": gap_classification_counts,
                "rejection_counts": dict(
                    sorted(rejection_counts.items(), key=lambda item: (-item[1], item[0]))
                ),
                "failure_reasons": reasons,
                "finalized_at": _now(),
            }
            db.execute("""
                INSERT INTO cup_handle_session_observations (
                    session_date, algorithm_version, parameter_version, status,
                    expected_open_cycles, observed_open_cycles, cycle_coverage,
                    evaluation_count, match_count, rejected_count,
                    not_ready_count, unevaluable_count, error_count,
                    error_cycle_ratio, detection_p95_ms, max_bar_count,
                    unique_ticker_count, evaluable_ticker_count,
                    evaluable_ticker_coverage, gap_ticker_count,
                    gap_ticker_ratio, gap_event_count,
                    gap_classification_counts_json, rejection_counts_json,
                    failure_reasons_json, finalized_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_date, algorithm_version)
                DO UPDATE SET parameter_version=excluded.parameter_version,
                              status=excluded.status,
                              expected_open_cycles=excluded.expected_open_cycles,
                              observed_open_cycles=excluded.observed_open_cycles,
                              cycle_coverage=excluded.cycle_coverage,
                              evaluation_count=excluded.evaluation_count,
                              match_count=excluded.match_count,
                              rejected_count=excluded.rejected_count,
                              not_ready_count=excluded.not_ready_count,
                              unevaluable_count=excluded.unevaluable_count,
                              error_count=excluded.error_count,
                              error_cycle_ratio=excluded.error_cycle_ratio,
                              detection_p95_ms=excluded.detection_p95_ms,
                              max_bar_count=excluded.max_bar_count,
                              unique_ticker_count=excluded.unique_ticker_count,
                              evaluable_ticker_count=excluded.evaluable_ticker_count,
                              evaluable_ticker_coverage=excluded.evaluable_ticker_coverage,
                              gap_ticker_count=excluded.gap_ticker_count,
                              gap_ticker_ratio=excluded.gap_ticker_ratio,
                              gap_event_count=excluded.gap_event_count,
                              gap_classification_counts_json=excluded.gap_classification_counts_json,
                              rejection_counts_json=excluded.rejection_counts_json,
                              failure_reasons_json=excluded.failure_reasons_json,
                              finalized_at=excluded.finalized_at
            """, (
                session_date,
                algorithm_version,
                parameter_version,
                status,
                int(expected_open_cycles),
                observed,
                coverage,
                len(evaluations),
                summary["match_count"],
                summary["rejected_count"],
                summary["not_ready_count"],
                summary["unevaluable_count"],
                summary["error_count"],
                error_ratio,
                detection_p95,
                observed_max_bars,
                unique_ticker_count,
                len(evaluable_tickers),
                evaluable_coverage,
                len(gap_tickers),
                gap_ratio,
                gap_event_count,
                json.dumps(gap_classification_counts, ensure_ascii=False),
                json.dumps(summary["rejection_counts"], ensure_ascii=False),
                json.dumps(reasons, ensure_ascii=False),
                summary["finalized_at"],
            ))
        return summary

    def cup_handle_promotion_status(
        self,
        expected_sessions: Iterable[str],
        *,
        algorithm_version: str,
    ) -> dict[str, Any]:
        expected = [str(value) for value in expected_sessions]
        with self._session() as db:
            placeholders = ",".join("?" for _ in expected)
            rows = (
                db.execute(
                    f"""SELECT * FROM cup_handle_session_observations
                        WHERE algorithm_version=?
                          AND session_date IN ({placeholders})""",
                    [algorithm_version, *expected],
                ).fetchall()
                if expected
                else []
            )
        by_session = {str(row["session_date"]): dict(row) for row in rows}
        missing = [session for session in expected if session not in by_session]
        failed = [
            session for session in expected
            if session in by_session and str(by_session[session]["status"]) != "PASS"
        ]
        return {
            "algorithm_version": algorithm_version,
            "eligible": bool(expected) and not missing and not failed,
            "required_sessions": len(expected),
            "passed_sessions": sum(
                str(by_session[session]["status"]) == "PASS"
                for session in expected if session in by_session
            ),
            "expected_sessions": expected,
            "missing_sessions": missing,
            "failed_sessions": failed,
        }

    def prune_cup_handle_detail(self, *, keep_sessions: int) -> dict[str, int]:
        """Bound detailed cycle/evaluation storage while retaining daily evidence."""
        keep = max(1, int(keep_sessions))
        with self._session() as db:
            dates = [
                str(row["session_date"])
                for row in db.execute("""
                    SELECT DISTINCT session_date FROM cup_handle_cycles
                    ORDER BY session_date DESC
                """).fetchall()
            ]
            if len(dates) <= keep:
                return {"cycles_deleted": 0, "evaluations_deleted": 0}
            cutoff = dates[keep - 1]
            evaluations = db.execute(
                "DELETE FROM cup_handle_evaluations WHERE session_date < ?",
                (cutoff,),
            ).rowcount
            cycles = db.execute(
                "DELETE FROM cup_handle_cycles WHERE session_date < ?",
                (cutoff,),
            ).rowcount
        return {
            "cycles_deleted": int(cycles),
            "evaluations_deleted": int(evaluations),
        }

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
            cup_observations = db.execute("""
                SELECT * FROM cup_handle_session_observations
                ORDER BY session_date DESC LIMIT 20
            """).fetchall()
            cup_counts = db.execute("""
                SELECT session_date, outcome, COUNT(*) AS count
                FROM cup_handle_evaluations
                WHERE session_date=(SELECT MAX(session_date) FROM cup_handle_evaluations)
                GROUP BY session_date, outcome
            """).fetchall()
            cup_rejections = db.execute("""
                SELECT rejection_reason, COUNT(*) AS count
                FROM cup_handle_evaluations
                WHERE session_date=(SELECT MAX(session_date) FROM cup_handle_evaluations)
                  AND outcome!='MATCH'
                GROUP BY rejection_reason
                ORDER BY count DESC, rejection_reason LIMIT 10
            """).fetchall()
            cup_gaps = db.execute("""
                SELECT session_date, classification,
                       COUNT(*) AS event_count,
                       COUNT(DISTINCT ticker) AS ticker_count
                FROM cup_handle_data_gaps
                WHERE session_date=(SELECT MAX(session_date) FROM cup_handle_data_gaps)
                GROUP BY session_date, classification
                ORDER BY event_count DESC, classification
            """).fetchall()
        observation_rows = []
        for row in observations:
            value = dict(row)
            value["failure_reasons"] = json.loads(
                str(value.pop("failure_reasons_json") or "[]")
            )
            observation_rows.append(value)
        cup_observation_rows = []
        for row in cup_observations:
            value = dict(row)
            value["rejection_counts"] = json.loads(
                str(value.pop("rejection_counts_json") or "{}")
            )
            value["failure_reasons"] = json.loads(
                str(value.pop("failure_reasons_json") or "[]")
            )
            value["gap_classification_counts"] = json.loads(
                str(value.pop("gap_classification_counts_json", "{}") or "{}")
            )
            cup_observation_rows.append(value)
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
            "cup_handle": {
                "latest_outcome_counts": {
                    str(row["outcome"]): int(row["count"])
                    for row in cup_counts
                },
                "latest_rejection_counts": {
                    str(row["rejection_reason"]): int(row["count"])
                    for row in cup_rejections
                },
                "latest_gap_counts": [dict(row) for row in cup_gaps],
                "session_observations": cup_observation_rows,
            },
        }


__all__ = ["IntradayMonitorState", "SignalDeliveryClaim"]
