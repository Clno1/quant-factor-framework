"""XNYS session resolution for fail-closed daily snapshot validation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


@dataclass(frozen=True, slots=True)
class XnysSessionSchedule:
    session_date: str
    opens_at: datetime
    closes_at: datetime
    expected_minutes: int


def _exchange_calendar(calendar: Any | None = None) -> Any:
    if calendar is not None:
        return calendar
    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise RuntimeError(
            "exchange-calendars is required for XNYS session validation"
        ) from exc
    return xcals.get_calendar("XNYS")


def expected_source_session(
    session_date: str,
    *,
    calendar: Any | None = None,
) -> str:
    label = pd.Timestamp(session_date).normalize()
    if label.strftime("%Y-%m-%d") != str(session_date):
        raise ValueError("session_date must use YYYY-MM-DD")
    calendar = _exchange_calendar(calendar)
    if not bool(calendar.is_session(label)):
        raise ValueError(f"{session_date} is not an XNYS trading session")
    previous = pd.Timestamp(calendar.previous_session(label))
    if previous.tzinfo is not None:
        previous = previous.tz_localize(None)
    return previous.normalize().strftime("%Y-%m-%d")


def xnys_session_schedule(
    session_date: str,
    *,
    timezone: str = "America/New_York",
    calendar: Any | None = None,
) -> XnysSessionSchedule:
    label = pd.Timestamp(session_date).normalize()
    if label.strftime("%Y-%m-%d") != str(session_date):
        raise ValueError("session_date must use YYYY-MM-DD")
    exchange = _exchange_calendar(calendar)
    if not bool(exchange.is_session(label)):
        raise ValueError(f"{session_date} is not an XNYS trading session")
    opens = pd.Timestamp(exchange.session_open(label))
    closes = pd.Timestamp(exchange.session_close(label))
    opens = opens.tz_localize("UTC") if opens.tzinfo is None else opens.tz_convert("UTC")
    closes = closes.tz_localize("UTC") if closes.tzinfo is None else closes.tz_convert("UTC")
    zone = ZoneInfo(timezone)
    expected = max(1, int((closes - opens) / pd.Timedelta(minutes=1)))
    return XnysSessionSchedule(
        session_date=session_date,
        opens_at=opens.to_pydatetime().astimezone(zone),
        closes_at=closes.to_pydatetime().astimezone(zone),
        expected_minutes=expected,
    )


def previous_xnys_sessions(
    session_date: str,
    count: int,
    *,
    calendar: Any | None = None,
) -> list[str]:
    if count < 1:
        return []
    label = pd.Timestamp(session_date).normalize()
    exchange = _exchange_calendar(calendar)
    if bool(exchange.is_session(label)):
        current = pd.Timestamp(exchange.previous_session(label))
    else:
        current = pd.Timestamp(exchange.date_to_session(label, direction="previous"))
    sessions = [current]
    while len(sessions) < count:
        sessions.append(pd.Timestamp(exchange.previous_session(sessions[-1])))
    normalized = []
    for value in reversed(sessions):
        if value.tzinfo is not None:
            value = value.tz_localize(None)
        normalized.append(value.normalize().strftime("%Y-%m-%d"))
    return normalized


__all__ = [
    "XnysSessionSchedule",
    "expected_source_session",
    "previous_xnys_sessions",
    "xnys_session_schedule",
]
