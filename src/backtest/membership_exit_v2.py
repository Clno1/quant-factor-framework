"""Point-in-time membership exits with explicit execution/return price units."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.backtest.quintile import _safe_lookup


def apply_membership_exit_policy_v2(
    returns: pd.DataFrame,
    held_assignment: pd.DataFrame,
    *,
    membership_mask: pd.DataFrame | None,
    membership_events: pd.DataFrame | None,
    rebalance_dates: pd.DatetimeIndex,
    execution_open_df: pd.DataFrame | None,
    execution_close_df: pd.DataFrame | None,
    total_return_open_df: pd.DataFrame | None,
    total_return_close_df: pd.DataFrame | None,
    policy: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Liquidate PIT removals without mixing fill prices and PnL prices.

    ``raw_price`` in the event ledger is always an actually executable market
    price.  The return written into the portfolio ledger is always a total-return
    ratio, except the explicit reviewed total-loss write-off.  If either side of
    that total-return ratio is unavailable the engine fails closed rather than
    falling back to a raw-price return that would silently omit dividends.
    """
    adjusted = returns.copy()
    if membership_mask is None or policy == "fail" or adjusted.empty:
        return adjusted, pd.DataFrame()

    dates = pd.DatetimeIndex(adjusted.index)
    columns = pd.Index(adjusted.columns)
    held = held_assignment.reindex(index=dates, columns=columns)
    membership = membership_mask.reindex(
        index=dates,
        columns=columns,
        fill_value=False,
    ).astype(bool)

    boundaries = {0, len(dates)}
    for decision_date in rebalance_dates:
        position = int(dates.searchsorted(decision_date, side="right"))
        if 0 < position < len(dates):
            boundaries.add(position)
    ordered = sorted(boundaries)
    cash_mask = pd.DataFrame(False, index=dates, columns=columns)
    for begin, finish in zip(ordered[:-1], ordered[1:]):
        if begin >= finish:
            continue
        segment_membership = membership.iloc[begin:finish]
        # A membership snapshot becomes knowable at that session's close. The
        # resulting exit can execute at the following open, so cash starts on
        # the next modeled interval, never on the observation row itself.
        exited = (~segment_membership).cummax().shift(1, fill_value=False)
        cash_mask.iloc[begin:finish] = held.iloc[begin:finish].notna() & exited
    adjusted = adjusted.mask(cash_mask, 0.0)

    event_ledger = None
    if membership_events is not None and not membership_events.empty:
        event_ledger = membership_events.copy()
        event_ledger["effective_date"] = pd.to_datetime(
            event_ledger["effective_date"], errors="coerce"
        ).dt.normalize()
        event_ledger["removed_ticker"] = (
            event_ledger["removed_ticker"].fillna("").astype(str).str.upper()
        )

    def settlement_reason(ticker: str, effective_date: pd.Timestamp) -> tuple[str, str]:
        if event_ledger is None:
            raise ValueError(
                "Membership exit predates the last tradable bar but the "
                f"published version has no event ledger: {ticker} {effective_date.date()}"
            )
        candidates = event_ledger.loc[
            event_ledger["removed_ticker"].eq(ticker)
            & event_ledger["effective_date"].between(
                effective_date - pd.Timedelta(days=7),
                effective_date + pd.Timedelta(days=1),
            )
        ].copy()
        if candidates.empty:
            raise ValueError(
                "No version-bound PIT removal event matches a stale membership "
                f"exit: {ticker} {effective_date.date()}"
            )
        candidates["distance"] = (
            candidates["effective_date"] - effective_date
        ).abs()
        candidates = candidates.sort_values(["distance", "effective_date"])
        best_distance = candidates.iloc[0]["distance"]
        best = candidates.loc[candidates["distance"].eq(best_distance)]
        if len(best) != 1:
            raise ValueError(
                f"Ambiguous PIT removal events for {ticker} {effective_date.date()}"
            )
        reason = str(best.iloc[0]["reason"] or "").strip()
        normalized = reason.casefold()
        if any(
            marker in normalized
            for marker in ("fdic", "receivership", "bankruptcy", "bankrupt")
        ):
            return "TOTAL_LOSS_WRITE_OFF", reason
        if any(
            marker in normalized
            for marker in (
                "acquired",
                "acquisition",
                "acquiring",
                "merger",
                "merged",
                "combined",
            )
        ):
            return "LAST_TRADABLE_CLOSE", reason
        raise ValueError(
            "Stale membership exit has no reviewed settlement rule: "
            f"{ticker} {effective_date.date()} reason={reason!r}"
        )

    previous_membership = membership.shift(1, fill_value=False)
    terminal = (
        held.notna()
        & previous_membership
        & membership.eq(False)
        & ~cash_mask
    )
    event_rows: list[dict] = []
    for row_no, col_no in np.argwhere(terminal.to_numpy()):
        dt = dates[row_no]
        has_next_session = row_no + 1 < len(dates)
        next_dt = dates[row_no + 1] if has_next_session else dt
        ticker = str(columns[col_no])
        assignment = held.iat[row_no, col_no]
        reason = ""
        stale_sessions = 0
        group_size = int(held.loc[dt].eq(assignment).sum())
        if group_size <= 0:
            raise ValueError(
                f"Invalid held group size for membership exit: {dt.date()} {ticker}"
            )

        next_execution_open = (
            _safe_lookup(execution_open_df, next_dt, ticker)
            if has_next_session
            else None
        )
        terminal_date = dt
        if next_execution_open is not None:
            missing_start = row_no
            while (
                missing_start > 0
                and pd.isna(adjusted.iat[missing_start - 1, col_no])
            ):
                missing_start -= 1
            base_date = dates[missing_start]
            base_total_open = _safe_lookup(
                total_return_open_df,
                base_date,
                ticker,
            )
            next_total_open = _safe_lookup(
                total_return_open_df,
                next_dt,
                ticker,
            )
            if base_total_open is None or next_total_open is None:
                raise ValueError(
                    "Membership exit has executable next-open prices but lacks "
                    "the matching total-return open needed for dividend-aware PnL: "
                    f"ticker={ticker} base={base_date.date()} next={next_dt.date()}"
                )
            if missing_start < row_no:
                assignment = held.iat[missing_start, col_no]
                if pd.isna(assignment):
                    raise ValueError(
                        "Halted membership exit has no modeled owner at its last "
                        f"observable open: date={base_date.date()} ticker={ticker}"
                    )
                group_size = int(held.loc[base_date].eq(assignment).sum())
                if group_size <= 0:
                    raise ValueError(
                        "Invalid held group size at the last observable open: "
                        f"date={base_date.date()} ticker={ticker}"
                    )
                stale_sessions = row_no - missing_start
                adjusted.iloc[missing_start:row_no, col_no] = 0.0
            adjusted.iat[row_no, col_no] = (
                next_total_open / base_total_open - 1.0
            )
            execution_date = next_dt
            decision_date = dt
            raw_price = next_execution_open
            pricing_method = "NEXT_OPEN"
        else:
            terminal_row = row_no
            current_execution_open = _safe_lookup(
                execution_open_df, dates[terminal_row], ticker
            )
            final_execution_close = _safe_lookup(
                execution_close_df, dates[terminal_row], ticker
            )
            while current_execution_open is None and terminal_row > 0:
                terminal_row -= 1
                current_execution_open = _safe_lookup(
                    execution_open_df, dates[terminal_row], ticker
                )
                final_execution_close = _safe_lookup(
                    execution_close_df, dates[terminal_row], ticker
                )

            terminal_assignment = held.iat[terminal_row, col_no]
            if current_execution_open is None:
                raise ValueError(
                    "Membership exit has no executable next open or final tradable "
                    f"close: date={dt.date()} ticker={ticker}"
                )
            if pd.isna(terminal_assignment):
                raise ValueError(
                    "Membership exit has no modeled holding at its last tradable "
                    f"open: date={dates[terminal_row].date()} ticker={ticker}"
                )
            assignment = terminal_assignment
            group_size = int(held.loc[dates[terminal_row]].eq(assignment).sum())
            if group_size <= 0:
                raise ValueError(
                    "Invalid held group size at the last tradable open: "
                    f"date={dates[terminal_row].date()} ticker={ticker}"
                )
            stale_sessions = row_no - terminal_row
            terminal_date = dates[terminal_row]
            current_total_open = _safe_lookup(
                total_return_open_df, terminal_date, ticker
            )
            if final_execution_close is None:
                raise ValueError(
                    "Membership exit has no final tradable close: "
                    f"date={terminal_date.date()} ticker={ticker}"
                )
            pricing_method = "LAST_TRADABLE_CLOSE"
            if stale_sessions > 0:
                pricing_method, reason = settlement_reason(ticker, dt)

            # The final tradable price is historical settlement evidence. It
            # becomes actionable only when the False membership state is
            # observed on dt, so the cumulative outcome is booked on dt and is
            # never backdated into an earlier NAV.
            adjusted.iloc[terminal_row:row_no, col_no] = 0.0
            if pricing_method == "TOTAL_LOSS_WRITE_OFF":
                adjusted.iat[row_no, col_no] = -1.0
                raw_price = 0.0
            else:
                final_total_close = _safe_lookup(
                    total_return_close_df, terminal_date, ticker
                )
                if current_total_open is None or final_total_close is None:
                    raise ValueError(
                        "Membership exit final-close settlement lacks matching "
                        "total-return prices; refusing a raw-price PnL fallback: "
                        f"ticker={ticker} date={terminal_date.date()}"
                    )
                adjusted.iat[row_no, col_no] = (
                    final_total_close / current_total_open - 1.0
                )
                # Execution accounting still uses the actual raw/split-adjusted
                # close, never the dividend-adjusted attribution price.
                raw_price = final_execution_close
            execution_date = dt
            decision_date = dt

        event_rows.append(
            {
                "decision_date": decision_date,
                "execution_date": execution_date,
                "terminal_date": terminal_date,
                "ticker": ticker,
                "assignment": float(assignment),
                "target_weight": 1.0 / group_size,
                "raw_price": float(raw_price),
                "pricing_method": pricing_method,
                "effective_exit_date": dt,
                "reason": reason,
                "stale_sessions": stale_sessions,
            }
        )

    if len(dates):
        adjusted.loc[dates[-1]] = adjusted.loc[dates[-1]].mask(cash_mask.loc[dates[-1]])
    return adjusted, pd.DataFrame(event_rows)


__all__ = ["apply_membership_exit_policy_v2"]
