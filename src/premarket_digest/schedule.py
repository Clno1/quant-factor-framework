"""XNYS-aware resolution of the upcoming opening and its source session."""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .models import PremarketContext, ScheduleSkip
from .settings import PremarketDigestSettings


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _calendar(calendar: Any | None = None) -> Any:
    if calendar is not None:
        return calendar
    try:
        import exchange_calendars as xcals
    except ImportError as exc:  # pragma: no cover - deployment preflight
        raise RuntimeError("exchange-calendars is required for premarket scheduling") from exc
    return xcals.get_calendar("XNYS")


def _session_label(value: str) -> pd.Timestamp:
    if not _ISO_DATE.fullmatch(value):
        raise ValueError("session must be an ISO-8601 date (YYYY-MM-DD)")
    parsed = pd.Timestamp(value)
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError("session must be a valid ISO-8601 date")
    return parsed.normalize()


def resolve_premarket_context(
    settings: PremarketDigestSettings,
    *,
    now: datetime | None = None,
    requested_session: str | None = None,
    scheduled: bool = False,
    calendar: Any | None = None,
) -> PremarketContext:
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    now_et = now_utc.astimezone(ZoneInfo(settings.timezone))
    label = (
        _session_label(requested_session)
        if requested_session
        else pd.Timestamp(now_et.date()).normalize()
    )
    cal = _calendar(calendar)
    try:
        is_session = bool(cal.is_session(label))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Unable to resolve the XNYS session calendar") from exc
    if not is_session:
        raise ScheduleSkip(
            "SKIPPED_NON_SESSION",
            f"{label.date()} is not an XNYS trading session",
        )
    if scheduled:
        current = now_et.strftime("%H:%M")
        if not settings.scheduled_window_start <= current <= settings.scheduled_window_end:
            raise ScheduleSkip(
                "SKIPPED_OUTSIDE_WINDOW",
                "scheduled invocation is outside the "
                f"{settings.scheduled_window_start}-{settings.scheduled_window_end} "
                "ET premarket window",
            )
    try:
        previous = pd.Timestamp(cal.previous_session(label))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Unable to resolve the previous XNYS session") from exc
    if previous.tzinfo is not None:
        previous = previous.tz_localize(None)
    return PremarketContext(
        target_session=label.strftime("%Y-%m-%d"),
        source_session=previous.normalize().strftime("%Y-%m-%d"),
        now_utc=now_utc,
        now_et=now_et,
    )


__all__ = ["resolve_premarket_context"]
