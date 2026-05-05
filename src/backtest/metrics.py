"""
绩效指标计算。

所有输入：pd.Series / pd.DataFrame，index=date，值为 **日收益率**。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.config import CONFIG


def _periods_per_year() -> int:
    try:
        return int(CONFIG.backtest.trading_days_per_year)
    except Exception:
        return 252


def annualized_return(daily_ret: pd.Series) -> float:
    r = daily_ret.dropna()
    if r.empty:
        return np.nan
    n = _periods_per_year()
    # 几何年化
    cum = (1.0 + r).prod()
    return float(cum ** (n / len(r)) - 1.0)


def annualized_volatility(daily_ret: pd.Series) -> float:
    r = daily_ret.dropna()
    if r.empty:
        return np.nan
    return float(r.std(ddof=1) * np.sqrt(_periods_per_year()))


def sharpe_ratio(daily_ret: pd.Series, rf: float | None = None) -> float:
    r = daily_ret.dropna()
    if r.empty:
        return np.nan
    rf = float(rf if rf is not None else CONFIG.backtest.risk_free_rate)
    rf_daily = (1 + rf) ** (1.0 / _periods_per_year()) - 1.0
    ex = r - rf_daily
    sd = ex.std(ddof=1)
    if sd == 0 or pd.isna(sd):
        return np.nan
    return float(ex.mean() / sd * np.sqrt(_periods_per_year()))


def max_drawdown(daily_ret: pd.Series) -> float:
    r = daily_ret.dropna()
    if r.empty:
        return np.nan
    nav = (1.0 + r).cumprod()
    peak = nav.cummax()
    dd = (nav / peak - 1.0)
    return float(dd.min())


def calmar_ratio(daily_ret: pd.Series) -> float:
    ar = annualized_return(daily_ret)
    mdd = max_drawdown(daily_ret)
    if pd.isna(ar) or pd.isna(mdd) or mdd == 0:
        return np.nan
    return float(ar / abs(mdd))


def win_rate(daily_ret: pd.Series) -> float:
    r = daily_ret.dropna()
    if r.empty:
        return np.nan
    return float((r > 0).mean())


def performance_summary(daily_ret: pd.Series) -> dict:
    """完整绩效摘要。"""
    r = daily_ret.dropna()
    return {
        "AnnReturn":  annualized_return(r),
        "AnnVol":     annualized_volatility(r),
        "Sharpe":     sharpe_ratio(r),
        "MaxDD":      max_drawdown(r),
        "Calmar":     calmar_ratio(r),
        "WinRate":    win_rate(r),
        "N_days":     int(len(r)),
    }


__all__ = [
    "annualized_return",
    "annualized_volatility",
    "sharpe_ratio",
    "max_drawdown",
    "calmar_ratio",
    "win_rate",
    "performance_summary",
]
