"""Durable at-most-once outbox for paper-trading Discord notifications."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator


DELIVERY_PENDING = "PENDING"
DELIVERY_SENDING = "SENDING"
DELIVERY_SENT = "SENT"
DELIVERY_FAILED = "FAILED"
DELIVERY_UNKNOWN = "UNKNOWN"
DELIVERY_BASELINED = "BASELINED"

KIND_FILL = "FILL"
KIND_DAILY_SUMMARY = "DAILY_SUMMARY"
_KINDS = {KIND_FILL, KIND_DAILY_SUMMARY}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _payload_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def payload_hash(payload: dict[str, Any]) -> str:
    encoded = _payload_json(payload).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class PaperDeliveryClaim:
    delivery_id: str
    kind: str
    account_id: str | None
    target_session: str
    source_id: str
    payload: dict[str, Any]
    payload_hash: str
    attempts: int


class PaperNotificationState:
    """Own the notification outbox without owning trading truth."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_notification_outbox (
                    delivery_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    account_id TEXT,
                    target_session TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    retryable INTEGER,
                    message_id TEXT,
                    last_error_code TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sent_at TEXT,
                    UNIQUE(kind, source_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_paper_notification_pending
                ON paper_notification_outbox(status, kind, created_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_paper_notification_session
                ON paper_notification_outbox(target_session, kind, status)
                """
            )

    @contextmanager
    def run_lock(self) -> Iterator[None]:
        with self.lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def stage(
        self,
        *,
        delivery_id: str,
        kind: str,
        account_id: str | None,
        target_session: str,
        source_id: str,
        payload: dict[str, Any],
        baseline: bool = False,
    ) -> bool:
        if kind not in _KINDS:
            raise ValueError(f"Unsupported paper notification kind: {kind}")
        encoded = _payload_json(payload)
        digest = payload_hash(payload)
        timestamp = _now()
        initial_status = DELIVERY_BASELINED if baseline else DELIVERY_PENDING
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM paper_notification_outbox WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if row is not None:
                if (
                    str(row["kind"]) != kind
                    or str(row["source_id"]) != source_id
                    or str(row["payload_hash"]) != digest
                ):
                    raise RuntimeError(
                        "Paper notification identity already exists with different "
                        f"immutable content: {delivery_id}"
                    )
                return False
            connection.execute(
                """
                INSERT INTO paper_notification_outbox (
                    delivery_id, kind, account_id, target_session, source_id,
                    payload_hash, payload_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    kind,
                    account_id,
                    target_session,
                    source_id,
                    digest,
                    encoded,
                    initial_status,
                    timestamp,
                    timestamp,
                ),
            )
        return True

    def claim_next(
        self,
        *,
        kinds: set[str],
        max_attempts: int,
    ) -> PaperDeliveryClaim | None:
        if not kinds or not kinds.issubset(_KINDS):
            raise ValueError("At least one valid paper notification kind is required")
        placeholders = ",".join("?" for _ in sorted(kinds))
        ordered_kinds = sorted(kinds)
        timestamp = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE paper_notification_outbox
                SET status=?, retryable=NULL, last_error_code=?, last_error=?,
                    updated_at=?
                WHERE status=?
                """,
                (
                    DELIVERY_UNKNOWN,
                    "INTERRUPTED_SENDING",
                    "A prior process ended without a confirmed Discord response.",
                    timestamp,
                    DELIVERY_SENDING,
                ),
            )
            row = connection.execute(
                f"""
                SELECT * FROM paper_notification_outbox
                WHERE kind IN ({placeholders})
                  AND (
                    status=?
                    OR (status=? AND retryable=1 AND attempts < ?)
                  )
                ORDER BY created_at, delivery_id
                LIMIT 1
                """,
                (
                    *ordered_kinds,
                    DELIVERY_PENDING,
                    DELIVERY_FAILED,
                    max(1, int(max_attempts)),
                ),
            ).fetchone()
            if row is None:
                return None
            attempts = int(row["attempts"] or 0) + 1
            cursor = connection.execute(
                """
                UPDATE paper_notification_outbox
                SET status=?, attempts=?, retryable=NULL, last_error_code=NULL,
                    last_error=NULL, updated_at=?
                WHERE delivery_id=? AND status IN (?, ?)
                """,
                (
                    DELIVERY_SENDING,
                    attempts,
                    timestamp,
                    str(row["delivery_id"]),
                    DELIVERY_PENDING,
                    DELIVERY_FAILED,
                ),
            )
            if cursor.rowcount != 1:
                return None
            return PaperDeliveryClaim(
                delivery_id=str(row["delivery_id"]),
                kind=str(row["kind"]),
                account_id=(
                    str(row["account_id"]) if row["account_id"] is not None else None
                ),
                target_session=str(row["target_session"]),
                source_id=str(row["source_id"]),
                payload=json.loads(str(row["payload_json"])),
                payload_hash=str(row["payload_hash"]),
                attempts=attempts,
            )

    def mark_sent(self, claim: PaperDeliveryClaim, *, message_id: str) -> None:
        timestamp = _now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE paper_notification_outbox
                SET status=?, message_id=?, sent_at=?, updated_at=?, retryable=NULL,
                    last_error_code=NULL, last_error=NULL
                WHERE delivery_id=? AND status=? AND payload_hash=?
                """,
                (
                    DELIVERY_SENT,
                    str(message_id),
                    timestamp,
                    timestamp,
                    claim.delivery_id,
                    DELIVERY_SENDING,
                    claim.payload_hash,
                ),
            )
            if cursor.rowcount == 1:
                return
            row = connection.execute(
                "SELECT status, message_id FROM paper_notification_outbox WHERE delivery_id=?",
                (claim.delivery_id,),
            ).fetchone()
            if row is None or not (
                str(row["status"]) == DELIVERY_SENT
                and str(row["message_id"] or "") == str(message_id)
            ):
                raise RuntimeError(
                    "Paper notification changed before SENT could be committed"
                )

    def mark_failed(
        self,
        claim: PaperDeliveryClaim,
        *,
        error_code: str,
        error_message: str,
        uncertain: bool,
        retryable: bool,
    ) -> None:
        status = DELIVERY_UNKNOWN if uncertain else DELIVERY_FAILED
        retryable_value = None if uncertain else int(bool(retryable))
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE paper_notification_outbox
                SET status=?, retryable=?, last_error_code=?, last_error=?,
                    updated_at=?
                WHERE delivery_id=? AND status=?
                """,
                (
                    status,
                    retryable_value,
                    str(error_code)[:80],
                    str(error_message)[:500],
                    _now(),
                    claim.delivery_id,
                    DELIVERY_SENDING,
                ),
            )

    def status(self, *, include_recent: bool = True) -> dict[str, Any]:
        with self._connection() as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            counts = connection.execute(
                """
                SELECT kind, status, COUNT(*) AS count
                FROM paper_notification_outbox
                GROUP BY kind, status
                ORDER BY kind, status
                """
            ).fetchall()
            recent = []
            if include_recent:
                recent = connection.execute(
                    """
                    SELECT delivery_id, kind, account_id, target_session, source_id,
                           status, attempts, message_id, last_error_code, created_at,
                           updated_at, sent_at
                    FROM paper_notification_outbox
                    ORDER BY created_at DESC, delivery_id DESC
                    LIMIT 20
                    """
                ).fetchall()
        output = {
            "database": str(self.path),
            "integrity": integrity,
            "counts": {
                f"{row['kind']}:{row['status']}": int(row["count"])
                for row in counts
            },
        }
        if include_recent:
            output["recent"] = [dict(row) for row in recent]
        return output


__all__ = [
    "DELIVERY_BASELINED",
    "DELIVERY_FAILED",
    "DELIVERY_PENDING",
    "DELIVERY_SENDING",
    "DELIVERY_SENT",
    "DELIVERY_UNKNOWN",
    "KIND_DAILY_SUMMARY",
    "KIND_FILL",
    "PaperDeliveryClaim",
    "PaperNotificationState",
    "payload_hash",
]
