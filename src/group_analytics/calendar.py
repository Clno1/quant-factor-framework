"""Exchange-session utilities for the EOD publication gate."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .models import GroupAnalyticsError


class CalendarUnavailableError(GroupAnalyticsError):
    code = "EXCHANGE_CALENDAR_UNAVAILABLE"
    stage = "resolve_session"


def _calendar(calendar: Any | None = None) -> Any:
    if calendar is not None:
        return calendar
    try:
        import exchange_calendars as xcals
    except ImportError as exc:  # pragma: no cover - exercised in deployment preflight
        raise CalendarUnavailableError(
            "exchange-calendars is required for latest EOD session resolution"
        ) from exc
    return xcals.get_calendar("XNYS")


def _utc_timestamp(value: datetime | pd.Timestamp | None) -> pd.Timestamp:
    if value is None:
        value = datetime.now(timezone.utc)
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def latest_completed_session(
    *,
    now: datetime | pd.Timestamp | None = None,
    calendar: Any | None = None,
) -> pd.Timestamp:
    """Return the latest XNYS session whose official close is not in the future."""
    cal = _calendar(calendar)
    now_utc = _utc_timestamp(now)
    start = (now_utc - pd.Timedelta(days=14)).date().isoformat()
    end = (now_utc + pd.Timedelta(days=1)).date().isoformat()
    sessions = cal.sessions_in_range(start, end)
    completed = [session for session in sessions if _utc_timestamp(cal.session_close(session)) <= now_utc]
    if not completed:
        raise CalendarUnavailableError("No completed XNYS session found in the lookback window")
    return pd.Timestamp(completed[-1]).tz_localize(None).normalize()


def official_session_close(
    session: str | pd.Timestamp,
    *,
    calendar: Any | None = None,
) -> pd.Timestamp:
    """Return the official close with timezone for a valid XNYS session."""
    cal = _calendar(calendar)
    label = pd.Timestamp(session).tz_localize(None).normalize()
    try:
        close = cal.session_close(label)
    except Exception as exc:  # noqa: BLE001
        raise CalendarUnavailableError(f"Not a valid XNYS session: {label.date()}") from exc
    return _utc_timestamp(close)


def previous_session(
    session: str | pd.Timestamp,
    *,
    calendar: Any | None = None,
) -> pd.Timestamp:
    """Return the immediately preceding XNYS session label."""
    cal = _calendar(calendar)
    label = pd.Timestamp(session).tz_localize(None).normalize()
    try:
        previous = cal.previous_session(label)
    except Exception as exc:  # noqa: BLE001
        raise CalendarUnavailableError(
            f"Unable to resolve the previous XNYS session for {label.date()}"
        ) from exc
    value = pd.Timestamp(previous)
    if value.tzinfo is not None:
        value = value.tz_localize(None)
    return value.normalize()


__all__ = [
    "CalendarUnavailableError",
    "latest_completed_session",
    "official_session_close",
    "previous_session",
]
