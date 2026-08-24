"""Audited forward economic outcomes for IC research.

Ordinary total returns are compounded from the published total-return series.
When that path disappears inside a requested horizon, only a version-bound,
reviewed membership event may resolve it. Unknown gaps remain unresolved so the
IC layer invalidates the complete cross-section instead of selecting survivors.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.ic import compute_forward_returns


TOTAL_LOSS_MARKERS = ("fdic", "receivership", "bankruptcy", "bankrupt")
ACQUISITION_MARKERS = (
    "acquired",
    "acquisition",
    "acquiring",
    "merger",
    "merged",
    "combined",
)


@dataclass(frozen=True)
class ForwardOutcomeResult:
    returns: pd.DataFrame
    audit: pd.DataFrame
    summary: dict[str, Any]


def _events(membership_events: pd.DataFrame | None) -> pd.DataFrame:
    if membership_events is None or membership_events.empty:
        return pd.DataFrame(columns=["effective_date", "removed_ticker", "reason"])
    required = {"effective_date", "removed_ticker", "reason"}
    missing = sorted(required - set(membership_events.columns))
    if missing:
        raise ValueError(f"Membership outcome events are missing columns: {missing}")
    events = membership_events.copy()
    events["effective_date"] = pd.to_datetime(
        events["effective_date"], errors="coerce"
    ).dt.normalize()
    events["removed_ticker"] = (
        events["removed_ticker"].fillna("").astype(str).str.strip().str.upper()
    )
    events["reason"] = events["reason"].fillna("").astype(str).str.strip()
    if events["effective_date"].isna().any() or events["removed_ticker"].eq("").any():
        raise ValueError("Membership outcome events contain invalid dates or tickers")
    return events.sort_values(["effective_date", "removed_ticker"]).reset_index(drop=True)


def _settlement_method(reason: str) -> str | None:
    normalized = str(reason or "").casefold()
    if any(marker in normalized for marker in TOTAL_LOSS_MARKERS):
        return "TOTAL_LOSS_WRITE_OFF"
    if any(marker in normalized for marker in ACQUISITION_MARKERS):
        return "LAST_TRADABLE_TOTAL_RETURN_CLOSE"
    return None


def build_forward_outcomes(
    returns_df: pd.DataFrame,
    *,
    total_return_close_df: pd.DataFrame,
    eligible_mask: pd.DataFrame,
    membership_events: pd.DataFrame | None,
    periods: int,
) -> ForwardOutcomeResult:
    """Resolve t+1..t+N outcomes and retain every exceptional observation."""
    if periods <= 0:
        raise ValueError("periods must be positive")
    common_dates = returns_df.index.intersection(total_return_close_df.index)
    common_cols = returns_df.columns.intersection(total_return_close_df.columns)
    returns = returns_df.reindex(index=common_dates, columns=common_cols)
    closes = total_return_close_df.reindex(index=common_dates, columns=common_cols)
    eligible = eligible_mask.reindex(
        index=common_dates,
        columns=common_cols,
        fill_value=False,
    ).astype(bool)
    resolved = compute_forward_returns(returns, periods=periods)
    events = _events(membership_events)
    events_by_ticker = {
        ticker: frame.reset_index(drop=True)
        for ticker, frame in events.groupby("removed_ticker", sort=False)
    }

    audit_rows: list[dict[str, Any]] = []
    complete_count = 0
    right_edge_count = 0
    resolved_count = 0
    unresolved_count = 0
    dates = pd.DatetimeIndex(common_dates)
    for row_no, dt in enumerate(dates):
        horizon_no = row_no + periods
        horizon_date = dates[horizon_no] if horizon_no < len(dates) else None
        for col_no, ticker_value in enumerate(common_cols):
            if not bool(eligible.iat[row_no, col_no]):
                continue
            ticker = str(ticker_value).upper()
            base_value = resolved.iat[row_no, col_no]
            if pd.notna(base_value):
                complete_count += 1
                continue
            common = {
                "decision_date": pd.Timestamp(dt),
                "horizon_date": horizon_date,
                "ticker": ticker,
                "periods": int(periods),
            }
            if horizon_date is None:
                right_edge_count += 1
                audit_rows.append(
                    {
                        **common,
                        "status": "RIGHT_EDGE_NOT_YET_OBSERVABLE",
                        "outcome": np.nan,
                        "settlement_method": None,
                        "settlement_date": pd.NaT,
                        "event_reason": None,
                    }
                )
                continue

            ticker_events = events_by_ticker.get(ticker)
            candidates = (
                ticker_events.loc[
                    ticker_events["effective_date"].gt(dt)
                    & ticker_events["effective_date"].le(horizon_date)
                ]
                if ticker_events is not None
                else pd.DataFrame()
            )
            if candidates.empty:
                unresolved_count += 1
                audit_rows.append(
                    {
                        **common,
                        "status": "UNRESOLVED_MISSING_OUTCOME",
                        "outcome": np.nan,
                        "settlement_method": None,
                        "settlement_date": pd.NaT,
                        "event_reason": None,
                    }
                )
                continue
            event = candidates.sort_values("effective_date").iloc[0]
            reason = str(event["reason"] or "")
            method = _settlement_method(reason)
            outcome = np.nan
            settlement_date = pd.NaT
            if method == "TOTAL_LOSS_WRITE_OFF":
                outcome = -1.0
                settlement_date = pd.Timestamp(event["effective_date"])
            elif method == "LAST_TRADABLE_TOTAL_RETURN_CLOSE":
                start_price = pd.to_numeric(
                    pd.Series([closes.iat[row_no, col_no]]), errors="coerce"
                ).iloc[0]
                path = pd.to_numeric(
                    closes.loc[
                        (closes.index > dt)
                        & (closes.index <= pd.Timestamp(event["effective_date"])),
                        ticker_value,
                    ],
                    errors="coerce",
                ).dropna()
                path = path.loc[np.isfinite(path) & path.gt(0)]
                if pd.notna(start_price) and float(start_price) > 0 and not path.empty:
                    settlement_date = pd.Timestamp(path.index[-1])
                    outcome = float(path.iloc[-1]) / float(start_price) - 1.0

            if np.isfinite(outcome) and float(outcome) >= -1.0:
                resolved.iat[row_no, col_no] = float(outcome)
                resolved_count += 1
                status = "RESOLVED_REVIEWED_EVENT"
            else:
                unresolved_count += 1
                status = "UNRESOLVED_EVENT_SETTLEMENT"
            audit_rows.append(
                {
                    **common,
                    "status": status,
                    "outcome": float(outcome) if np.isfinite(outcome) else np.nan,
                    "settlement_method": method,
                    "settlement_date": settlement_date,
                    "event_reason": reason,
                }
            )

    audit = pd.DataFrame(audit_rows)
    summary = {
        "schema_version": 1,
        "periods": int(periods),
        "eligible_observations": int(eligible.sum().sum()),
        "ordinary_complete": int(complete_count),
        "resolved_reviewed_events": int(resolved_count),
        "right_edge_not_observable": int(right_edge_count),
        "unresolved_missing_outcomes": int(unresolved_count),
    }
    return ForwardOutcomeResult(returns=resolved, audit=audit, summary=summary)


__all__ = ["ForwardOutcomeResult", "build_forward_outcomes"]
