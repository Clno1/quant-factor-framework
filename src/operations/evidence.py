"""Small, fail-closed helpers shared by operational evidence collectors."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd

from src.config import CONFIG, PROJECT_ROOT
from src.operations.models import JobDefinition, JobStatus
from src.utils.market_calendar import (
    latest_publishable_xnys_session,
    xnys_session_on_or_after,
)


_SECRET = re.compile(
    r"(?i)(api[_-]?key|authorization|password|secret|token)(\s*[=:]\s*)([^\s&;,]+)"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | pd.Timestamp | str | None) -> str | None:
    if value is None or value == "":
        return None
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.isoformat()


def parse_datetime(value: Any) -> datetime | None:
    normalized = iso_utc(value)
    return datetime.fromisoformat(normalized) if normalized else None


def safe_text(value: Any, *, limit: int = 800) -> str | None:
    if value is None:
        return None
    text = str(value).replace(str(PROJECT_ROOT), "<project>")
    text = _SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text)
    return text[: max(1, int(limit))]


def stable_id(prefix: str, *parts: Any) -> str:
    source = "\x1f".join(str(part or "") for part in parts)
    return prefix + hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]


def load_json(path: str | Path) -> dict[str, Any] | None:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def sqlite_rows(
    path: str | Path,
    sql: str,
    parameters: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    if not candidate.is_file():
        return []

    def query(*, immutable: bool) -> list[dict[str, Any]]:
        suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
        uri = "file:" + quote(str(candidate.resolve())) + suffix
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=2000")
        try:
            return [
                dict(row)
                for row in connection.execute(sql, parameters).fetchall()
            ]
        finally:
            connection.close()

    try:
        return query(immutable=False)
    except sqlite3.OperationalError as exc:
        if "unable to open database file" not in str(exc).lower():
            raise

    # ProtectHome=read-only can prevent SQLite from opening a WAL database
    # because a reader may create or update its -shm sidecar.  Immutable mode
    # is safe only when no transaction log contains data and the main file is
    # unchanged for the entire query.  Otherwise this collector fails closed
    # and the watchdog retains the prior coherent snapshot.
    def file_identity(candidate_path: Path) -> tuple[int, int, int] | None:
        try:
            info = candidate_path.stat()
        except FileNotFoundError:
            return None
        return info.st_ino, info.st_size, info.st_mtime_ns

    database_before = file_identity(candidate)
    sidecars = [
        Path(str(candidate) + suffix)
        for suffix in ("-wal", "-journal")
    ]
    sidecars_before = {str(item): file_identity(item) for item in sidecars}
    if any(identity and identity[1] > 0 for identity in sidecars_before.values()):
        raise sqlite3.OperationalError(
            "read-only SQLite evidence has an active transaction log"
        )
    rows = query(immutable=True)
    database_after = file_identity(candidate)
    sidecars_after = {str(item): file_identity(item) for item in sidecars}
    if (
        database_before is None
        or database_before != database_after
        or any(identity and identity[1] > 0 for identity in sidecars_after.values())
        or sidecars_before != sidecars_after
    ):
        raise sqlite3.OperationalError(
            "SQLite evidence changed during immutable read"
        )
    return rows


def sqlite_tables(path: str | Path) -> set[str]:
    rows = sqlite_rows(
        path,
        "SELECT name FROM sqlite_master WHERE type='table'",
    )
    return {str(row["name"]) for row in rows}


def expected_target_session(
    job: JobDefinition,
    *,
    now: datetime,
) -> str | None:
    policy = str(job.schedule.get("target_policy") or "")
    if policy == "latest_publishable_xnys":
        delay = int(CONFIG.data.foundation.close_delay_minutes)
        return latest_publishable_xnys_session(
            now=now,
            delay_minutes=delay,
        ).date().isoformat()
    if policy == "current_xnys":
        local_now = now.astimezone(ZoneInfo("America/New_York"))
        return xnys_session_on_or_after(local_now.date().isoformat()).date().isoformat()
    return None


def schedule_bounds(
    job: JobDefinition,
    *,
    now: datetime,
    target_session: str | None,
) -> tuple[str | None, str | None]:
    schedule = job.schedule
    timezone_name = str(schedule.get("timezone") or "UTC")
    trigger = schedule.get("time")
    if not trigger or not target_session:
        return None, None
    local_day = date.fromisoformat(target_session)
    if timezone_name == "Asia/Singapore" and str(
        schedule.get("target_policy")
    ) == "latest_publishable_xnys":
        local_day += timedelta(days=1)
    hour, minute = (int(part) for part in str(trigger).split(":", 1))
    scheduled = datetime.combine(
        local_day,
        time(hour=hour, minute=minute),
        tzinfo=ZoneInfo(timezone_name),
    )
    if schedule.get("deadline_time"):
        end_hour, end_minute = (
            int(part)
            for part in str(schedule["deadline_time"]).split(":", 1)
        )
        deadline = datetime.combine(
            local_day,
            time(hour=end_hour, minute=end_minute),
            tzinfo=ZoneInfo(timezone_name),
        )
    else:
        deadline = scheduled + timedelta(
            minutes=int(schedule.get("deadline_minutes") or 0)
        )
    return iso_utc(scheduled), iso_utc(deadline)


def time_relative_status(
    *,
    now: datetime,
    scheduled_for: str | None,
    deadline_at: str | None,
    has_older_evidence: bool,
) -> JobStatus:
    scheduled = parse_datetime(scheduled_for)
    deadline = parse_datetime(deadline_at)
    if scheduled and now < scheduled:
        return JobStatus.SCHEDULED
    if deadline and now > deadline:
        return JobStatus.STALE if has_older_evidence else JobStatus.MISSED
    return JobStatus.SCHEDULED


def session_delay(expected: str | None, actual: str | None) -> int | None:
    if not expected or not actual:
        return None
    expected_day = pd.Timestamp(expected).normalize()
    actual_day = pd.Timestamp(actual).normalize()
    if actual_day >= expected_day:
        return 0
    try:
        import exchange_calendars as xcals

        calendar = xcals.get_calendar(
            "XNYS",
            start=(actual_day - pd.Timedelta(days=7)).date().isoformat(),
            end=(expected_day + pd.Timedelta(days=7)).date().isoformat(),
        )
        return max(
            0,
            len(calendar.sessions_in_range(actual_day, expected_day)) - 1,
        )
    except Exception:
        return max(0, (expected_day - actual_day).days)


def status_from_source(value: Any) -> JobStatus:
    normalized = str(value or "").strip().upper().replace("-", "_")
    aliases = {
        "PUBLISHED": JobStatus.SUCCESS,
        "PASS": JobStatus.SUCCESS,
        "READY": JobStatus.SUCCESS,
        "COMPLETED": JobStatus.SUCCESS,
        "COMPLETE": JobStatus.SUCCESS,
        "SENT": JobStatus.SUCCESS,
        "NOOP": JobStatus.SKIPPED,
        "DRY_RUN": JobStatus.SKIPPED,
        "OBSERVING": JobStatus.RUNNING,
        "WAITING_FOR_PROVIDER": JobStatus.BLOCKED,
        "WAITING_FOR_DATA": JobStatus.BLOCKED,
        "REJECTED": JobStatus.FAILED,
        "ERROR": JobStatus.FAILED,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return JobStatus(normalized)
    except ValueError:
        return JobStatus.UNKNOWN


def duration_seconds(started_at: Any, completed_at: Any) -> float | None:
    started = parse_datetime(started_at)
    completed = parse_datetime(completed_at)
    if not started or not completed:
        return None
    return max(0.0, (completed - started).total_seconds())


__all__ = [
    "duration_seconds",
    "expected_target_session",
    "iso_utc",
    "load_json",
    "parse_datetime",
    "safe_text",
    "schedule_bounds",
    "session_delay",
    "sqlite_rows",
    "sqlite_tables",
    "stable_id",
    "status_from_source",
    "time_relative_status",
    "utc_now",
]
