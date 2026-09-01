from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.backtest.double_sort import double_sort_backtest


def test_double_sort_rejects_current_market_cap_snapshot_layout():
    dates = pd.date_range("2026-01-05", periods=5, freq="B")
    tickers = ["A", "B", "C", "D"]
    factor = pd.DataFrame(
        np.tile(np.arange(4, dtype=float), (len(dates), 1)),
        index=dates,
        columns=tickers,
    )
    current_snapshot = pd.DataFrame(
        {"market_cap": [1.0, 2.0, 3.0, 4.0]},
        index=tickers,
    )

    with pytest.raises(ValueError, match="PIT date x ticker control matrix"):
        double_sort_backtest(
            factor,
            current_snapshot,
            factor * 0.0,
            open_df=pd.DataFrame(100.0, index=dates, columns=tickers),
        )


def test_double_sort_uses_stateful_cell_portfolios_and_real_rebalance_orders():
    dates = pd.date_range("2026-01-05", periods=8, freq="B")
    tickers = ["A", "B", "C", "D"]
    factor = pd.DataFrame(
        np.tile(np.arange(4, dtype=float), (len(dates), 1)),
        index=dates,
        columns=tickers,
    )
    pit_market_cap = pd.DataFrame(
        np.tile([10.0, 20.0, 30.0, 40.0], (len(dates), 1)),
        index=dates,
        columns=tickers,
    )
    opens = pd.DataFrame(
        {
            "A": [100, 100, 120, 132, 132, 132, 132, 132],
            "B": [100] * len(dates),
            "C": [100] * len(dates),
            "D": [100] * len(dates),
        },
        index=dates,
        dtype=float,
    )
    result = double_sort_backtest(
        factor,
        pit_market_cap,
        opens.pct_change(fill_method=None),
        n_control=1,
        n_factor=2,
        rebalance_days=3,
        rebalance_mode="every_n_days",
        open_df=opens,
        price_df=opens,
        volume_df=pd.DataFrame(1_000_000_000.0, index=dates, columns=tickers),
        tradable_mask=pd.DataFrame(True, index=dates, columns=tickers),
        execution={
            "fee_model": "simple_bps",
            "commission_bps": 0.0,
            "slippage_model": "none",
        },
    )

    low_cell = result.cell_daily_returns["C1_F1"]
    assert low_cell.loc[dates[2]] == pytest.approx((60.0 / 110.0) * 0.10)
    second_rebalance = result.trades_detail.loc[
        result.trades_detail["date"].eq(dates[4].strftime("%Y-%m-%d"))
        & result.trades_detail["group"].eq("C1_F1")
    ]
    assert set(second_rebalance["ticker"]) == {"A", "B"}
    assert second_rebalance["trade_abs_weight"].sum() > 0
    assert result.portfolio_daily["accounting_error"].abs().max() < 1e-8


def test_double_sort_spread_pays_both_cell_costs():
    dates = pd.date_range("2026-01-05", periods=6, freq="B")
    tickers = ["A", "B", "C", "D"]
    factor = pd.DataFrame(
        np.tile(np.arange(4, dtype=float), (len(dates), 1)),
        index=dates,
        columns=tickers,
    )
    control = pd.DataFrame(
        np.tile([10.0, 20.0, 30.0, 40.0], (len(dates), 1)),
        index=dates,
        columns=tickers,
    )
    opens = pd.DataFrame(100.0, index=dates, columns=tickers)
    result = double_sort_backtest(
        factor,
        control,
        opens.pct_change(fill_method=None),
        n_control=1,
        n_factor=2,
        rebalance_days=10,
        rebalance_mode="every_n_days",
        open_df=opens,
        price_df=opens,
        volume_df=pd.DataFrame(1_000_000_000.0, index=dates, columns=tickers),
        tradable_mask=pd.DataFrame(True, index=dates, columns=tickers),
        execution={
            "fee_model": "simple_bps",
            "commission_bps": 10.0,
            "slippage_model": "none",
        },
    )

    first = result.factor_returns.first_valid_index()
    assert result.factor_returns.loc[first] < 0
