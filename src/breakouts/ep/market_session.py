"""EP clock adapter over the shared exchange calendar, not the live monitor."""
from dataclasses import dataclass
from datetime import datetime

from src.utils.market_calendar import _calendar
from .models import NEW_YORK


@dataclass(frozen=True)
class Session:
    opens_at: datetime
    closes_at: datetime
    expected_minutes: int


def xnys_session_schedule(day):
    calendar = _calendar()
    if not calendar.is_session(day):
        raise ValueError('NOT_AN_XNYS_SESSION')
    start = calendar.session_open(day).to_pydatetime().astimezone(NEW_YORK)
    end = calendar.session_close(day).to_pydatetime().astimezone(NEW_YORK)
    return Session(start, end, int((end - start).total_seconds() // 60))


def previous_xnys_sessions(day, count):
    calendar, result = _calendar(), []
    if not 1 <= count <= 100 or not calendar.is_session(day):
        raise ValueError('VALID_SESSION_AND_BOUNDED_HISTORY_REQUIRED')
    for _ in range(count):
        day = calendar.previous_session(day)
        result.append(day.date().isoformat())
    return list(reversed(result))


def expected_source_session(day):
    return previous_xnys_sessions(day, 1)[0]
