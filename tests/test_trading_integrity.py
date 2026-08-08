from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.backtest.quintile import (
    _assign_groups_on_rebalance,
    _build_execution_details,
    _strict_equal_weight_group_returns,
    build_tradable_mask,
    quintile_backtest,
)
from src.execution import max_volume_fill_quantity, resolve_execution_config


def _matrix(
    dates: pd.DatetimeIndex,
    tickers: list[str],
    value: float,
) -> pd.DataFrame:
    return pd.DataFrame(value, index=dates, columns=tickers)


def test_rebalance_replaces_excluded_security_instead_of_forward_filling():
    dates = pd.date_range("2026-01-05", periods=6, freq="B")
    tickers = ["A", "B", "C", "D"]
    scores = pd.DataFrame(
        np.tile(np.arange(4, dtype=float), (len(dates), 1)),
        index=dates,
        columns=tickers,
    )
    tradable = _matrix(dates, tickers, True)
    tradable.loc[dates[2]:, "B"] = False

    assignment = _assign_groups_on_rebalance(
        scores,
        rebalance_days=2,
        n_groups=2,
        rebalance_mode="every_n_days",
        tradable_mask=tradable,
    )

    assert pd.notna(assignment.loc[dates[1], "B"])
    assert assignment.loc[dates[2]:, "B"].isna().all()


def test_active_portfolio_requires_complete_groups_at_each_rebalance():
    dates = pd.date_range("2026-01-05", periods=5, freq="B")
    scores = pd.DataFrame(
        {
            "A": [1.0, 1.0, 1.0, 1.0, 1.0],
            "B": [2.0, 2.0, np.nan, np.nan, np.nan],
        },
        index=dates,
    )

    with pytest.raises(ValueError, match="Insufficient eligible securities"):
        _assign_groups_on_rebalance(
            scores,
            rebalance_days=2,
            n_groups=2,
            rebalance_mode="every_n_days",
        )


def test_tradable_mask_does_not_peek_at_tomorrows_open():
    dates = pd.date_range("2026-01-05", periods=25, freq="B")
    tickers = ["A", "B"]
    returns = _matrix(dates, tickers, 0.01)
    prices = _matrix(dates, tickers, 100.0)
    volumes = _matrix(dates, tickers, 1_000_000.0)
    complete_open = _matrix(dates, tickers, 100.0)
    future_missing_open = complete_open.copy()
    future_missing_open.loc[dates[-1], "A"] = np.nan

    complete = build_tradable_mask(
        index=dates,
        columns=tickers,
        returns_df=returns,
        price_df=prices,
        open_df=complete_open,
        volume_df=volumes,
        timing="next_open",
    )
    missing = build_tradable_mask(
        index=dates,
        columns=tickers,
        returns_df=returns,
        price_df=prices,
        open_df=future_missing_open,
        volume_df=volumes,
        timing="next_open",
    )

    pd.testing.assert_frame_equal(complete, missing)


def test_missing_held_return_is_not_renormalized():
    dates = pd.date_range("2026-01-05", periods=3, freq="B")
    returns = pd.DataFrame(
        {"A": [0.01, 0.02, 0.03], "B": [0.01, np.nan, 0.03]},
        index=dates,
    )
    held = pd.DataFrame(1.0, index=dates, columns=["A", "B"])

    with pytest.raises(ValueError, match="Missing return for held securities"):
        _strict_equal_weight_group_returns(
            returns,
            held,
            n_groups=1,
            timing="next_open",
        )


def test_same_day_close_execution_is_rejected():
    dates = pd.date_range("2026-01-05", periods=5, freq="B")
    tickers = ["A", "B", "C", "D"]
    scores = pd.DataFrame(
        np.tile(np.arange(4, dtype=float), (len(dates), 1)),
        index=dates,
        columns=tickers,
    )

    with pytest.raises(ValueError, match="Only execution.timing='next_open'"):
        quintile_backtest(
            scores,
            _matrix(dates, tickers, 0.01),
            n_groups=2,
            open_df=_matrix(dates, tickers, 100.0),
            execution={"timing": "close"},
        )


