"""Rebalance calendar helpers."""
from __future__ import annotations

import pandas as pd


def get_rebalance_dates(
    index: pd.Index,
    *,
    mode: str = "every_n_days",
    step_days: int = 5,
) -> pd.DatetimeIndex:
    """Return rebalance decision dates from a trading-date index."""
    if len(index) == 0:
        return pd.DatetimeIndex([])

    dates = pd.DatetimeIndex(index).sort_values()
    mode = (mode or "every_n_days").lower()
    step_days = max(1, int(step_days or 1))

    if mode in ("every_n_days", "n_days", "interval"):
        return dates[::step_days]

    if mode in ("month_end", "monthly"):
        s = pd.Series(dates, index=dates)
        return pd.DatetimeIndex(s.groupby(dates.to_period("M")).max().values)

    if mode in ("week_end", "weekly"):
        s = pd.Series(dates, index=dates)
        return pd.DatetimeIndex(s.groupby(dates.to_period("W-FRI")).max().values)

    raise ValueError(
        f"Unknown rebalance mode={mode!r}; expected every_n_days/month_end/week_end."
    )


__all__ = ["get_rebalance_dates"]
