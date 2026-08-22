"""Single-factor IC analysis with overlap-robust inference and censor audits."""
from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd
from scipy import stats

from src.analysis.hac import newey_west_mean_stats
from src.config import CONFIG
from src.utils.logger import get_logger


log = get_logger(__name__)
CENSOR_INVALIDATE = "invalidate_cross_section"
CENSOR_FAIL = "fail"
CENSOR_POLICIES = {CENSOR_INVALIDATE, CENSOR_FAIL}


class ICOutcomeCensoringError(ValueError):
    """A factor observation has no audited forward outcome while peers do."""


def compute_forward_returns(returns_df: pd.DataFrame, periods: int) -> pd.DataFrame:
    """Compute t+1..t+N cumulative total return without losing -100% outcomes.

    Product-space compounding is deliberate: a reviewed -100% delisting/write-off
    is a valid economic outcome and must not become ``-inf`` through ``log1p``.
    """
    if periods <= 0:
        raise ValueError("periods must be positive")
    if returns_df.empty:
        return returns_df.copy()
    gross = 1.0 + returns_df.apply(pd.to_numeric, errors="coerce")
    fwd_gross = gross.rolling(
        window=periods,
        min_periods=periods,
    ).apply(np.prod, raw=True).shift(-periods)
    return fwd_gross - 1.0


def _compute_ic_row(
    factor_row: pd.Series,
    ret_row: pd.Series,
    *,
    method: str,
    min_stocks: int,
    censor_policy: str,
) -> tuple[float, dict]:
    """Compute one cross-section and explicitly diagnose missing outcomes."""
    f = pd.to_numeric(factor_row, errors="coerce")
    r = pd.to_numeric(ret_row, errors="coerce")
    factor_valid = f.notna()
    factor_count = int(factor_valid.sum())
    available = factor_valid & r.notna()
    available_count = int(available.sum())
    missing = factor_valid & r.isna()
    missing_tickers = [str(value) for value in f.index[missing]][:20]

    diagnostics = {
        "factor_count": factor_count,
        "outcome_count": available_count,
        "censored_count": int(missing.sum()),
        "censored_tickers_sample": missing_tickers,
        "status": "ok",
    }
    if factor_count < min_stocks:
        diagnostics["status"] = "insufficient_factor_cross_section"
        return np.nan, diagnostics
    if available_count == 0:
        # This is normally the right edge of the dataset where the requested
        # t+N horizon has not happened yet. It is not selective censoring.
        diagnostics["status"] = "forward_horizon_unavailable"
        return np.nan, diagnostics
    if missing.any():
        diagnostics["status"] = "selectively_censored"
        if censor_policy == CENSOR_FAIL:
            raise ICOutcomeCensoringError(
                "IC forward outcomes are selectively missing: "
                f"missing={int(missing.sum())} available={available_count} "
                f"tickers={missing_tickers}"
            )
        # Do not drop missing securities and recompute on survivors. Invalidating
        # the whole date is conservative until an audited delisting outcome is
        # supplied through resolved_forward_returns.
        return np.nan, diagnostics
    if available_count < min_stocks:
        diagnostics["status"] = "insufficient_outcome_cross_section"
        return np.nan, diagnostics

    pair = pd.DataFrame({"f": f.loc[available], "r": r.loc[available]})
    if method == "spearman":
        corr, _ = stats.spearmanr(pair["f"], pair["r"])
    elif method == "pearson":
        corr = pair["f"].corr(pair["r"])
    else:
        raise ValueError(f"Unknown IC method: {method}")
    return (float(corr) if pd.notna(corr) else np.nan), diagnostics


