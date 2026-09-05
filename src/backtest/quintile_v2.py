"""Price-semantics-safe quantile backtest.

This is the formal backtest entry point for next-open research. Unlike the
legacy function, execution prices, total-return attribution prices and benchmark
returns are separate required inputs. This prevents dividend-adjusted prices
from leaking into fills/tradability/forced exits and prevents executable opens
from silently dropping dividends from holding PnL.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from src.backtest.membership_exit_v2 import apply_membership_exit_policy_v2
from src.backtest.metrics import performance_summary, relative_performance_summary
from src.backtest.portfolio import simulate_group_portfolios
from src.backtest.quintile import (
    BacktestCapacityError,
    QuintileResult,
    _assign_groups_on_rebalance,
    _resolve_execution,
    build_tradable_mask,
)
from src.backtest.rebalance import get_rebalance_dates
from src.config import CONFIG
from src.utils.logger import get_logger


log = get_logger(__name__)


def quintile_backtest_v2(
    factor_df: pd.DataFrame,
    returns_df: pd.DataFrame,
    n_groups: Optional[int] = None,
    rebalance_days: Optional[int] = None,
    factor_direction: int = +1,
    *,
    execution_open_df: pd.DataFrame,
    execution_close_df: pd.DataFrame,
    total_return_open_df: pd.DataFrame,
    total_return_close_df: pd.DataFrame,
    volume_df: pd.DataFrame | None = None,
    tradable_mask: pd.DataFrame | None = None,
    membership_mask: pd.DataFrame | None = None,
    membership_events: pd.DataFrame | None = None,
    benchmark_returns: pd.Series,
    rebalance_mode: str | None = None,
    execution: dict | None = None,
) -> QuintileResult:
    """Run a next-open quantile backtest with explicit economic price units.

    Parameters
    ----------
    returns_df
        Close-to-close total returns. Used only for decision-date history and
        tradability lookback checks.
    execution_open_df / execution_close_df
        Split-adjusted executable market prices. These are the only matrices
        allowed for fills, price floors, dollar volume, ADV and execution-side
        forced-exit accounting.
    total_return_open_df / total_return_close_df
        Dividend-adjusted attribution prices. They are never fill prices; they
        are used only to measure economic PnL, including forced-exit intervals.
    benchmark_returns
        Required benchmark total return for the same [t open, t+1 open)
        interval, labelled on t. Formal runs never silently fall back to an
        equal-weight universe benchmark.
    """
    n_groups = int(n_groups or CONFIG.backtest.n_groups)
    rebalance_days = int(rebalance_days or CONFIG.backtest.rebalance_days)
    rebalance_mode = str(
        rebalance_mode
        or getattr(CONFIG.backtest, "rebalance_mode", "every_n_days")
    ).lower()
    exec_cfg = _resolve_execution(execution)
    if factor_direction not in {-1, 1}:
        raise ValueError("factor_direction must be fixed ex ante as +1 or -1")
    if execution_open_df is None or execution_open_df.empty:
        raise ValueError("Formal next-open backtest requires execution_open_df")
    if execution_close_df is None or execution_close_df.empty:
        raise ValueError("Formal backtest requires execution_close_df")
    if total_return_open_df is None or total_return_open_df.empty:
        raise ValueError("Formal backtest requires total_return_open_df")
    if total_return_close_df is None or total_return_close_df.empty:
        raise ValueError("Formal backtest requires total_return_close_df")
    if benchmark_returns is None or benchmark_returns.empty:
        raise ValueError(
            "Formal backtest requires an explicit immutable benchmark return series"
        )

    # Total-return open is an attribution series, never an execution price.
    held_returns = total_return_open_df.pct_change(fill_method=None).shift(-1)
    common_dates = factor_df.index.intersection(held_returns.index)
    common_cols = factor_df.columns.intersection(held_returns.columns)
    if common_dates.empty or common_cols.empty:
        raise ValueError("Factor and total-return price histories do not overlap")
    f = factor_df.loc[common_dates, common_cols].copy()
    r = held_returns.loc[common_dates, common_cols].copy()

    execution_open = execution_open_df.reindex(
        index=common_dates, columns=common_cols
    )
    execution_close = execution_close_df.reindex(
        index=common_dates, columns=common_cols
    )
    total_return_open = total_return_open_df.reindex(
        index=common_dates, columns=common_cols
    )
    total_return_close = total_return_close_df.reindex(
        index=common_dates, columns=common_cols
    )
    volume = (
        volume_df.reindex(index=common_dates, columns=common_cols)
        if volume_df is not None and not volume_df.empty
        else None
    )

    if tradable_mask is None:
        tradable_mask = build_tradable_mask(
            index=common_dates,
            columns=common_cols,
            returns_df=returns_df,
            price_df=execution_close,
            open_df=execution_open,
            volume_df=volume,
            timing=exec_cfg["timing"],
        )
    else:
        tradable_mask = tradable_mask.reindex(
            index=common_dates,
            columns=common_cols,
            fill_value=False,
        )

    assign = _assign_groups_on_rebalance(
        f,
        rebalance_days=rebalance_days,
        n_groups=n_groups,
        rebalance_mode=rebalance_mode,
        tradable_mask=tradable_mask,
    )
    assign_held = assign.shift(1)
    rebal_dates = get_rebalance_dates(
        pd.DatetimeIndex(f.index),
        mode=rebalance_mode,
        step_days=rebalance_days,
    )

    # Fill prices remain executable; return attribution remains total-return.
    r, forced_exit_events = apply_membership_exit_policy_v2(
        r,
        assign_held,
        membership_mask=membership_mask,
        membership_events=membership_events,
        rebalance_dates=rebal_dates,
        execution_open_df=execution_open,
        execution_close_df=execution_close,
        total_return_open_df=total_return_open,
        total_return_close_df=total_return_close,
        policy=exec_cfg["membership_exit_policy"],
    )

    benchmark_base = pd.to_numeric(
        benchmark_returns.reindex(common_dates), errors="coerce"
    ).rename("Benchmark")

    group_cols = [f"Q{g}" for g in range(1, n_groups + 1)]
    simulation = simulate_group_portfolios(
        assign,
        r,
        rebal_dates,
        group_names={g: f"Q{g}" for g in range(1, n_groups + 1)},
        execution=exec_cfg,
        execution_prices=execution_open,
        volume=volume,
        forced_exit_events=forced_exit_events,
    )
    if simulation.capacity_breaches:
        raise BacktestCapacityError(simulation.capacity_breaches)
    gross_ret = simulation.gross_returns.reindex(columns=group_cols)
    group_ret = simulation.net_returns.reindex(columns=group_cols)
    cost_df = simulation.cost_returns.reindex(
        index=group_ret.index, columns=group_cols, fill_value=0.0,
    )
    benchmark_aligned = benchmark_base.reindex(group_ret.index)
    missing_benchmark = benchmark_aligned.isna()
    if missing_benchmark.any():
        sample = [
            pd.Timestamp(value).date().isoformat()
            for value in benchmark_aligned.index[missing_benchmark][:20]
        ]
        raise ValueError(
            "Benchmark must cover every measured strategy return interval; "
            f"missing_dates={sample} missing_count={int(missing_benchmark.sum())}"
        )
    days_total = max(len(group_ret.index), 1)
    cost_bps_per_year = {
        f"Q{g}": float(cost_df[f"Q{g}"].sum())
        * (252.0 / days_total)
        * 10000.0
        for g in range(1, n_groups + 1)
    }

    top, bot = f"Q{n_groups}", "Q1"
    direction = int(factor_direction)
    ls = (
        (gross_ret[top] - gross_ret[bot]) * direction
        - cost_df[top].fillna(0.0) - cost_df[bot].fillna(0.0)
    ).rename("LongShort")
    top_returns = group_ret[top].rename(top)
    excess = (top_returns - benchmark_aligned).rename("Excess")

    group_nav = (1.0 + group_ret.fillna(0.0)).cumprod()
    ls_nav = (1.0 + ls.fillna(0.0)).cumprod().rename("LongShort")
    benchmark_nav = (
        (1.0 + benchmark_aligned)
        .cumprod()
        .rename("Benchmark")
    )
    turnover = simulation.turnover.reindex(columns=group_cols)

    metrics_rows: dict[str, dict] = {}
    for col in group_cols:
        metrics_rows[col] = performance_summary(group_ret[col])
        if col == top:
            metrics_rows[col].update(
                relative_performance_summary(group_ret[col], benchmark_aligned)
            )
    metrics_rows["LongShort"] = performance_summary(ls)
    metrics_df = pd.DataFrame(metrics_rows).T
    if not turnover.empty:
        avg_to = turnover.mean(axis=0)
        avg_to["LongShort"] = (
            turnover.get(top, pd.Series(dtype=float)).mean()
            + turnover.get(bot, pd.Series(dtype=float)).mean()
        )
        metrics_df["AvgTurnover"] = metrics_df.index.map(avg_to.to_dict())

    log.info(
        "Price-safe quintile backtest done: n_groups=%d timing=%s benchmark_obs=%d",
        n_groups,
        exec_cfg["timing"],
        int(benchmark_aligned.notna().sum()),
    )
    return QuintileResult(
        group_daily_returns=group_ret,
        long_short_returns=ls,
        group_nav=group_nav,
        long_short_nav=ls_nav,
        turnover=turnover,
        group_metrics=metrics_df,
        group_assignment=assign,
        holdings_detail=simulation.holdings_detail,
        trades_detail=simulation.trades_detail,
        costs_detail=simulation.costs_detail,
        portfolio_daily=simulation.daily_state,
        position_daily=simulation.position_daily,
        config={
            "schema_version": 2,
            "price_semantics": "EXPLICIT_EXECUTION_AND_TOTAL_RETURN_V1",
            "portfolio_accounting": "STATEFUL_NAV_V1",
            "n_groups": n_groups,
            "rebalance_days": rebalance_days,
            "rebalance_mode": rebalance_mode,
            "direction": direction,
            "execution": exec_cfg,
            "benchmark_required": True,
        },
        benchmark_returns=benchmark_aligned,
        benchmark_nav=benchmark_nav,
        excess_returns=excess.dropna(),
        execution_cost_bps_per_year=cost_bps_per_year,
        gross_group_returns=gross_ret,
        cost_returns=cost_df,
        effective_returns=r,
        held_assignment=assign_held,
        tradable_mask=tradable_mask,
        rebalance_dates=rebal_dates,
    )


__all__ = ["quintile_backtest_v2"]
