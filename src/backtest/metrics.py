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
    # 几何年化：只适用于一条可投资财富过程（strategy / benchmark）。
    cum = (1.0 + r).prod()
    return float(cum ** (n / len(r)) - 1.0)


def annualized_active_return(excess_ret: pd.Series) -> float:
    """Annualized arithmetic active return used by the Information Ratio.

    Daily ``strategy - benchmark`` is not itself a self-financing wealth
    process, so geometrically compounding ``1 + daily_excess`` is not a valid
    definition of active return.  The standard IR numerator is the annualized
    mean active return.
    """
    r = excess_ret.dropna()
    if r.empty:
        return np.nan
    return float(r.mean() * _periods_per_year())


def relative_wealth_annualized_return(
    strategy_ret: pd.Series,
    benchmark_ret: pd.Series,
) -> float:
    """Geometric annualized return of strategy wealth relative to benchmark.

    This is intentionally separate from ``ExcessAnnReturn`` / IR.  It answers
    the different question: how quickly did the strategy/benchmark wealth ratio
    compound over the common sample?
    """
    pair = pd.concat(
        [strategy_ret, benchmark_ret],
        axis=1,
        keys=["strategy", "benchmark"],
        sort=False,
    ).dropna()
    if pair.empty:
        return np.nan
    strategy_wealth = float((1.0 + pair["strategy"]).prod())
    benchmark_wealth = float((1.0 + pair["benchmark"]).prod())
    if benchmark_wealth <= 0 or strategy_wealth <= 0:
        return np.nan
    ratio = strategy_wealth / benchmark_wealth
    return float(ratio ** (_periods_per_year() / len(pair)) - 1.0)


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


def drawdown_series(daily_ret: pd.Series) -> pd.Series:
    """Drawdown from the high-water mark, including starting capital of one."""
    nav = (1.0 + daily_ret.dropna()).cumprod()
    return nav / nav.cummax().clip(lower=1.0) - 1.0


def max_drawdown(daily_ret: pd.Series) -> float:
    r = daily_ret.dropna()
    if r.empty:
        return np.nan
    return float(drawdown_series(r).min())


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
        "AnnReturn": annualized_return(r),
        "AnnVol": annualized_volatility(r),
        "Sharpe": sharpe_ratio(r),
        "MaxDD": max_drawdown(r),
        "Calmar": calmar_ratio(r),
        "WinRate": win_rate(r),
        "N_days": int(len(r)),
    }


def tracking_error(excess_ret: pd.Series) -> float:
    r = excess_ret.dropna()
    if r.empty:
        return np.nan
    return float(r.std(ddof=1) * np.sqrt(_periods_per_year()))


def information_ratio(excess_ret: pd.Series) -> float:
    """Standard annualized information ratio = mean(active)/std(active)."""
    r = excess_ret.dropna()
    if r.empty:
        return np.nan
    te = tracking_error(r)
    if te == 0 or pd.isna(te):
        return np.nan
    return float(annualized_active_return(r) / te)


def beta_to_benchmark(strategy_ret: pd.Series, benchmark_ret: pd.Series) -> float:
    pair = pd.concat(
        [strategy_ret, benchmark_ret],
        axis=1,
        keys=["s", "b"],
        sort=False,
    ).dropna()
    if pair.empty:
        return np.nan
    var_b = pair["b"].var(ddof=1)
    if var_b == 0 or pd.isna(var_b):
        return np.nan
    return float(pair["s"].cov(pair["b"]) / var_b)


def relative_performance_summary(
    strategy_ret: pd.Series,
    benchmark_ret: pd.Series,
) -> dict:
    """Benchmark-relative metrics for daily strategy and benchmark returns."""
    pair = pd.concat(
        [strategy_ret, benchmark_ret],
        axis=1,
        keys=["strategy", "benchmark"],
        sort=False,
    ).dropna()
    if pair.empty:
        return {
            "BenchmarkAnnReturn": np.nan,
            "ExcessAnnReturn": np.nan,
            "RelativeWealthAnnReturn": np.nan,
            "TrackingError": np.nan,
            "InformationRatio": np.nan,
            "Beta": np.nan,
        }
    excess = pair["strategy"] - pair["benchmark"]
    return {
        "BenchmarkAnnReturn": annualized_return(pair["benchmark"]),
        "ExcessAnnReturn": annualized_active_return(excess),
        "RelativeWealthAnnReturn": relative_wealth_annualized_return(
            pair["strategy"], pair["benchmark"]
        ),
        "TrackingError": tracking_error(excess),
        "InformationRatio": information_ratio(excess),
        "Beta": beta_to_benchmark(pair["strategy"], pair["benchmark"]),
    }


__all__ = [
    "annualized_return",
    "annualized_active_return",
    "relative_wealth_annualized_return",
    "annualized_volatility",
    "sharpe_ratio",
    "max_drawdown",
    "drawdown_series",
    "calmar_ratio",
    "win_rate",
    "performance_summary",
    "tracking_error",
    "information_ratio",
    "beta_to_benchmark",
    "relative_performance_summary",
]
