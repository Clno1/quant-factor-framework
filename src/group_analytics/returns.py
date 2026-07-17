"""Pure end-of-day return calculations for group analytics.

The functions in this module deliberately work on the shared market-session
axis.  They never drop missing observations per security and never forward
fill, so a price gap cannot be converted into a zero return.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .models import ReasonCode, sorted_reason_codes


AsOf = str | date | datetime | pd.Timestamp | None


@dataclass(frozen=True, slots=True)
class AdjacentSessions:
    """The two market-wide rows used for an EOD return calculation."""

    asof: pd.Timestamp
    previous: pd.Timestamp | None
    current_prices: pd.Series
    previous_prices: pd.Series


def _normalized_session(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("session cannot be NaT")
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("America/New_York").tz_localize(None)
    return timestamp.normalize()


def adjacent_sessions(adj_close: pd.DataFrame, *, asof: AsOf = None) -> AdjacentSessions:
    """Select *shared-axis* current and previous sessions from ``adj_close``.

    ``asof`` is matched by calendar session, not by a security's last non-null
    value.  With fewer than two sessions, ``previous_prices`` is all-null;
    callers therefore receive null returns instead of an exception or fill.
    """

    if not isinstance(adj_close, pd.DataFrame):
        raise TypeError("adj_close must be a pandas DataFrame")
    if adj_close.empty or len(adj_close.index) == 0:
        raise ValueError("adj_close must contain at least one market session")
    if adj_close.columns.has_duplicates:
        raise ValueError("adj_close columns must be unique")

    frame = adj_close.copy(deep=False)
    normalized_index = pd.Index(
        [_normalized_session(value) for value in frame.index],
        name="session",
    )
    if normalized_index.has_duplicates:
        raise ValueError("adj_close must contain at most one row per market session")

    frame = frame.copy()
    frame.index = normalized_index
    frame = frame.sort_index()
    target = frame.index[-1] if asof is None else _normalized_session(asof)
    if target not in frame.index:
        raise KeyError(f"asof session {target.date().isoformat()} is unavailable")

    position = int(frame.index.get_loc(target))
    current = pd.to_numeric(frame.iloc[position], errors="coerce").astype(float)
    if position == 0:
        previous_session: pd.Timestamp | None = None
        previous = pd.Series(np.nan, index=frame.columns, dtype=float)
    else:
        previous_session = frame.index[position - 1]
        previous = pd.to_numeric(frame.iloc[position - 1], errors="coerce").astype(float)

    current.name = target
    previous.name = previous_session
    return AdjacentSessions(
        asof=target,
        previous=previous_session,
        current_prices=current,
        previous_prices=previous,
    )


def compute_eod_returns(adj_close: pd.DataFrame, *, asof: AsOf = None) -> pd.Series:
    """Return ``AdjClose[t] / AdjClose[t-1] - 1`` without any filling.

    Non-finite, zero, or negative adjusted closes are invalid and yield NaN.
    The previous row is the immediately preceding shared market session even
    when a particular security is missing on that row.
    """

    selected = adjacent_sessions(adj_close, asof=asof)
    current = selected.current_prices
    previous = selected.previous_prices
    valid = (
        np.isfinite(current.to_numpy(dtype=float))
        & np.isfinite(previous.to_numpy(dtype=float))
        & current.gt(0).to_numpy()
        & previous.gt(0).to_numpy()
    )
    result = pd.Series(np.nan, index=current.index, dtype=float, name="raw_return_1d")
    if valid.any():
        valid_index = current.index[valid]
        result.loc[valid_index] = (
            current.loc[valid_index] / previous.loc[valid_index] - 1.0
        )
    result = result.where(np.isfinite(result))
    result.attrs.update(
        {
            "asof": selected.asof.date().isoformat(),
            "previous_session": (
                selected.previous.date().isoformat() if selected.previous is not None else None
            ),
            "return_status": "FINAL",
        }
    )
    return result


def compute_eod_return_audit(
    adj_close: pd.DataFrame,
    *,
    asof: AsOf = None,
    delisting_return_required: pd.Series | Mapping[str, Any] | set[str] | None = None,
) -> pd.DataFrame:
    """Return prices, raw returns, validity, and sorted member reason codes."""

    selected = adjacent_sessions(adj_close, asof=asof)
    returns = compute_eod_returns(adj_close, asof=selected.asof)
    current = selected.current_prices
    previous = selected.previous_prices
    required = pd.Series(False, index=current.index, dtype=bool)
    if isinstance(delisting_return_required, pd.Series):
        values = delisting_return_required.copy()
        values.index = values.index.astype(str)
        required = values.reindex(current.index).fillna(False).astype(bool)
    elif isinstance(delisting_return_required, Mapping):
        values = pd.Series(dict(delisting_return_required))
        values.index = values.index.astype(str)
        required = values.reindex(current.index).fillna(False).astype(bool)
    elif delisting_return_required is not None:
        required.loc[
            required.index.astype(str).isin(
                {str(value) for value in delisting_return_required}
            )
        ] = True
    rows: list[dict[str, object]] = []
    for symbol in current.index:
        reasons: list[ReasonCode] = []
        current_value = current.at[symbol]
        previous_value = previous.at[symbol]
        if not np.isfinite(current_value) or current_value <= 0:
            reasons.append(ReasonCode.MISSING_PRICE)
        if not np.isfinite(previous_value) or previous_value <= 0:
            reasons.append(ReasonCode.MISSING_PREVIOUS_CLOSE)
        return_value = returns.at[symbol]
        if not np.isfinite(return_value):
            reasons.append(ReasonCode.MISSING_RETURN)
            if bool(required.at[symbol]):
                reasons.append(ReasonCode.MISSING_DELISTING_RETURN)
        rows.append(
            {
                "ticker": str(symbol),
                "adj_close_t": float(current_value) if np.isfinite(current_value) else np.nan,
                "adj_close_t_1": (
                    float(previous_value) if np.isfinite(previous_value) else np.nan
                ),
                "raw_return_1d": (
                    float(return_value) if np.isfinite(return_value) else np.nan
                ),
                "is_valid_return": bool(np.isfinite(return_value)),
                "data_asof": selected.asof.date().isoformat(),
                "previous_session": (
                    selected.previous.date().isoformat()
                    if selected.previous is not None
                    else None
                ),
                "return_status": "FINAL",
                "delisting_return_required": bool(required.at[symbol]),
                "reason_codes": sorted_reason_codes(reasons),
            }
        )
    return pd.DataFrame(rows).set_index("ticker", drop=False)


__all__ = [
    "AdjacentSessions",
    "adjacent_sessions",
    "compute_eod_return_audit",
    "compute_eod_returns",
]