def compute_ic(
    factor_df: pd.DataFrame,
    returns_df: pd.DataFrame,
    periods: int | None = None,
    method: str | None = None,
    min_stocks: int | None = None,
    *,
    resolved_forward_returns: pd.DataFrame | None = None,
    censor_policy: str | None = None,
) -> pd.Series:
    """Compute daily IC without silently deleting delisted/censored securities.

    ``resolved_forward_returns`` is the extension point for an audited outcome
    builder.  When supplied it must already contain t+1..t+N economic outcomes,
    including reviewed acquisition or total-loss settlements.  Without such an
    override, a date with some observable outcomes and some missing outcomes is
    invalidated (or fails, under ``censor_policy='fail'``) rather than being
    recomputed on survivors.
    """
    periods = int(
        periods if periods is not None else CONFIG.ic_analysis.forward_periods
    )
    method = (method or CONFIG.ic_analysis.method).lower()
    min_stocks = int(
        min_stocks if min_stocks is not None else CONFIG.ic_analysis.min_stocks
    )
    policy = str(
        censor_policy
        or getattr(CONFIG.ic_analysis, "censor_policy", CENSOR_INVALIDATE)
    ).strip().lower()
    if policy not in CENSOR_POLICIES:
        raise ValueError(f"Unknown IC censor_policy: {policy}")

    fwd = (
        resolved_forward_returns.copy()
        if resolved_forward_returns is not None
        else compute_forward_returns(returns_df, periods=periods)
    )
    common_dates = factor_df.index.intersection(fwd.index)
    common_cols = factor_df.columns.intersection(fwd.columns)
    f = factor_df.loc[common_dates, common_cols]
    r = fwd.loc[common_dates, common_cols]

    ic_vals: list[float] = []
    idx: list[pd.Timestamp] = []
    diagnostic_rows: list[dict] = []
    for dt in common_dates:
        value, diagnostics = _compute_ic_row(
            f.loc[dt],
            r.loc[dt],
            method=method,
            min_stocks=min_stocks,
            censor_policy=policy,
        )
        ic_vals.append(value)
        idx.append(dt)
        diagnostic_rows.append(
            {"date": pd.Timestamp(dt).date().isoformat(), **diagnostics}
        )

    ic = pd.Series(ic_vals, index=pd.Index(idx, name="date"), name="IC").dropna()
    censored = [
        row for row in diagnostic_rows if row["status"] == "selectively_censored"
    ]
    ic.attrs["forward_periods"] = periods
    ic.attrs["censor_policy"] = policy
    ic.attrs["resolved_forward_returns"] = resolved_forward_returns is not None
    ic.attrs["censor_diagnostics"] = diagnostic_rows
    ic.attrs["selectively_censored_dates"] = len(censored)
    ic.attrs["selectively_censored_observations"] = int(
        sum(int(row["censored_count"]) for row in censored)
    )
    std = ic.std(ddof=1)
    log.info(
        "IC computed: N=%d mean=%.4f std=%.4f IR=%.4f censored_dates=%d",
        len(ic),
        ic.mean() if len(ic) else np.nan,
        std if len(ic) else np.nan,
        ic.mean() / std if len(ic) and pd.notna(std) and std else np.nan,
        len(censored),
    )
    return ic


def ic_summary(
    ic_series: pd.Series,
    ic_threshold: float = 0.02,
    *,
    hac_lags: int | None = None,
) -> dict:
    """Summarize IC using Newey-West/HAC inference for overlapping horizons."""
    s = pd.to_numeric(ic_series, errors="coerce").dropna()
    n = len(s)
    forward_periods = int(ic_series.attrs.get("forward_periods") or 1)
    lags = (
        int(hac_lags)
        if hac_lags is not None
        else max(0, forward_periods - 1)
    )
    if n == 0:
        return {
            "IC_mean": np.nan,
            "IC_std": np.nan,
            "IC_IR": np.nan,
            "IC_gt0_pct": np.nan,
            "IC_abs_gt_thr_pct": np.nan,
            "t_stat": np.nan,
            "p_value": np.nan,
            "p_value_two_sided": np.nan,
            "ci95_low": np.nan,
            "ci95_high": np.nan,
            "HAC_lags": lags,
            "N": 0,
            "censored_dates": int(ic_series.attrs.get("selectively_censored_dates") or 0),
            "censored_observations": int(ic_series.attrs.get("selectively_censored_observations") or 0),
        }

    mean = float(s.mean())
    std = float(s.std(ddof=1)) if n > 1 else np.nan
    ir = mean / std if np.isfinite(std) and std > 0 else np.nan
    hac = newey_west_mean_stats(s, max_lag=lags)
    return {
        "IC_mean": mean,
        "IC_std": std,
        "IC_IR": float(ir) if np.isfinite(ir) else np.nan,
        "IC_gt0_pct": float((s > 0).mean()),
        "IC_abs_gt_thr_pct": float((s.abs() > ic_threshold).mean()),
        "t_stat": hac.t_stat,
        "p_value": hac.p_value_one_sided,
        "p_value_two_sided": hac.p_value_two_sided,
        "ci95_low": hac.ci95_low,
        "ci95_high": hac.ci95_high,
        "HAC_lags": hac.max_lag,
        "N": int(n),
        "censored_dates": int(ic_series.attrs.get("selectively_censored_dates") or 0),
        "censored_observations": int(ic_series.attrs.get("selectively_censored_observations") or 0),
    }


def ic_summary_table(
    ic_dict: Mapping[str, pd.Series],
    ic_threshold: float = 0.02,
) -> pd.DataFrame:
    """Multi-factor IC summary; displayed t-stat is the HAC t-stat."""
    rows = []
    for name, series in ic_dict.items():
        summary = ic_summary(series, ic_threshold=ic_threshold)
        summary["factor"] = name
        rows.append(summary)
    df = pd.DataFrame(rows).set_index("factor")
    df = df[
        [
            "IC_mean",
            "IC_std",
            "IC_IR",
            "IC_gt0_pct",
            "IC_abs_gt_thr_pct",
            "t_stat",
        ]
    ]
    df = df.sort_values("IC_mean", ascending=False, key=lambda x: x.abs())
    df.columns = [
        "IC均值",
        "IC标准差",
        "IC_IR",
        "IC>0占比",
        f"|IC|>{ic_threshold}占比",
        "HAC t统计量",
    ]
    return df


__all__ = [
    "CENSOR_FAIL",
    "CENSOR_INVALIDATE",
    "ICOutcomeCensoringError",
    "compute_forward_returns",
    "compute_ic",
    "ic_summary",
    "ic_summary_table",
]
