"""
五分位分组回测（Quintile Analysis）。

核心流程（向量化）：
  1. 每隔 rebalance_days 取一个"调仓日"，在调仓日按因子值把股票分为 Q1..QN 组（pd.qcut）
  2. 组合持仓在下一个调仓日前保持不变（等权）
  3. 组合日收益 = 当日各持仓股票收益的等权平均
  4. Long-Short 组合 = QN - Q1（按因子方向自动调整）
  5. 计算换手率、累计净值、绩效指标

防前视偏差：
  - 调仓日 t 使用 factor_t，持仓从 t+1 开始生效到下一个调仓日 t'，收益用 t+1..t' 的日收益。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from src.backtest.metrics import performance_summary
from src.config import CONFIG
from src.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class QuintileResult:
    """五分位回测输出容器。"""
    group_daily_returns: pd.DataFrame   # date x [Q1..QN]，每组日收益
    long_short_returns: pd.Series       # date，Long-Short 日收益
    group_nav: pd.DataFrame             # 净值曲线（以 1.0 为初值）
    long_short_nav: pd.Series
    turnover: pd.DataFrame              # 每个调仓日的各组换手率（新旧持仓差集 / 持仓数）
    group_metrics: pd.DataFrame         # 每组绩效指标 + Long-Short
    group_assignment: pd.DataFrame      # date x ticker，每日所在组号（NaN 表示当日未分组）
    config: dict = field(default_factory=dict)


def _assign_groups_on_rebalance(
    factor_df: pd.DataFrame,
    rebalance_days: int,
    n_groups: int,
) -> pd.DataFrame:
    """
    仅在调仓日做 qcut 分组；非调仓日 forward-fill 沿用上一次分组。
    输出：date x ticker，值 ∈ {1..n_groups} 或 NaN。
    """
    dates = factor_df.index
    # 选取调仓日（从第一天开始每隔 rebalance_days 一次）
    rebal_mask = np.zeros(len(dates), dtype=bool)
    rebal_mask[::rebalance_days] = True
    rebal_dates = dates[rebal_mask]

    assign = pd.DataFrame(np.nan, index=dates, columns=factor_df.columns)
    for dt in rebal_dates:
        row = factor_df.loc[dt].dropna()
        # 要求每组至少 1 只；小样本（如 MAG7 共 7 只分 3 组）也能工作
        if len(row) < n_groups:
            continue
        try:
            labels = pd.qcut(row.rank(method="first"),
                             q=n_groups,
                             labels=list(range(1, n_groups + 1)))
        except ValueError:
            continue
        assign.loc[dt, labels.index] = labels.astype(int).values

    # 沿用上次分组直至下一次调仓
    assign = assign.ffill()
    return assign


def _compute_turnover(assign_df: pd.DataFrame, n_groups: int, rebalance_days: int) -> pd.DataFrame:
    """计算每组每次调仓的换手率（对称差集 / 2 / 持仓数）。"""
    rebal_dates = assign_df.index[::rebalance_days]
    rows = []
    prev_holdings: dict[int, set] = {g: set() for g in range(1, n_groups + 1)}
    for dt in rebal_dates:
        row_result = {"date": dt}
        for g in range(1, n_groups + 1):
            curr = set(assign_df.columns[assign_df.loc[dt].values == g])
            if prev_holdings[g]:
                sym_diff = prev_holdings[g].symmetric_difference(curr)
                denom = max(len(prev_holdings[g]) + len(curr), 1)
                # 双向换手率 = 对称差 / (两期持仓合计)
                row_result[f"Q{g}"] = len(sym_diff) / denom
            else:
                row_result[f"Q{g}"] = np.nan
            prev_holdings[g] = curr
        rows.append(row_result)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("date")


def quintile_backtest(
    factor_df: pd.DataFrame,
    returns_df: pd.DataFrame,
    n_groups: Optional[int] = None,
    rebalance_days: Optional[int] = None,
    factor_direction: int = 0,
) -> QuintileResult:
    """
    五分位分组回测。

    Parameters
    ----------
    factor_df : date x ticker 因子值（通常已预处理）
    returns_df: date x ticker 日收益率
    n_groups  : 分组数（默认 5）
    rebalance_days : 调仓频率（交易日），默认读配置
    factor_direction : +1 / -1 / 0
        0 表示根据 QN-Q1 的历史表现自动判断方向（若均值为负则反向 Long-Short）。

    Returns
    -------
    QuintileResult
    """
    n_groups = int(n_groups or CONFIG.backtest.n_groups)
    rebalance_days = int(rebalance_days or CONFIG.backtest.rebalance_days)

    # 对齐索引与列
    common_dates = factor_df.index.intersection(returns_df.index)
    common_cols = factor_df.columns.intersection(returns_df.columns)
    f = factor_df.loc[common_dates, common_cols].copy()
    r = returns_df.loc[common_dates, common_cols].copy()

    # 分组：以 t 日因子分组，持仓从 t+1 日生效 —— 故对 assign 做 shift(1)
    assign = _assign_groups_on_rebalance(f, rebalance_days=rebalance_days, n_groups=n_groups)
    assign_held = assign.shift(1)  # 今天的持仓由昨天决定

    # 各组日收益（等权）
    group_cols = [f"Q{g}" for g in range(1, n_groups + 1)]
    group_ret = pd.DataFrame(np.nan, index=common_dates, columns=group_cols)
    for g in range(1, n_groups + 1):
        mask = (assign_held == g)
        # 当日等权平均
        r_masked = r.where(mask)
        group_ret[f"Q{g}"] = r_masked.mean(axis=1)

    group_ret = group_ret.dropna(how="all")

    # Long-Short：Q_top - Q_bottom，方向自动判断
    top, bot = f"Q{n_groups}", "Q1"
    raw_ls = group_ret[top] - group_ret[bot]
    if factor_direction == 0:
        direction = +1 if raw_ls.mean() >= 0 else -1
    else:
        direction = int(np.sign(factor_direction)) or 1
    ls = raw_ls * direction
    ls.name = "LongShort"

    # 净值
    group_nav = (1.0 + group_ret.fillna(0)).cumprod()
    ls_nav = (1.0 + ls.fillna(0)).cumprod()
    ls_nav.name = "LongShort"

    # 换手率
    turnover = _compute_turnover(assign, n_groups=n_groups, rebalance_days=rebalance_days)

    # 每组 + Long-Short 绩效
    metrics_rows = {}
    for col in group_cols:
        metrics_rows[col] = performance_summary(group_ret[col])
    metrics_rows["LongShort"] = performance_summary(ls)
    metrics_df = pd.DataFrame(metrics_rows).T  # 行=Q1..QN,LongShort

    # 加入平均换手率
    if not turnover.empty:
        avg_to = turnover.mean(axis=0)
        avg_to["LongShort"] = (
            turnover.get(top, pd.Series(dtype=float)).mean()
            + turnover.get(bot, pd.Series(dtype=float)).mean()
        )
        metrics_df["AvgTurnover"] = metrics_df.index.map(avg_to.to_dict())

    log.info(
        "Quintile backtest done: n_groups=%d, rebalance=%dd, direction=%+d, "
        "LongShort AnnReturn=%.4f, Sharpe=%.3f, MaxDD=%.4f",
        n_groups, rebalance_days, direction,
        metrics_df.loc["LongShort", "AnnReturn"],
        metrics_df.loc["LongShort", "Sharpe"],
        metrics_df.loc["LongShort", "MaxDD"],
    )

    return QuintileResult(
        group_daily_returns=group_ret,
        long_short_returns=ls,
        group_nav=group_nav,
        long_short_nav=ls_nav,
        turnover=turnover,
        group_metrics=metrics_df,
        group_assignment=assign,
        config={
            "n_groups": n_groups,
            "rebalance_days": rebalance_days,
            "direction": direction,
        },
    )


__all__ = ["QuintileResult", "quintile_backtest"]
