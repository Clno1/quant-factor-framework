"""
因子置信评估系统。

目标不是替代 IC/分层回测，而是在它们之上给出更接近成熟多因子研究流程的
「能不能进入策略库」判断：
  - 预测力：IC 均值、IR、t/p/q 值、置信区间
  - 稳定性：滚动/月份/子样本方向一致性
  - 经济意义：分组单调性、多空净收益与 Sharpe
  - 可交易性：Rank 自相关、Top 分位换手、年化交易摩擦
  - 数据质量：覆盖率、截面离散度、最近有效覆盖
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import numpy as np
import pandas as pd
from scipy import stats

from src.backtest.rebalance import get_rebalance_dates
from src.config import CONFIG


PASS = "PASS"
WATCH = "WATCH"
FAIL = "FAIL"


@dataclass
class ConfidenceArtifacts:
    """单因子置信评估落盘所需的全部产物。"""

    report: dict[str, Any]
    checks: pd.DataFrame
    rank_autocorr: pd.DataFrame
    quantile_turnover: pd.DataFrame


def _cfg(path: str, default: Any) -> Any:
    node: Any = CONFIG
    for key in path.split("."):
        try:
            node = getattr(node, key)
        except Exception:  # noqa: BLE001
            return default
    return node


def _finite_float(value: Any, default: float = np.nan) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    return x if np.isfinite(x) else default


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator == 0 or not np.isfinite(denominator):
        return np.nan
    return numerator / denominator


def _status_min(value: float, pass_value: float, watch_value: float) -> tuple[str, float]:
    if not np.isfinite(value):
        return FAIL, 0.0
    if value >= pass_value:
        return PASS, 1.0
    if value >= watch_value:
        return WATCH, 0.5
    return FAIL, 0.0


def _status_max(value: float, pass_value: float, watch_value: float) -> tuple[str, float]:
    if not np.isfinite(value):
        return FAIL, 0.0
    if value <= pass_value:
        return PASS, 1.0
    if value <= watch_value:
        return WATCH, 0.5
    return FAIL, 0.0


def _check_row(
    *,
    category: str,
    check_id: str,
    label: str,
    value: float,
    pass_threshold: float,
    watch_threshold: float,
    higher_is_better: bool = True,
    unit: str = "",
    message: str = "",
) -> dict[str, Any]:
    if higher_is_better:
        status, score = _status_min(value, pass_threshold, watch_threshold)
        op = ">="
    else:
        status, score = _status_max(value, pass_threshold, watch_threshold)
        op = "<="
    return {
        "category": category,
        "check_id": check_id,
        "label": label,
        "value": float(value) if np.isfinite(value) else np.nan,
        "unit": unit,
        "pass_threshold": float(pass_threshold),
        "watch_threshold": float(watch_threshold),
        "operator": op,
        "status": status,
        "score": float(score),
        "message": message,
    }


def _bh_q_values(p_values: Iterable[float]) -> list[float]:
    """Benjamini-Hochberg FDR q-value。NaN 会原样保留。"""
    p = np.array([_finite_float(x) for x in p_values], dtype=float)
    q = np.full_like(p, np.nan, dtype=float)
    valid = np.isfinite(p)
    if not valid.any():
        return q.tolist()

    idx = np.where(valid)[0]
    p_valid = p[idx]
    order = np.argsort(p_valid)
    ranked = p_valid[order]
    m = len(ranked)
    raw = ranked * m / np.arange(1, m + 1)
    monotone = np.minimum.accumulate(raw[::-1])[::-1]
    q_valid = np.clip(monotone, 0.0, 1.0)
    restored = np.empty_like(q_valid)
    restored[order] = q_valid
    q[idx] = restored
    return q.tolist()


def _ic_stats(ic: pd.Series, direction_sign: int) -> dict[str, float]:
    s_raw = pd.to_numeric(ic, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if s_raw.empty:
        return {
            "n_obs": 0, "ic_mean_raw": np.nan, "ic_mean": np.nan, "ic_std": np.nan,
            "ic_ir": np.nan, "t_stat": np.nan, "p_value": np.nan,
            "p_value_two_sided": np.nan, "ci95_low": np.nan, "ci95_high": np.nan,
            "ic_positive_pct": np.nan,
        }

    s = s_raw * direction_sign
    n = int(len(s))
    mean = float(s.mean())
    std = float(s.std(ddof=1)) if n > 1 else np.nan
    se = std / np.sqrt(n) if np.isfinite(std) and std > 0 else np.nan
    t_stat = mean / se if np.isfinite(se) and se > 0 else np.nan
    p_two = float(2.0 * stats.t.sf(abs(t_stat), df=n - 1)) if np.isfinite(t_stat) and n > 1 else np.nan
    p_one = float(stats.t.sf(t_stat, df=n - 1)) if np.isfinite(t_stat) and n > 1 else np.nan
    t_crit = float(stats.t.ppf(0.975, df=n - 1)) if n > 1 else np.nan
    ci_low = mean - t_crit * se if np.isfinite(t_crit) and np.isfinite(se) else np.nan
    ci_high = mean + t_crit * se if np.isfinite(t_crit) and np.isfinite(se) else np.nan
    return {
        "n_obs": n,
        "ic_mean_raw": float(s_raw.mean()),
        "ic_mean": mean,
        "ic_std": std,
        "ic_ir": _safe_ratio(mean, std),
        "t_stat": float(t_stat) if np.isfinite(t_stat) else np.nan,
        "p_value": p_one,
        "p_value_two_sided": p_two,
        "ci95_low": float(ci_low) if np.isfinite(ci_low) else np.nan,
        "ci95_high": float(ci_high) if np.isfinite(ci_high) else np.nan,
        "ic_positive_pct": float((s > 0).mean()),
    }


def _stability_stats(ic: pd.Series, direction_sign: int) -> dict[str, float]:
    s = pd.to_numeric(ic, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if s.empty:
        return {
            "recent_ic_mean_63d": np.nan,
            "rolling_positive_pct_63d": np.nan,
            "monthly_positive_pct": np.nan,
            "subperiod_positive_pct": np.nan,
        }

    oriented = s * direction_sign
    rolling = oriented.rolling(63, min_periods=20).mean().dropna()
    monthly = oriented.resample("ME").mean().dropna()
    subperiod_positive = []
    if len(oriented) >= 60:
        for chunk in np.array_split(oriented.sort_index(), 3):
            chunk = pd.Series(chunk).dropna()
            if not chunk.empty:
                subperiod_positive.append(float(chunk.mean() > 0))

    return {
        "recent_ic_mean_63d": float(oriented.tail(63).mean()) if not oriented.empty else np.nan,
        "rolling_positive_pct_63d": float((rolling > 0).mean()) if not rolling.empty else np.nan,
        "monthly_positive_pct": float((monthly > 0).mean()) if not monthly.empty else np.nan,
        "subperiod_positive_pct": float(np.mean(subperiod_positive)) if subperiod_positive else np.nan,
    }


def _economic_stats(
    group_metrics: pd.DataFrame,
    *,
    direction_sign: int,
) -> dict[str, float]:
    out = {
        "monotonic_corr": np.nan,
        "long_short_ann_return": np.nan,
        "long_short_sharpe": np.nan,
        "long_short_maxdd": np.nan,
    }
    if group_metrics is None or group_metrics.empty:
        return out

    q_rows = [idx for idx in group_metrics.index if str(idx).startswith("Q")]
    q_rows = sorted(q_rows, key=lambda x: int(str(x)[1:]) if str(x)[1:].isdigit() else 0)
    ann = []
    ranks = []
    for i, idx in enumerate(q_rows, start=1):
        value = _finite_float(group_metrics.loc[idx].get("AnnReturn"))
        if np.isfinite(value):
            ann.append(value)
            ranks.append(i * direction_sign)
    if len(ann) >= 3:
        corr, _ = stats.spearmanr(ranks, ann)
        out["monotonic_corr"] = float(corr) if np.isfinite(corr) else np.nan

    if "LongShort" in group_metrics.index:
        ls = group_metrics.loc["LongShort"]
        out["long_short_ann_return"] = _finite_float(ls.get("AnnReturn"))
        out["long_short_sharpe"] = _finite_float(ls.get("Sharpe"))
        out["long_short_maxdd"] = _finite_float(ls.get("MaxDD"))
    return out


def compute_rank_autocorrelation(
    factor_values: pd.DataFrame | None,
    *,
    lag: int = 1,
    min_stocks: int | None = None,
) -> pd.DataFrame:
    """逐日截面 rank 自相关，衡量信号持久性和潜在换手压力。"""
    if factor_values is None or factor_values.empty:
        return pd.DataFrame(columns=["date", "rank_autocorr"]).set_index("date")

    min_stocks = int(min_stocks or _cfg("ic_analysis.min_stocks", 10))
    f = factor_values.apply(pd.to_numeric, errors="coerce")
    rows = []
    dates = pd.DatetimeIndex(f.index)
    for i in range(lag, len(dates)):
        dt = dates[i]
        prev_dt = dates[i - lag]
        pair = pd.concat([f.loc[prev_dt], f.loc[dt]], axis=1, keys=["prev", "curr"]).dropna()
        if len(pair) < min_stocks:
            continue
        corr, _ = stats.spearmanr(pair["prev"], pair["curr"])
        if np.isfinite(corr):
            rows.append({"date": dt, "rank_autocorr": float(corr)})
    if not rows:
        return pd.DataFrame(columns=["rank_autocorr"], index=pd.DatetimeIndex([], name="date"))
    return pd.DataFrame(rows).set_index("date")


def compute_quantile_turnover(
    factor_values: pd.DataFrame | None,
    *,
    direction_sign: int,
    n_groups: int | None = None,
    rebalance_mode: str | None = None,
    rebalance_days: int | None = None,
    min_stocks: int | None = None,
) -> pd.DataFrame:
    """按调仓日计算 Top/Bottom 分位的持仓替换比例。"""
    if factor_values is None or factor_values.empty:
        return pd.DataFrame(columns=["date", "top_turnover", "bottom_turnover"]).set_index("date")

    n_groups = int(n_groups or _cfg("backtest.n_groups", 5))
    rebalance_mode = str(rebalance_mode or _cfg("backtest.rebalance_mode", "month_end"))
    rebalance_days = int(rebalance_days or _cfg("backtest.rebalance_days", 5))
    min_stocks = int(min_stocks or _cfg("ic_analysis.min_stocks", 10))
    f = factor_values.apply(pd.to_numeric, errors="coerce")
    dates = pd.DatetimeIndex(f.index)
    rebal_dates = get_rebalance_dates(dates, mode=rebalance_mode, step_days=rebalance_days)

    prev_top: set[str] | None = None
    prev_bottom: set[str] | None = None
    rows = []
    for dt in rebal_dates:
        if dt not in f.index:
            continue
        row = f.loc[dt].dropna().sort_values()
        if len(row) < max(min_stocks, n_groups):
            continue
        group_size = max(1, int(np.floor(len(row) / n_groups)))
        low = set(row.head(group_size).index.astype(str))
        high = set(row.tail(group_size).index.astype(str))
        top = high if direction_sign >= 0 else low
        bottom = low if direction_sign >= 0 else high

        top_turnover = np.nan
        bottom_turnover = np.nan
        if prev_top is not None:
            denom = max(len(top), 1)
            top_turnover = 1.0 - len(top & prev_top) / denom
        if prev_bottom is not None:
            denom = max(len(bottom), 1)
            bottom_turnover = 1.0 - len(bottom & prev_bottom) / denom
        rows.append({
            "date": dt,
            "top_count": len(top),
            "bottom_count": len(bottom),
            "top_turnover": top_turnover,
            "bottom_turnover": bottom_turnover,
        })
        prev_top = top
        prev_bottom = bottom

    if not rows:
        return pd.DataFrame(columns=["top_count", "bottom_count", "top_turnover", "bottom_turnover"],
                            index=pd.DatetimeIndex([], name="date"))
    return pd.DataFrame(rows).set_index("date")


def _tradability_stats(
    rank_autocorr: pd.DataFrame,
    quantile_turnover: pd.DataFrame,
    execution_cost_bps_per_year: dict[str, float] | None,
) -> dict[str, float]:
    rank_median = np.nan
    if rank_autocorr is not None and not rank_autocorr.empty:
        rank_median = _finite_float(rank_autocorr["rank_autocorr"].median())

    top_to = np.nan
    if quantile_turnover is not None and not quantile_turnover.empty:
        top_to = _finite_float(quantile_turnover["top_turnover"].dropna().mean())

    costs = []
    for key, value in (execution_cost_bps_per_year or {}).items():
        if str(key) in {"Q1", f"Q{_cfg('backtest.n_groups', 5)}", "LongShort"}:
            x = _finite_float(value)
            if np.isfinite(x):
                costs.append(x)
    return {
        "rank_autocorr_median": rank_median,
        "top_quantile_turnover_avg": top_to,
        "cost_bps_per_year_avg": float(np.mean(costs)) if costs else np.nan,
    }


def _data_quality_stats(factor_values: pd.DataFrame | None) -> dict[str, float]:
    if factor_values is None or factor_values.empty:
        return {
            "avg_coverage": np.nan,
            "latest_coverage": np.nan,
            "zero_std_pct": np.nan,
            "avg_cross_section_std": np.nan,
        }
    f = factor_values.apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
    total = max(len(f.columns), 1)
    coverage = f.notna().sum(axis=1) / total
    std = f.std(axis=1, ddof=1).replace([np.inf, -np.inf], np.nan)
    active = coverage[coverage > 0]
    latest_coverage = float(active.iloc[-1]) if not active.empty else np.nan
    valid_std = std.dropna()
    return {
        "avg_coverage": float(coverage.mean()) if not coverage.empty else np.nan,
        "latest_coverage": latest_coverage,
        "zero_std_pct": float((valid_std <= 1e-12).mean()) if not valid_std.empty else np.nan,
        "avg_cross_section_std": float(valid_std.mean()) if not valid_std.empty else np.nan,
    }


def _thresholds() -> dict[str, float]:
    t = _cfg("factor_confidence.thresholds", {})

    def g(key: str, default: float) -> float:
        try:
            return float(getattr(t, key))
        except Exception:  # noqa: BLE001
            if isinstance(t, dict):
                return float(t.get(key, default))
            return float(default)

    return {
        "min_observations_pass": g("min_observations_pass", 120),
        "min_observations_watch": g("min_observations_watch", 60),
        "min_ic_mean_pass": g("min_ic_mean_pass", 0.020),
        "min_ic_mean_watch": g("min_ic_mean_watch", 0.010),
        "min_ic_ir_pass": g("min_ic_ir_pass", 0.150),
        "min_ic_ir_watch": g("min_ic_ir_watch", 0.070),
        "min_t_stat_pass": g("min_t_stat_pass", 2.0),
        "min_t_stat_watch": g("min_t_stat_watch", 1.5),
        "max_p_value_pass": g("max_p_value_pass", 0.05),
        "max_p_value_watch": g("max_p_value_watch", 0.10),
        "max_q_value_pass": g("max_q_value_pass", 0.10),
        "max_q_value_watch": g("max_q_value_watch", 0.20),
        "min_ic_positive_pct_pass": g("min_ic_positive_pct_pass", 0.52),
        "min_ic_positive_pct_watch": g("min_ic_positive_pct_watch", 0.50),
        "min_monthly_positive_pct_pass": g("min_monthly_positive_pct_pass", 0.55),
        "min_monthly_positive_pct_watch": g("min_monthly_positive_pct_watch", 0.50),
        "min_rolling_positive_pct_pass": g("min_rolling_positive_pct_pass", 0.55),
        "min_rolling_positive_pct_watch": g("min_rolling_positive_pct_watch", 0.50),
        "min_subperiod_positive_pct_pass": g("min_subperiod_positive_pct_pass", 0.67),
        "min_subperiod_positive_pct_watch": g("min_subperiod_positive_pct_watch", 0.34),
        "min_monotonic_corr_pass": g("min_monotonic_corr_pass", 0.70),
        "min_monotonic_corr_watch": g("min_monotonic_corr_watch", 0.30),
        "min_long_short_sharpe_pass": g("min_long_short_sharpe_pass", 0.50),
        "min_long_short_sharpe_watch": g("min_long_short_sharpe_watch", 0.20),
        "min_long_short_ann_return_pass": g("min_long_short_ann_return_pass", 0.02),
        "min_long_short_ann_return_watch": g("min_long_short_ann_return_watch", 0.00),
        "min_rank_autocorr_pass": g("min_rank_autocorr_pass", 0.70),
        "min_rank_autocorr_watch": g("min_rank_autocorr_watch", 0.40),
        "max_top_quantile_turnover_pass": g("max_top_quantile_turnover_pass", 0.75),
        "max_top_quantile_turnover_watch": g("max_top_quantile_turnover_watch", 0.90),
        "max_cost_bps_per_year_pass": g("max_cost_bps_per_year_pass", 300),
        "max_cost_bps_per_year_watch": g("max_cost_bps_per_year_watch", 800),
        "min_avg_coverage_pass": g("min_avg_coverage_pass", 0.80),
        "min_avg_coverage_watch": g("min_avg_coverage_watch", 0.60),
        "min_latest_coverage_pass": g("min_latest_coverage_pass", 0.80),
        "min_latest_coverage_watch": g("min_latest_coverage_watch", 0.60),
        "max_zero_std_pct_pass": g("max_zero_std_pct_pass", 0.02),
        "max_zero_std_pct_watch": g("max_zero_std_pct_watch", 0.05),
    }


def _category_weights() -> dict[str, float]:
    w = _cfg("factor_confidence.score_weights", {})

    def g(key: str, default: float) -> float:
        try:
            return float(getattr(w, key))
        except Exception:  # noqa: BLE001
            if isinstance(w, dict):
                return float(w.get(key, default))
            return float(default)

    weights = {
        "predictive": g("predictive", 0.35),
        "stability": g("stability", 0.25),
        "economic": g("economic", 0.20),
        "tradability": g("tradability", 0.10),
        "data_quality": g("data_quality", 0.10),
    }
    total = sum(weights.values()) or 1.0
    return {k: v / total for k, v in weights.items()}


def _build_checks(summary: dict[str, float], thresholds: dict[str, float]) -> pd.DataFrame:
    rows = [
        _check_row(
            category="predictive", check_id="n_obs", label="IC 样本天数",
            value=summary["n_obs"],
            pass_threshold=thresholds["min_observations_pass"],
            watch_threshold=thresholds["min_observations_watch"],
            unit="days",
        ),
        _check_row(
            category="predictive", check_id="ic_mean", label="方向调整后 IC 均值",
            value=summary["ic_mean"],
            pass_threshold=thresholds["min_ic_mean_pass"],
            watch_threshold=thresholds["min_ic_mean_watch"],
        ),
        _check_row(
            category="predictive", check_id="ic_ir", label="IC_IR",
            value=summary["ic_ir"],
            pass_threshold=thresholds["min_ic_ir_pass"],
            watch_threshold=thresholds["min_ic_ir_watch"],
        ),
        _check_row(
            category="predictive", check_id="t_stat", label="t 统计量",
            value=summary["t_stat"],
            pass_threshold=thresholds["min_t_stat_pass"],
            watch_threshold=thresholds["min_t_stat_watch"],
        ),
        _check_row(
            category="predictive", check_id="p_value", label="单侧 p-value",
            value=summary["p_value"],
            pass_threshold=thresholds["max_p_value_pass"],
            watch_threshold=thresholds["max_p_value_watch"],
            higher_is_better=False,
        ),
        _check_row(
            category="predictive", check_id="ic_positive_pct", label="IC 同向占比",
            value=summary["ic_positive_pct"],
            pass_threshold=thresholds["min_ic_positive_pct_pass"],
            watch_threshold=thresholds["min_ic_positive_pct_watch"],
        ),
        _check_row(
            category="stability", check_id="monthly_positive_pct", label="月度 IC 同向占比",
            value=summary["monthly_positive_pct"],
            pass_threshold=thresholds["min_monthly_positive_pct_pass"],
            watch_threshold=thresholds["min_monthly_positive_pct_watch"],
        ),
        _check_row(
            category="stability", check_id="rolling_positive_pct_63d", label="63D 滚动均值同向占比",
            value=summary["rolling_positive_pct_63d"],
            pass_threshold=thresholds["min_rolling_positive_pct_pass"],
            watch_threshold=thresholds["min_rolling_positive_pct_watch"],
        ),
        _check_row(
            category="stability", check_id="subperiod_positive_pct", label="三段样本同向占比",
            value=summary["subperiod_positive_pct"],
            pass_threshold=thresholds["min_subperiod_positive_pct_pass"],
            watch_threshold=thresholds["min_subperiod_positive_pct_watch"],
        ),
        _check_row(
            category="economic", check_id="monotonic_corr", label="分组收益单调性",
            value=summary["monotonic_corr"],
            pass_threshold=thresholds["min_monotonic_corr_pass"],
            watch_threshold=thresholds["min_monotonic_corr_watch"],
        ),
        _check_row(
            category="economic", check_id="long_short_sharpe", label="多空 Sharpe（扣费后）",
            value=summary["long_short_sharpe"],
            pass_threshold=thresholds["min_long_short_sharpe_pass"],
            watch_threshold=thresholds["min_long_short_sharpe_watch"],
        ),
        _check_row(
            category="economic", check_id="long_short_ann_return", label="多空年化收益（扣费后）",
            value=summary["long_short_ann_return"],
            pass_threshold=thresholds["min_long_short_ann_return_pass"],
            watch_threshold=thresholds["min_long_short_ann_return_watch"],
        ),
        _check_row(
            category="tradability", check_id="rank_autocorr_median", label="Rank 自相关中位数",
            value=summary["rank_autocorr_median"],
            pass_threshold=thresholds["min_rank_autocorr_pass"],
            watch_threshold=thresholds["min_rank_autocorr_watch"],
        ),
        _check_row(
            category="tradability", check_id="top_quantile_turnover_avg", label="Top 分位平均换手",
            value=summary["top_quantile_turnover_avg"],
            pass_threshold=thresholds["max_top_quantile_turnover_pass"],
            watch_threshold=thresholds["max_top_quantile_turnover_watch"],
            higher_is_better=False,
        ),
        _check_row(
            category="tradability", check_id="cost_bps_per_year_avg", label="年化交易摩擦",
            value=summary["cost_bps_per_year_avg"],
            pass_threshold=thresholds["max_cost_bps_per_year_pass"],
            watch_threshold=thresholds["max_cost_bps_per_year_watch"],
            higher_is_better=False,
            unit="bps",
        ),
        _check_row(
            category="data_quality", check_id="avg_coverage", label="平均覆盖率",
            value=summary["avg_coverage"],
            pass_threshold=thresholds["min_avg_coverage_pass"],
            watch_threshold=thresholds["min_avg_coverage_watch"],
        ),
        _check_row(
            category="data_quality", check_id="latest_coverage", label="最近覆盖率",
            value=summary["latest_coverage"],
            pass_threshold=thresholds["min_latest_coverage_pass"],
            watch_threshold=thresholds["min_latest_coverage_watch"],
        ),
        _check_row(
            category="data_quality", check_id="zero_std_pct", label="零截面标准差占比",
            value=summary["zero_std_pct"],
            pass_threshold=thresholds["max_zero_std_pct_pass"],
            watch_threshold=thresholds["max_zero_std_pct_watch"],
            higher_is_better=False,
        ),
    ]
    return pd.DataFrame(rows)


def _score_report(report: dict[str, Any], checks: pd.DataFrame) -> dict[str, Any]:
    weights = _category_weights()
    category_scores: dict[str, float] = {}
    for category, weight in weights.items():
        subset = checks[checks["category"] == category]
        category_scores[category] = float(subset["score"].mean() * 100.0) if not subset.empty else 0.0
    overall = float(sum(category_scores[k] * weights[k] for k in weights))

    t = _thresholds()
    q_value = _finite_float(report["summary"].get("q_value"))
    t_stat = _finite_float(report["summary"].get("t_stat"))
    score_gate_pass = overall >= 70.0
    fdr_gate_pass = not np.isfinite(q_value) or q_value <= t["max_q_value_watch"]
    t_gate_pass = np.isfinite(t_stat) and t_stat >= t["min_t_stat_watch"]

    if overall >= 80 and fdr_gate_pass and t_stat >= t["min_t_stat_pass"]:
        grade = "A"
    elif overall >= 65 and fdr_gate_pass and t_gate_pass:
        grade = "B"
    elif overall >= 50:
        grade = "C"
    else:
        grade = "D"

    if score_gate_pass and fdr_gate_pass and t_gate_pass:
        verdict = PASS
    elif overall >= 50 or t_gate_pass:
        verdict = WATCH
    else:
        verdict = FAIL

    report["score"] = round(overall, 2)
    report["grade"] = grade
    report["verdict"] = verdict
    report["category_scores"] = {k: round(v, 2) for k, v in category_scores.items()}
    return report


def build_factor_confidence(
    *,
    factor_name: str,
    ic: pd.Series,
    factor_values: pd.DataFrame | None,
    group_metrics: pd.DataFrame,
    factor_direction: int = 0,
    execution_cost_bps_per_year: dict[str, float] | None = None,
) -> ConfidenceArtifacts:
    """构建单因子置信评估，q-value 会在 finalize_confidence_reports 中补齐。"""
    direction_sign = int(np.sign(factor_direction)) if factor_direction else 1
    ic_s = pd.to_numeric(ic, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if factor_direction == 0 and not ic_s.empty and ic_s.mean() < 0:
        direction_sign = -1

    rank_autocorr = compute_rank_autocorrelation(factor_values)
    quantile_turnover = compute_quantile_turnover(
        factor_values,
        direction_sign=direction_sign,
    )
    summary = {
        "direction_sign": direction_sign,
        **_ic_stats(ic_s, direction_sign),
        **_stability_stats(ic_s, direction_sign),
        **_economic_stats(group_metrics, direction_sign=direction_sign),
        **_tradability_stats(rank_autocorr, quantile_turnover, execution_cost_bps_per_year),
        **_data_quality_stats(factor_values),
        "q_value": np.nan,
    }
    thresholds = _thresholds()
    checks = _build_checks(summary, thresholds)
    report = {
        "factor": factor_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "methodology_version": "factor_confidence_v1",
        "score": np.nan,
        "grade": "D",
        "verdict": FAIL,
        "category_scores": {},
        "summary": summary,
        "thresholds": thresholds,
    }
    report = _score_report(report, checks)
    return ConfidenceArtifacts(
        report=report,
        checks=checks,
        rank_autocorr=rank_autocorr,
        quantile_turnover=quantile_turnover,
    )


def finalize_confidence_reports(
    artifacts: dict[str, ConfidenceArtifacts],
) -> dict[str, ConfidenceArtifacts]:
    """对同一股票池内所有因子做 FDR 校正，并刷新评分/结论。"""
    names = list(artifacts.keys())
    q_values = _bh_q_values([
        artifacts[name].report["summary"].get("p_value")
        for name in names
    ])
    thresholds = _thresholds()
    finalized: dict[str, ConfidenceArtifacts] = {}
    for name, q_value in zip(names, q_values):
        art = artifacts[name]
        report = dict(art.report)
        summary = dict(report["summary"])
        summary["q_value"] = q_value
        report["summary"] = summary

        checks = art.checks.copy()
        q_row = _check_row(
            category="predictive",
            check_id="q_value",
            label="FDR q-value",
            value=q_value,
            pass_threshold=thresholds["max_q_value_pass"],
            watch_threshold=thresholds["max_q_value_watch"],
            higher_is_better=False,
        )
        checks = checks[checks["check_id"] != "q_value"]
        checks = pd.concat([checks, pd.DataFrame([q_row])], ignore_index=True)
        report = _score_report(report, checks)
        finalized[name] = ConfidenceArtifacts(
            report=report,
            checks=checks,
            rank_autocorr=art.rank_autocorr,
            quantile_turnover=art.quantile_turnover,
        )
    return finalized


__all__ = [
    "ConfidenceArtifacts",
    "build_factor_confidence",
    "compute_quantile_turnover",
    "compute_rank_autocorrelation",
    "finalize_confidence_reports",
]
