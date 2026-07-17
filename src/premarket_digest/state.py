"""SQLite outbox and process lock for one-message-per-channel delivery."""
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

from .models import DeliveryState, DigestChannel


class ConcurrentDigestWorkerError(RuntimeError):
    """Raised when another process already owns the delivery lock."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _payload_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def payload_hash(payload: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_payload_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    action: str
    status: DeliveryState
    payload: dict[str, Any] | None = None
    payload_hash: str | None = None
    attempts: int = 0
    message_id: str | None = None
    error_code: str | None = None


class DigestStateStore:
    def __init__(self, path: Path | str):
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
                CREATE TABLE IF NOT EXISTS deliveries (
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
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(deliveries)").fetchall()
            }
            if "retryable" not in columns:
                connection.execute(
                    "ALTER TABLE deliveries ADD COLUMN retryable INTEGER"
                )

    @contextmanager
    def run_lock(self) -> Iterator[None]:
        with self.lock_path.open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ConcurrentDigestWorkerError(
                    "another premarket digest worker is already running"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def stage(
        self,
        target_session: str,
        channel: DigestChannel,
        source_session: str,
        payload: dict[str, Any],
        *,
        rebuild_failed: bool = False,
    ) -> dict[str, Any]:
        encoded = _payload_json(payload)
        digest = "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        timestamp = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM deliveries WHERE target_session=? AND channel=?",
                (target_session, channel.value),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO deliveries (
                        target_session, channel, destination, source_session,
                        payload_hash, payload_json, status, attempts,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        target_session,
                        channel.value,
                        channel.destination,
                        source_session,
                        digest,
                        encoded,
                        DeliveryState.PENDING.value,
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM deliveries WHERE target_session=? AND channel=?",
                    (target_session, channel.value),
                ).fetchone()
            elif rebuild_failed and str(row["status"]) == DeliveryState.FAILED.value:
                connection.execute(
                    """
                    UPDATE deliveries
                    SET destination=?, source_session=?, payload_hash=?, payload_json=?,
                        status=?, message_id=NULL, last_error_code=NULL,
                        last_error=NULL, updated_at=?, sent_at=NULL, retryable=NULL
                    WHERE target_session=? AND channel=? AND status=?
                    """,
                    (
                        channel.destination,
                        source_session,
                        digest,
                        encoded,
                        DeliveryState.PENDING.value,
                        timestamp,
                        target_session,
                        channel.value,
                        DeliveryState.FAILED.value,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM deliveries WHERE target_session=? AND channel=?",
                    (target_session, channel.value),
                ).fetchone()
            connection.commit()
        return dict(row)

    def claim(
        self,
        target_session: str,
        channel: DigestChannel,
        *,
        retry_unknown: bool = False,
    ) -> DeliveryClaim:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM deliveries WHERE target_session=? AND channel=?",
                (target_session, channel.value),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RuntimeError("delivery must be staged before it is claimed")
            status = DeliveryState(str(row["status"]))
            if status is DeliveryState.SENT:
                connection.commit()
                return DeliveryClaim(
                    "already_sent",
                    status,
                    payload_hash=str(row["payload_hash"]),
                    attempts=int(row["attempts"]),
                    message_id=row["message_id"],
                )
            if status is DeliveryState.SENDING:
                status = DeliveryState.UNKNOWN
                connection.execute(
                    """
                    UPDATE deliveries
                    SET status=?, last_error_code=?, last_error=?, retryable=NULL,
                        updated_at=?
                    WHERE target_session=? AND channel=?
                    """,
                    (
                        status.value,
                        "INTERRUPTED_SENDING",
                        "A previous process ended without a confirmed Discord response.",
                        _now(),
                        target_session,
                        channel.value,
                    ),
                )
            if status is DeliveryState.UNKNOWN and not retry_unknown:
                connection.commit()
                return DeliveryClaim(
                    "unknown_blocked",
                    status,
                    payload_hash=str(row["payload_hash"]),
                    attempts=int(row["attempts"]),
                )
            if status is DeliveryState.FAILED and int(row["retryable"] or 0) != 1:
                connection.commit()
                return DeliveryClaim(
                    "permanent_blocked",
                    status,
                    payload_hash=str(row["payload_hash"]),
                    attempts=int(row["attempts"]),
                    error_code=str(row["last_error_code"] or "PERMANENT_FAILURE_FROZEN"),
                )
            attempts = int(row["attempts"]) + 1
            connection.execute(
                """
                UPDATE deliveries
                SET status=?, attempts=?, last_error_code=NULL, last_error=NULL,
                    retryable=NULL, updated_at=?
                WHERE target_session=? AND channel=?
                """,
                (
                    DeliveryState.SENDING.value,
                    attempts,
                    _now(),
                    target_session,
                    channel.value,
                ),
            )
            connection.commit()
            return DeliveryClaim(
                "send",
                DeliveryState.SENDING,
                payload=json.loads(str(row["payload_json"])),
                payload_hash=str(row["payload_hash"]),
                attempts=attempts,
            )

    def mark_sent(
        self,
        target_session: str,
        channel: DigestChannel,
        message_id: str,
        *,
        source_session: str | None = None,
        payload: dict[str, Any] | None = None,
        expected_payload_hash: str | None = None,
        attempts: int = 1,
    ) -> None:
        timestamp = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE deliveries
                SET status=?, message_id=?, sent_at=?, updated_at=?,
                    last_error_code=NULL, last_error=NULL, retryable=NULL
                WHERE target_session=? AND channel=? AND status=?
                """,
                (
                    DeliveryState.SENT.value,
                    str(message_id),
                    timestamp,
                    timestamp,
                    target_session,
                    channel.value,
                    DeliveryState.SENDING.value,
                ),
            )
            if cursor.rowcount == 1:
                return
            row = connection.execute(
                "SELECT status, message_id FROM deliveries WHERE target_session=? AND channel=?",
                (target_session, channel.value),
            ).fetchone()
            if row is not None:
                if (
                    str(row["status"]) == DeliveryState.SENT.value
                    and str(row["message_id"] or "") == str(message_id)
                ):
                    return
                raise RuntimeError("delivery state changed before SENT could be committed")
            if (
                source_session is None
                or payload is None
                or expected_payload_hash is None
            ):
                raise RuntimeError("delivery row disappeared before SENT could be committed")
            encoded = _payload_json(payload)
            if payload_hash(payload) != expected_payload_hash:
                raise RuntimeError("confirmed payload hash does not match the delivery claim")
            connection.execute(
                """
                INSERT INTO deliveries (
                    target_session, channel, destination, source_session,
                    payload_hash, payload_json, status, attempts, message_id,
                    created_at, updated_at, sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target_session,
                    channel.value,
                    channel.destination,
                    source_session,
                    expected_payload_hash,
                    encoded,
                    DeliveryState.SENT.value,
                    max(1, int(attempts)),
                    str(message_id),
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )

    def release_unsent(
        self,
        target_session: str,
        channel: DigestChannel,
        *,
        reason: str,
    ) -> None:
        """Return a claimed row to PENDING when no HTTP request was made."""
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE deliveries
                SET status=?, last_error_code=?, last_error=?, retryable=NULL,
                    updated_at=?
                WHERE target_session=? AND channel=? AND status=?
                """,
                (
                    DeliveryState.PENDING.value,
                    "DELIVERY_WINDOW_CLOSED",
                    reason[:500],
                    _now(),
                    target_session,
                    channel.value,
                    DeliveryState.SENDING.value,
                ),
            )

    def mark_failed(
        self,
        target_session: str,
        channel: DigestChannel,
        *,
        error_code: str,
        error_message: str,
        uncertain: bool,
        retryable: bool,
    ) -> None:
        status = DeliveryState.UNKNOWN if uncertain else DeliveryState.FAILED
        retryable_value = None if uncertain else int(bool(retryable))
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE deliveries
                SET status=?, last_error_code=?, last_error=?, retryable=?, updated_at=?
                WHERE target_session=? AND channel=?
                """,
                (
                    status.value,
                    error_code[:80],
                    error_message[:500],
                    retryable_value,
                    _now(),
                    target_session,
                    channel.value,
                ),
            )

    def get(self, target_session: str, channel: DigestChannel) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM deliveries WHERE target_session=? AND channel=?",
                (target_session, channel.value),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result.pop("payload_json", None)
        return result


__all__ = [
    "ConcurrentDigestWorkerError",
    "DeliveryClaim",
    "DigestStateStore",
    "payload_hash",
]