def test_backtest_refuses_ex_post_direction_selection():
    dates = pd.date_range("2026-01-05", periods=5, freq="B")
    tickers = ["A", "B", "C", "D"]
    scores = pd.DataFrame(
        np.tile(np.arange(4, dtype=float), (len(dates), 1)),
        index=dates,
        columns=tickers,
    )

    with pytest.raises(ValueError, match="fixed ex ante"):
        quintile_backtest(
            scores,
            _matrix(dates, tickers, 0.01),
            factor_direction=0,
            n_groups=2,
            open_df=_matrix(dates, tickers, 100.0),
        )


@pytest.mark.parametrize(
    "override",
    [
        {"commission_bps": -1},
        {"slippage_bps": float("nan")},
        {"portfolio_value": 0},
        {"slippage": {"volume_limit": 1.01}},
        {"slippage": {"adv_window": 2.5}},
        {"fees": {"include_regulatory": "not-a-boolean"}},
    ],
)
def test_execution_configuration_rejects_unrealistic_values(override):
    with pytest.raises(ValueError):
        resolve_execution_config(override)


def test_execution_refuses_missing_open_and_uses_trailing_adv():
    dates = pd.date_range("2026-01-05", periods=4, freq="B")
    assignment = pd.DataFrame(
        {"A": [1.0] * 4, "B": [2.0] * 4},
        index=dates,
    )
    execution = resolve_execution_config({
        "timing": "next_open",
        "portfolio_value": 10.0,
        "fee_model": "simple_bps",
        "commission_bps": 0.0,
        "slippage_model": "volume_share",
        "slippage": {
            "volume_limit": 0.025,
            "price_impact": 0.0,
            "spread_bps": 0.0,
            "adv_window": 20,
        },
    })
    opens = _matrix(dates, ["A", "B"], 10.0)
    volumes = _matrix(dates, ["A", "B"], 100.0)
    volumes.loc[dates[2], :] = 1_000_000_000.0

    _, trades, _ = _build_execution_details(
        assignment,
        dates,
        pd.DatetimeIndex([dates[1]]),
        2,
        execution=execution,
        execution_price_df=opens,
        volume_df=volumes,
    )
    assert set(trades["bar_volume"]) == {100.0}
    assert set(trades["volume_reference"]) == {"ADV20_asof_decision"}

    opens.loc[dates[2], "A"] = np.nan
    with pytest.raises(ValueError, match="Missing execution price"):
        _build_execution_details(
            assignment,
            dates,
            pd.DatetimeIndex([dates[1]]),
            2,
            execution=execution,
            execution_price_df=opens,
            volume_df=volumes,
        )


def test_volume_share_without_volume_has_zero_fill_capacity():
    execution = resolve_execution_config({
        "slippage_model": "volume_share",
        "slippage": {"volume_limit": 0.025},
    })
    assert max_volume_fill_quantity(
        requested_quantity=100,
        volume=None,
        execution=execution,
    ) == 0.0


def test_penultimate_signal_does_not_create_truncated_horizon_trade():
    dates = pd.date_range("2026-01-05", periods=4, freq="B")
    assignment = pd.DataFrame(
        {"A": [1.0] * 4, "B": [2.0] * 4},
        index=dates,
    )
    execution = resolve_execution_config({
        "portfolio_value": 100.0,
        "fee_model": "simple_bps",
        "commission_bps": 0.0,
        "slippage_model": "constant_bps",
        "slippage_bps": 0.0,
    })

    _, trades, costs = _build_execution_details(
        assignment,
        dates,
        pd.DatetimeIndex([dates[-2]]),
        2,
        execution=execution,
        execution_price_df=_matrix(dates, ["A", "B"], 10.0),
        volume_df=_matrix(dates, ["A", "B"], 1_000_000.0),
    )

    assert trades.empty
    assert costs.empty
