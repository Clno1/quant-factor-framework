"""XNYS session resolution for fail-closed daily snapshot validation."""
from __future__ import annotations

from typing import Any

import pandas as pd


def expected_source_session(
    session_date: str,
    *,
    calendar: Any | None = None,
) -> str:
    label = pd.Timestamp(session_date).normalize()
    if label.strftime("%Y-%m-%d") != str(session_date):
        raise ValueError("session_date must use YYYY-MM-DD")
    if calendar is None:
        try:
            import exchange_calendars as xcals
        except ImportError as exc:
            raise RuntimeError(
                "exchange-calendars is required for XNYS session validation"
            ) from exc
        calendar = xcals.get_calendar("XNYS")
    if not bool(calendar.is_session(label)):
        raise ValueError(f"{session_date} is not an XNYS trading session")
    previous = pd.Timestamp(calendar.previous_session(label))
    if previous.tzinfo is not None:
        previous = previous.tz_localize(None)
    return previous.normalize().strftime("%Y-%m-%d")
