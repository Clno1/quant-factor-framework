"""Independent double-sort backtest for factor robustness checks."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.backtest.metrics import performance_summary
from src.backtest.quintile import _resolve_execution, build_tradable_mask
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

    for dt in rebal_dates:
        valid = (
            control_df.loc[dt].notna()
            & factor_df.loc[dt].notna()
            & tradable_mask.loc[dt].reindex(factor_df.columns).fillna(False)
        )
        if int(valid.sum()) < max(n_control, n_factor):
            continue
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
            continue
        control_assign.loc[dt, c_labels.index] = c_labels.astype(int).values
        factor_assign.loc[dt, f_labels.index] = f_labels.astype(int).values

    return control_assign.ffill(), factor_assign.ffill(), rebal_dates


def double_sort_backtest(
    factor_df: pd.DataFrame,
    control_df: pd.DataFrame,
    returns_df: pd.DataFrame,
    *,
    n_control: int = 5,
    n_factor: int = 5,
    rebalance_days: int | None = None,
    rebalance_mode: str | None = None,
    open_df: pd.DataFrame | None = None,
    price_df: pd.DataFrame | None = None,
    volume_df: pd.DataFrame | None = None,
    tradable_mask: pd.DataFrame | None = None,
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

    if exec_cfg["timing"] == "next_open":
        if open_df is None or open_df.empty:
            raise ValueError("double_sort_backtest requires open_df for next_open.")
        held_returns = open_df.pct_change().shift(-1)
    else:
        held_returns = returns_df

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
            price_df=price_df,
            open_df=open_df,
            volume_df=volume_df,
            timing=exec_cfg["timing"],
        )
    else:
        tradable_mask = tradable_mask.reindex(
            index=common_dates, columns=common_cols, fill_value=False
        )

    control_assign, factor_assign, _ = _assign_independent(
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

    cell_returns: dict[str, pd.Series] = {}
    for i in range(1, n_control + 1):
        for j in range(1, n_factor + 1):
            name = f"C{i}_F{j}"
            mask = (control_held == i) & (factor_held == j)
            cell_returns[name] = r.where(mask).mean(axis=1).dropna()

    spreads = []
    for i in range(1, n_control + 1):
        hi = cell_returns.get(f"C{i}_F{n_factor}", pd.Series(dtype="float64"))
        lo = cell_returns.get(f"C{i}_F1", pd.Series(dtype="float64"))
        spreads.append((hi - lo).rename(f"C{i}_spread"))
    factor_returns = pd.concat(spreads, axis=1).mean(axis=1).dropna()
    factor_returns.name = "DoubleSortHighMinusLow"
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
            "rebalance_days": rebalance_days,
            "rebalance_mode": rebalance_mode,
            "execution": exec_cfg,
        },
    )


__all__ = ["DoubleSortResult", "double_sort_backtest"]
