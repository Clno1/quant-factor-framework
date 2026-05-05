"""
单因子 IC 分析（Information Coefficient）。

核心逻辑：
  - 前向收益：在 t 日计算 factor_t，对齐 [t+1 -> t+1+N] 的累计收益（严格防前视）
  - 每日截面：计算 factor_t 与 forward_return_t 的相关系数（默认 Spearman = Rank IC）
  - 汇总指标：IC均值、IC标准差、IC_IR、IC>0占比、|IC|>0.02占比、t统计量

输出与用户参考图一致，便于未来多因子对比汇总。
"""
from __future__ import annotations

from typing import Iterable, Mapping

import numpy as np
import pandas as pd
from scipy import stats

from src.config import CONFIG
from src.utils.logger import get_logger

log = get_logger(__name__)


def compute_forward_returns(returns_df: pd.DataFrame, periods: int) -> pd.DataFrame:
    """
    以日收益率宽表 (date x ticker) 计算未来 N 日累计收益。

    对齐关系（严格防前视）：
        t 日的 forward_return = (1+r_{t+1})*(1+r_{t+2})*...*(1+r_{t+N}) - 1
    这样 factor_t 与 fwd_return_t 同日对齐，即可直接求截面 IC。
    """
    if periods <= 0:
        raise ValueError("periods must be positive")
    if returns_df.empty:
        return returns_df.copy()

    log_ret = np.log1p(returns_df)
    # 未来 N 日收益 = sum of log returns from t+1 to t+N
    # rolling(N).sum().shift(-N) 得到 [t+1 .. t+N] 的和
    fwd_log = log_ret.rolling(window=periods, min_periods=periods).sum().shift(-periods)
    fwd = np.expm1(fwd_log)
    return fwd


def _compute_ic_row(factor_row: pd.Series, ret_row: pd.Series, method: str, min_stocks: int) -> float:
    """单截面 IC，缺失/样本不足返回 NaN。"""
    pair = pd.concat([factor_row, ret_row], axis=1, keys=["f", "r"]).dropna()
    if len(pair) < min_stocks:
        return np.nan
    if method == "spearman":
        corr, _ = stats.spearmanr(pair["f"], pair["r"])
    elif method == "pearson":
        corr = pair["f"].corr(pair["r"])
    else:
        raise ValueError(f"Unknown IC method: {method}")
    return float(corr) if pd.notna(corr) else np.nan


def compute_ic(
    factor_df: pd.DataFrame,
    returns_df: pd.DataFrame,
    periods: int | None = None,
    method: str | None = None,
    min_stocks: int | None = None,
) -> pd.Series:
    """
    计算因子的每日 IC 时序。

    Parameters
    ----------
    factor_df : date x ticker 因子值（通常已预处理）
    returns_df: date x ticker 日收益率
    periods   : 前向收益窗口，默认取自配置
    method    : 'spearman'(Rank IC) / 'pearson'
    min_stocks: 单日截面最少有效股票数

    Returns
    -------
    pd.Series (index=date, value=IC)
    """
    periods = int(periods if periods is not None else CONFIG.ic_analysis.forward_periods)
    method = (method or CONFIG.ic_analysis.method).lower()
    min_stocks = int(min_stocks if min_stocks is not None else CONFIG.ic_analysis.min_stocks)

    fwd = compute_forward_returns(returns_df, periods=periods)

    # 对齐索引 & 列
    common_dates = factor_df.index.intersection(fwd.index)
    common_cols = factor_df.columns.intersection(fwd.columns)
    f = factor_df.loc[common_dates, common_cols]
    r = fwd.loc[common_dates, common_cols]

    ic_vals: list[float] = []
    idx: list[pd.Timestamp] = []
    for dt in common_dates:
        ic_vals.append(_compute_ic_row(f.loc[dt], r.loc[dt], method=method, min_stocks=min_stocks))
        idx.append(dt)

    ic = pd.Series(ic_vals, index=pd.Index(idx, name="date"), name="IC")
    ic = ic.dropna()
    log.info("IC computed: N=%d, mean=%.4f, std=%.4f, IR=%.4f",
             len(ic), ic.mean(), ic.std(ddof=1), ic.mean() / (ic.std(ddof=1) or np.nan))
    return ic


def ic_summary(ic_series: pd.Series, ic_threshold: float = 0.02) -> dict:
    """
    汇总 IC 统计指标（列顺序与用户参考图一致）：
      IC均值 / IC标准差 / IC_IR / IC>0占比 / |IC|>ic_threshold 占比 / t统计量
    """
    s = ic_series.dropna()
    n = len(s)
    if n == 0:
        return {
            "IC_mean": np.nan, "IC_std": np.nan, "IC_IR": np.nan,
            "IC_gt0_pct": np.nan, "IC_abs_gt_thr_pct": np.nan, "t_stat": np.nan, "N": 0,
        }

    mean = s.mean()
    std = s.std(ddof=1)
    ir = mean / std if std and not pd.isna(std) else np.nan
    gt0 = float((s > 0).mean())
    abs_gt = float((s.abs() > ic_threshold).mean())
    # t = mean / (std / sqrt(n))
    t_stat = mean / (std / np.sqrt(n)) if std and not pd.isna(std) else np.nan

    return {
        "IC_mean": float(mean),
        "IC_std": float(std),
        "IC_IR": float(ir) if pd.notna(ir) else np.nan,
        "IC_gt0_pct": gt0,
        "IC_abs_gt_thr_pct": abs_gt,
        "t_stat": float(t_stat) if pd.notna(t_stat) else np.nan,
        "N": int(n),
    }


def ic_summary_table(ic_dict: Mapping[str, pd.Series], ic_threshold: float = 0.02) -> pd.DataFrame:
    """
    多因子 IC 汇总表。输入 {factor_name: IC 时序}，输出对齐用户图片的 DataFrame。
    行按 IC_mean 倒序排序。
    """
    rows = []
    for name, s in ic_dict.items():
        summary = ic_summary(s, ic_threshold=ic_threshold)
        summary["factor"] = name
        rows.append(summary)
    df = pd.DataFrame(rows).set_index("factor")
    df = df[["IC_mean", "IC_std", "IC_IR", "IC_gt0_pct", "IC_abs_gt_thr_pct", "t_stat"]]
    df = df.sort_values("IC_mean", ascending=False, key=lambda s: s.abs())
    # 与图片保持一致的列名
    df.columns = ["IC均值", "IC标准差", "IC_IR", "IC>0占比", f"|IC|>{ic_threshold}占比", "t统计量"]
    return df


__all__ = [
    "compute_forward_returns",
    "compute_ic",
    "ic_summary",
    "ic_summary_table",
]
