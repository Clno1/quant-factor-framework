"""Shared exchange-session helpers for core trading domains."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd


def _calendar(
    calendar: Any | None = None,
    *,
    start: str | None = None,
    end: str | None = None,
) -> Any:
    if calendar is not None:
        return calendar
    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise RuntimeError(
            "exchange-calendars is required for XNYS session resolution"
        ) from exc
    kwargs = {
        key: value
        for key, value in {"start": start, "end": end}.items()
        if value
    }
    return xcals.get_calendar("XNYS", **kwargs)


def _utc_timestamp(value: datetime | pd.Timestamp | None) -> pd.Timestamp:
    if value is None:
        value = datetime.now(timezone.utc)
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def latest_completed_xnys_session(
    *,
    now: datetime | pd.Timestamp | None = None,
    calendar: Any | None = None,
) -> pd.Timestamp:
    """Return the latest XNYS session whose official close has passed."""
    exchange = _calendar(calendar)
    now_utc = _utc_timestamp(now)
    sessions = exchange.sessions_in_range(
        (now_utc - pd.Timedelta(days=14)).date().isoformat(),
        (now_utc + pd.Timedelta(days=1)).date().isoformat(),
    )
    completed = [
        session
        for session in sessions
        if _utc_timestamp(exchange.session_close(session)) <= now_utc
    ]
    if not completed:
        raise RuntimeError("No completed XNYS session found")
    value = pd.Timestamp(completed[-1])
    if value.tzinfo is not None:
        value = value.tz_localize(None)
    return value.normalize()


def latest_publishable_xnys_session(
    *,
    now: datetime | pd.Timestamp | None = None,
    delay_minutes: int = 120,
    calendar: Any | None = None,
) -> pd.Timestamp:
    """
    Return the latest XNYS session whose close plus publication delay has passed.

    The delay prevents the daily writer from publishing while the vendor may
    still be finalizing the session's OHLCV bar.  It is measured from the
    exchange's actual close, so early closes and daylight-saving changes need
    no special cases in the scheduler.
    """
    if int(delay_minutes) < 0:
        raise ValueError("delay_minutes must be non-negative")
    exchange = _calendar(calendar)
    now_utc = _utc_timestamp(now)
    sessions = exchange.sessions_in_range(
        (now_utc - pd.Timedelta(days=14)).date().isoformat(),
        (now_utc + pd.Timedelta(days=1)).date().isoformat(),
    )
    publishable = [
        session
        for session in sessions
        if (
            _utc_timestamp(exchange.session_close(session))
            + pd.Timedelta(minutes=int(delay_minutes))
        )
        <= now_utc
    ]
    if not publishable:
        raise RuntimeError("No publishable XNYS session found")
    value = pd.Timestamp(publishable[-1])
    if value.tzinfo is not None:
        value = value.tz_localize(None)
    return value.normalize()


def is_xnys_session(
    value: str | pd.Timestamp,
    *,
    calendar: Any | None = None,
) -> bool:
    """Return whether ``value`` is an official XNYS trading session."""
    exchange = _calendar(calendar)
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_localize(None)
    return bool(exchange.is_session(timestamp.normalize()))


def xnys_session_on_or_before(
    value: str | pd.Timestamp,
    *,
    calendar: Any | None = None,
) -> pd.Timestamp:
    """Resolve a calendar date to the same or previous XNYS session."""
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_localize(None)
    exchange = _calendar(
        calendar,
        start=(timestamp - pd.Timedelta(days=14)).date().isoformat(),
        end=(timestamp + pd.Timedelta(days=1)).date().isoformat(),
    )
    session = pd.Timestamp(
        exchange.date_to_session(timestamp.normalize(), direction="previous")
    )
    if session.tzinfo is not None:
        session = session.tz_localize(None)
    return session.normalize()


def xnys_session_on_or_after(
    value: str | pd.Timestamp,
    *,
    calendar: Any | None = None,
) -> pd.Timestamp:
    """Resolve a calendar date to the same or next XNYS session."""
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_localize(None)
    exchange = _calendar(
        calendar,
        start=(timestamp - pd.Timedelta(days=14)).date().isoformat(),
        end=(timestamp + pd.Timedelta(days=14)).date().isoformat(),
    )
    session = pd.Timestamp(
        exchange.date_to_session(timestamp.normalize(), direction="next")
    )
    if session.tzinfo is not None:
        session = session.tz_localize(None)
    return session.normalize()


__all__ = [
    "is_xnys_session",
    "latest_completed_xnys_session",
    "latest_publishable_xnys_session",
    "xnys_session_on_or_after",
    "xnys_session_on_or_before",
]
