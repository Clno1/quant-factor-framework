from __future__ import annotations

import pandas as pd
import pytest

from src.backtest.membership_exit_v2 import apply_membership_exit_policy_v2


def test_last_close_forced_exit_uses_raw_fill_but_total_return_pnl() -> None:
    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    cols = ["AAA"]
    # Missing ordinary next-open holding return forces LAST_TRADABLE_CLOSE.
    returns = pd.DataFrame([float("nan"), 0.0, float("nan")], index=dates, columns=cols)
    held = pd.DataFrame([1.0, 1.0, 1.0], index=dates, columns=cols)
    membership = pd.DataFrame([True, False, False], index=dates, columns=cols)

    execution_open = pd.DataFrame([100.0, float("nan"), float("nan")], index=dates, columns=cols)
    execution_close = pd.DataFrame([95.0, float("nan"), float("nan")], index=dates, columns=cols)
    # Attribution prices deliberately differ from raw prices. If the engine
    # regresses to raw close/open, return would be -5%; the correct total-return
    # settlement is 91/90 - 1.
    total_return_open = pd.DataFrame([90.0, float("nan"), float("nan")], index=dates, columns=cols)
    total_return_close = pd.DataFrame([91.0, float("nan"), float("nan")], index=dates, columns=cols)

    adjusted, events = apply_membership_exit_policy_v2(
        returns,
        held,
        membership_mask=membership,
        membership_events=None,
        rebalance_dates=pd.DatetimeIndex([dates[0]]),
        execution_open_df=execution_open,
        execution_close_df=execution_close,
        total_return_open_df=total_return_open,
        total_return_close_df=total_return_close,
        policy="next_open_or_last_close_to_cash",
    )

    assert adjusted.loc[dates[0], "AAA"] == pytest.approx(91.0 / 90.0 - 1.0)
    assert adjusted.loc[dates[0], "AAA"] != pytest.approx(95.0 / 100.0 - 1.0)
    assert len(events) == 1
    event = events.iloc[0]
    assert event["pricing_method"] == "LAST_TRADABLE_CLOSE"
    assert event["raw_price"] == pytest.approx(95.0)


def test_forced_exit_refuses_raw_pnl_fallback_when_total_return_price_missing() -> None:
    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    cols = ["AAA"]
    returns = pd.DataFrame([float("nan"), 0.0, float("nan")], index=dates, columns=cols)
    held = pd.DataFrame([1.0, 1.0, 1.0], index=dates, columns=cols)
    membership = pd.DataFrame([True, False, False], index=dates, columns=cols)
    execution_open = pd.DataFrame([100.0, float("nan"), float("nan")], index=dates, columns=cols)
    execution_close = pd.DataFrame([95.0, float("nan"), float("nan")], index=dates, columns=cols)
    total_return_open = pd.DataFrame([90.0, float("nan"), float("nan")], index=dates, columns=cols)
    total_return_close = pd.DataFrame([float("nan"), float("nan"), float("nan")], index=dates, columns=cols)

    with pytest.raises(ValueError, match="refusing a raw-price PnL fallback"):
        apply_membership_exit_policy_v2(
            returns,
            held,
            membership_mask=membership,
            membership_events=None,
            rebalance_dates=pd.DatetimeIndex([dates[0]]),
            execution_open_df=execution_open,
            execution_close_df=execution_close,
            total_return_open_df=total_return_open,
            total_return_close_df=total_return_close,
            policy="next_open_or_last_close_to_cash",
        )
