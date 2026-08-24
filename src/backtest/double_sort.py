"""Independent double-sort backtest for factor robustness checks."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.backtest.metrics import performance_summary
from src.backtest.membership_exit_v2 import apply_membership_exit_policy_v2
from src.backtest.quintile import (
    _resolve_execution,
    build_tradable_mask,
)
from src.backtest.rebalance import get_rebalance_dates
from src.config import CONFIG


@dataclass
class DoubleSortResult:
    cell_daily_returns: dict[str, pd.Series]
    factor_returns: pd.Series
    factor_nav: pd.Series
    metrics: dict
    assignment_control: pd.DataFrame = field(default_factory=pd.DataFrame)
    assignment_factor: pd.DataFrame = field(default_factory=pd.DataFrame)
    config: dict = field(default_factory=dict)


def _assign_independent(
    control_df: pd.DataFrame,
    factor_df: pd.DataFrame,
    *,
    n_control: int,
    n_factor: int,
    rebalance_mode: str,
    rebalance_days: int,
    tradable_mask: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DatetimeIndex]:
    dates = pd.DatetimeIndex(factor_df.index)
    rebal_dates = get_rebalance_dates(
        dates, mode=rebalance_mode, step_days=rebalance_days
    )
    control_assign = pd.DataFrame(np.nan, index=dates, columns=factor_df.columns)
    factor_assign = pd.DataFrame(np.nan, index=dates, columns=factor_df.columns)
    current_control = pd.Series(np.nan, index=factor_df.columns)
    current_factor = pd.Series(np.nan, index=factor_df.columns)
    rebalance_set = set(rebal_dates)

    for dt in dates:
        if dt in rebalance_set:
            current_control = pd.Series(np.nan, index=factor_df.columns)
            current_factor = pd.Series(np.nan, index=factor_df.columns)
            valid = (
                control_df.loc[dt].notna()
                & factor_df.loc[dt].notna()
                & tradable_mask.loc[dt]
                .reindex(factor_df.columns)
                .fillna(False)
            )
            if int(valid.sum()) >= max(n_control, n_factor):
                try:
                    c_labels = pd.qcut(
                        control_df.loc[dt, valid].rank(method="first"),
                        q=n_control,
                        labels=list(range(1, n_control + 1)),
                    )
                    f_labels = pd.qcut(
                        factor_df.loc[dt, valid].rank(method="first"),
                        q=n_factor,
                        labels=list(range(1, n_factor + 1)),
                    )
                except ValueError:
                    c_labels = None
                    f_labels = None
                if c_labels is not None and f_labels is not None:
                    current_control.loc[c_labels.index] = (
                        c_labels.astype(int).values
                    )
                    current_factor.loc[f_labels.index] = (
                        f_labels.astype(int).values
                    )
        control_assign.loc[dt] = current_control
        factor_assign.loc[dt] = current_factor

    return control_assign, factor_assign, rebal_dates


def _strict_cell_return(
    returns: pd.DataFrame,
    held: pd.DataFrame,
    *,
    label: str,
) -> pd.Series:
    held_count = held.sum(axis=1)
    available_count = (held & returns.notna()).sum(axis=1)
    incomplete = (held_count > 0) & (available_count < held_count)
    if len(returns.index):
        final_date = pd.Timestamp(returns.index[-1])
        incomplete &= ~(
            (pd.DatetimeIndex(returns.index) == final_date)
            & (available_count == 0)
        )
    if incomplete.any():
        bad_date = pd.Timestamp(incomplete.index[incomplete][0])
        missing = held.loc[bad_date] & returns.loc[bad_date].isna()
        raise ValueError(
            "Missing return for held double-sort securities; refusing "
            f"renormalization: date={bad_date.date()} cell={label} "
            f"tickers={list(returns.columns[missing])[:10]}"
        )
    weights = held.astype(float).div(
        held_count.replace(0, np.nan),
        axis=0,
    )
    values = (returns * weights).sum(axis=1, min_count=1)
    return values.where(
        (held_count > 0) & (available_count == held_count)
    ).dropna()


def double_sort_backtest(
    factor_df: pd.DataFrame,
    control_df: pd.DataFrame,
    returns_df: pd.DataFrame,
    *,
    factor_direction: int = +1,
    n_control: int = 5,
    n_factor: int = 5,
    rebalance_days: int | None = None,
    rebalance_mode: str | None = None,
    open_df: pd.DataFrame | None = None,
    price_df: pd.DataFrame | None = None,
    execution_open_df: pd.DataFrame | None = None,
    execution_close_df: pd.DataFrame | None = None,
    total_return_open_df: pd.DataFrame | None = None,
    total_return_close_df: pd.DataFrame | None = None,
    volume_df: pd.DataFrame | None = None,
    tradable_mask: pd.DataFrame | None = None,
    membership_mask: pd.DataFrame | None = None,
    membership_events: pd.DataFrame | None = None,
    execution: dict | None = None,
) -> DoubleSortResult:
    """
    Independently sort stocks by control and target factor, then compute
    high-minus-low target-factor returns averaged across control buckets.
    """
    rebalance_days = int(rebalance_days or CONFIG.backtest.rebalance_days)
    rebalance_mode = str(
        rebalance_mode
        or getattr(CONFIG.backtest, "rebalance_mode", "every_n_days")
    ).lower()
    exec_cfg = _resolve_execution(execution)
    if factor_direction not in {-1, 1}:
        raise ValueError(
            "factor_direction must be fixed ex ante as +1 or -1"
        )

    explicit_semantics = all(
        value is not None and not value.empty
        for value in (
            execution_open_df,
            execution_close_df,
            total_return_open_df,
            total_return_close_df,
        )
    )
    if explicit_semantics:
        execution_open = execution_open_df
        execution_close = execution_close_df
        total_return_open = total_return_open_df
        total_return_close = total_return_close_df
        held_returns = total_return_open.pct_change(fill_method=None).shift(-1)
    else:
        if open_df is None or open_df.empty:
            raise ValueError(
                "double_sort_backtest requires explicit execution/total-return "
                "matrices for formal next-open research"
            )
        # Legacy synthetic callers retain their historical behavior. Formal
        # run_mvp always supplies the four explicit matrices above.
        execution_open = open_df
        execution_close = price_df
        total_return_open = open_df
        total_return_close = price_df
        held_returns = open_df.pct_change(fill_method=None).shift(-1)

    common_dates = factor_df.index.intersection(control_df.index).intersection(
        held_returns.index
    )
    common_cols = factor_df.columns.intersection(control_df.columns).intersection(
        held_returns.columns
    )
    f = factor_df.loc[common_dates, common_cols]
    c = control_df.loc[common_dates, common_cols]
    r = held_returns.loc[common_dates, common_cols]

    if tradable_mask is None:
        tradable_mask = build_tradable_mask(
            index=common_dates,
            columns=common_cols,
            returns_df=returns_df,
            price_df=execution_close,
            open_df=execution_open,
            volume_df=volume_df,
            timing=exec_cfg["timing"],
        )
    else:
        tradable_mask = tradable_mask.reindex(
            index=common_dates, columns=common_cols, fill_value=False
        )

    control_assign, factor_assign, rebal_dates = _assign_independent(
        c,
        f,
        n_control=n_control,
        n_factor=n_factor,
        rebalance_mode=rebalance_mode,
        rebalance_days=rebalance_days,
        tradable_mask=tradable_mask,
    )
    control_held = control_assign.shift(1)
    factor_held = factor_assign.shift(1)
    held_cell = control_held * 100.0 + factor_held
    r, _ = apply_membership_exit_policy_v2(
        r,
        held_cell,
        membership_mask=membership_mask,
        membership_events=membership_events,
        rebalance_dates=rebal_dates,
        execution_open_df=execution_open,
        execution_close_df=execution_close,
        total_return_open_df=total_return_open,
        total_return_close_df=total_return_close,
        policy=exec_cfg["membership_exit_policy"],
    )

    cell_returns: dict[str, pd.Series] = {}
    for i in range(1, n_control + 1):
        for j in range(1, n_factor + 1):
            name = f"C{i}_F{j}"
            mask = (control_held == i) & (factor_held == j)
            cell_returns[name] = _strict_cell_return(
                r,
                mask,
                label=name,
            )

    spreads = []
    for i in range(1, n_control + 1):
        hi = cell_returns.get(f"C{i}_F{n_factor}", pd.Series(dtype="float64"))
        lo = cell_returns.get(f"C{i}_F1", pd.Series(dtype="float64"))
        spreads.append((hi - lo).rename(f"C{i}_spread"))
    spread_frame = pd.concat(spreads, axis=1)
    available_spreads = spread_frame.notna().sum(axis=1)
    partial_spreads = (available_spreads > 0) & (
        available_spreads < n_control
    )
    if partial_spreads.any():
        bad_date = pd.Timestamp(partial_spreads.index[partial_spreads][0])
        raise ValueError(
            "Double-sort control-bucket spread is partially missing; "
            f"refusing renormalization: date={bad_date.date()}"
        )
    factor_returns = spread_frame.mean(axis=1).where(
        available_spreads == n_control
    ).dropna() * int(factor_direction)
    factor_returns.name = "DoubleSortOrientedSpread"
    factor_nav = (1.0 + factor_returns.fillna(0)).cumprod()
    factor_nav.name = "DoubleSortNAV"

    return DoubleSortResult(
        cell_daily_returns=cell_returns,
        factor_returns=factor_returns,
        factor_nav=factor_nav,
        metrics=performance_summary(factor_returns),
        assignment_control=control_assign,
        assignment_factor=factor_assign,
        config={
            "n_control": n_control,
            "n_factor": n_factor,
            "factor_direction": int(factor_direction),
            "rebalance_days": rebalance_days,
            "rebalance_mode": rebalance_mode,
            "execution": exec_cfg,
            "price_semantics": (
                "EXPLICIT_EXECUTION_AND_TOTAL_RETURN_V1"
                if explicit_semantics
                else "LEGACY_SYNTHETIC"
            ),
        },
    )


__all__ = ["DoubleSortResult", "double_sort_backtest"]
