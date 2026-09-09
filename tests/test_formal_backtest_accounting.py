"""Economic invariants at the formal four-price entry points."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.backtest.double_sort import double_sort_backtest
from src.backtest.metrics import max_drawdown
from src.backtest.quintile import BacktestCapacityError
from src.backtest.quintile_v2 import quintile_backtest_v2


def _inputs():
    dates = pd.bdate_range("2026-01-05", periods=8)
    factor = pd.DataFrame(np.tile([1., 2., 3., 4.], (8, 1)), index=dates, columns=list("ABCD"))
    opens = pd.DataFrame(100., index=dates, columns=factor.columns)
    opens["A"] = [100., 100., 120., 132., 132., 132., 132., 132.]
    return factor, opens, {
        "execution_open_df": opens,
        "execution_close_df": opens,
        "total_return_open_df": opens,
        "total_return_close_df": opens,
        "volume_df": opens * 10_000_000,
        "tradable_mask": opens.notna(),
        "execution": {"fee_model": "simple_bps", "commission_bps": 0.,
                      "slippage_model": "none", "portfolio_value": 1000.},
        "rebalance_days": 3,
        "rebalance_mode": "every_n_days",
    }


def test_formal_returns_positions_and_rebalances_share_drifting_capital():
    factor, opens, kwargs = _inputs()
    result = quintile_backtest_v2(factor, opens.pct_change(), n_groups=2,
                                benchmark_returns=pd.Series(0., index=opens.index), **kwargs)
    dates = opens.index
    assert result.group_daily_returns.loc[dates[2], "Q1"] == pytest.approx(60 / 110 * .1)
    positions = result.position_daily.set_index(["date", "group", "ticker"])
    assert positions.loc[(str(dates[2].date()), "Q1", "A"), "start_weight"] == pytest.approx(600 / 1100)
    rebalance = result.trades_detail.loc[
        result.trades_detail["date"].eq(str(dates[4].date())) & result.trades_detail["group"].eq("Q1")]
    assert dict(zip(rebalance["ticker"], rebalance["side"])) == {"A": "SELL", "B": "BUY"}
    assert rebalance["estimated_notional"].to_list() == pytest.approx([80., 80.])
    assert result.portfolio_daily["accounting_error"].abs().max() < 1e-8
    contributions = result.position_daily.groupby(["date", "group"])["contribution_return"].sum()
    for (date, group), contribution in contributions.items():
        assert contribution == pytest.approx(result.gross_group_returns.loc[pd.Timestamp(date), group])


@pytest.mark.parametrize("direction", [-1, 1])
def test_formal_long_short_charges_both_legs_in_both_directions(direction):
    factor, opens, kwargs = _inputs()
    opens.loc[:, :] = 100.
    kwargs["execution"]["commission_bps"] = 10.
    result = quintile_backtest_v2(factor, opens.pct_change(), n_groups=2, factor_direction=direction,
                                benchmark_returns=pd.Series(0., index=opens.index), **kwargs)
    first = result.long_short_returns.first_valid_index()
    # Cost reserves leave 1000 / 1.001 invested, with both accounts paying fees.
    assert result.long_short_returns.loc[first] == pytest.approx(-2 * .001 / 1.001)
    pd.testing.assert_series_equal(result.long_short_returns,
        ((result.gross_group_returns.Q2 - result.gross_group_returns.Q1) * direction
         - result.cost_returns.Q2 - result.cost_returns.Q1).rename("LongShort"))


def test_formal_prices_keep_execution_and_dividend_attribution_separate():
    factor, opens, kwargs = _inputs()
    opens.loc[:, :] = 100.
    attribution = opens.copy()
    attribution.loc[opens.index[2]:, "A"] = 102.
    kwargs["total_return_open_df"] = attribution
    kwargs["total_return_close_df"] = attribution
    result = quintile_backtest_v2(factor, attribution.pct_change(), n_groups=2,
                                benchmark_returns=pd.Series(0., index=opens.index), **kwargs)
    assert result.group_daily_returns.loc[opens.index[1], "Q1"] == pytest.approx(.01)
    assert result.trades_detail["raw_price"].eq(100.).all()


def test_formal_stateful_orders_still_enforce_capacity():
    factor, opens, kwargs = _inputs()
    kwargs["volume_df"] = opens * 0 + 1.
    kwargs["execution"].update({"slippage_model": "volume_share", "slippage": {"volume_limit": .025}})
    with pytest.raises(BacktestCapacityError):
        quintile_backtest_v2(factor, opens.pct_change(), n_groups=2,
                            benchmark_returns=pd.Series(0., index=opens.index), **kwargs)


def test_double_sort_accepts_only_explicit_execution_prices():
    factor, opens, kwargs = _inputs()
    result = double_sort_backtest(factor, factor * 10., opens.pct_change(),
                                 n_control=1, n_factor=2, **kwargs)
    assert result.cell_daily_returns["C1_F1"].loc[opens.index[2]] == pytest.approx(60 / 110 * .1)
    assert not result.trades_detail.empty
    assert result.portfolio_daily["accounting_error"].abs().max() < 1e-8


@pytest.mark.parametrize("returns, expected", [([-.1, -.1], -.19), ([-.1, 0], -.1), ([.1, -.1], -.1)])
def test_drawdown_includes_initial_capital_in_metrics_and_both_charts(returns, expected):
    from src.visualization.plots_mpl import plot_drawdown_mpl
    from src.visualization.plots_plotly import plot_drawdown_plotly
    import matplotlib.pyplot as plt

    series = pd.Series(returns, index=pd.bdate_range("2026-01-05", periods=len(returns)))
    assert max_drawdown(series) == pytest.approx(expected)
    plotly_figure = plot_drawdown_plotly(series)
    assert min(plotly_figure.data[0].y) == pytest.approx(expected)
    figure = plot_drawdown_mpl(series)
    try:
        assert min(figure.axes[0].lines[0].get_ydata()) == pytest.approx(expected)
    finally:
        plt.close(figure)
