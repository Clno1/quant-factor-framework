from __future__ import annotations

import pandas as pd

from src.utils.market_calendar import (
    latest_completed_xnys_session,
    latest_publishable_xnys_session,
    xnys_session_on_or_before,
)


def test_session_on_or_before_handles_weekend_at_calendar_upper_bound():
    assert xnys_session_on_or_before("2026-08-29") == pd.Timestamp("2026-08-28")


def test_session_on_or_before_handles_long_holiday_weekend():
    assert xnys_session_on_or_before("2026-07-05") == pd.Timestamp("2026-07-02")


def test_latest_completed_session_handles_weekend():
    assert latest_completed_xnys_session(
        now=pd.Timestamp("2026-08-30 04:00:00", tz="UTC")
    ) == pd.Timestamp("2026-08-28")


def test_latest_publishable_session_handles_weekend():
    assert latest_publishable_xnys_session(
        now=pd.Timestamp("2026-08-30 04:00:00", tz="UTC"),
        delay_minutes=120,
    ) == pd.Timestamp("2026-08-28")
