"""Small dependency-free Newey-West/HAC inference helpers."""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from scipy import stats


@dataclass(frozen=True)
class HACMeanStats:
    n_obs: int
    mean: float
    std: float
    max_lag: int
    standard_error: float
    t_stat: float
    p_value_one_sided: float
    p_value_two_sided: float
    ci95_low: float
    ci95_high: float

    def to_dict(self) -> dict:
        return asdict(self)


def newey_west_mean_stats(
    values: pd.Series,
    *,
    max_lag: int,
) -> HACMeanStats:
    """Estimate mean inference with Bartlett-kernel Newey-West covariance.

    The long-run variance estimator uses autocovariances divided by N, matching
    the standard HAC estimator for the intercept-only regression.  Inference is
    asymptotic normal, which is the conventional Newey-West interpretation.
    """
    s = (
        pd.to_numeric(values, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .dropna()
        .astype("float64")
    )
    n = int(len(s))
    lag = max(0, int(max_lag))
    if n == 0:
        return HACMeanStats(0, np.nan, np.nan, lag, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan)
    lag = min(lag, max(0, n - 1))
    x = s.to_numpy(dtype=float)
    mean = float(np.mean(x))
    std = float(np.std(x, ddof=1)) if n > 1 else np.nan
    centered = x - mean
    gamma0 = float(np.dot(centered, centered) / n)
    long_run_variance = gamma0
    for j in range(1, lag + 1):
        gamma = float(np.dot(centered[j:], centered[:-j]) / n)
        weight = 1.0 - j / (lag + 1.0)
        long_run_variance += 2.0 * weight * gamma
    # Small numerical negatives can occur from finite precision. A materially
    # negative LRV is not silently converted into a valid t statistic.
    if long_run_variance < -1e-14:
        se = np.nan
    else:
        se = float(np.sqrt(max(long_run_variance, 0.0) / n))
    t_stat = mean / se if np.isfinite(se) and se > 0 else np.nan
    p_two = (
        float(2.0 * stats.norm.sf(abs(t_stat)))
        if np.isfinite(t_stat)
        else np.nan
    )
    p_one = float(stats.norm.sf(t_stat)) if np.isfinite(t_stat) else np.nan
    z = float(stats.norm.ppf(0.975))
    ci_low = mean - z * se if np.isfinite(se) else np.nan
    ci_high = mean + z * se if np.isfinite(se) else np.nan
    return HACMeanStats(
        n_obs=n,
        mean=mean,
        std=std,
        max_lag=lag,
        standard_error=se,
        t_stat=float(t_stat) if np.isfinite(t_stat) else np.nan,
        p_value_one_sided=p_one,
        p_value_two_sided=p_two,
        ci95_low=float(ci_low) if np.isfinite(ci_low) else np.nan,
        ci95_high=float(ci_high) if np.isfinite(ci_high) else np.nan,
    )


__all__ = ["HACMeanStats", "newey_west_mean_stats"]
