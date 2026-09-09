"""HAC IC statistics used by the formal confidence gate."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.analysis.hac import newey_west_mean_stats
from src.config import CONFIG


def confidence_ic_stats_hac(
    ic: pd.Series,
    direction_sign: int,
) -> dict[str, float]:
    """Drop-in replacement for confidence._ic_stats using Newey-West inference."""
    s_raw = (
        pd.to_numeric(ic, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .dropna()
    )
    if s_raw.empty:
        return {
            "n_obs": 0,
            "ic_mean_raw": np.nan,
            "ic_mean": np.nan,
            "ic_std": np.nan,
            "ic_ir": np.nan,
            "t_stat": np.nan,
            "p_value": np.nan,
            "p_value_two_sided": np.nan,
            "ci95_low": np.nan,
            "ci95_high": np.nan,
            "ic_positive_pct": np.nan,
            "hac_lags": max(
                0, int(getattr(CONFIG.ic_analysis, "forward_periods", 1)) - 1
            ),
        }
    s = s_raw * int(direction_sign)
    n = int(len(s))
    mean = float(s.mean())
    std = float(s.std(ddof=1)) if n > 1 else np.nan
    lags = max(
        0,
        int(ic.attrs.get("forward_periods") or getattr(CONFIG.ic_analysis, "forward_periods", 1)) - 1,
    )
    hac = newey_west_mean_stats(s, max_lag=lags)
    return {
        "n_obs": n,
        "ic_mean_raw": float(s_raw.mean()),
        "ic_mean": mean,
        "ic_std": std,
        "ic_ir": mean / std if np.isfinite(std) and std > 0 else np.nan,
        "t_stat": hac.t_stat,
        "p_value": hac.p_value_one_sided,
        "p_value_two_sided": hac.p_value_two_sided,
        "ci95_low": hac.ci95_low,
        "ci95_high": hac.ci95_high,
        "ic_positive_pct": float((s > 0).mean()),
        "hac_lags": hac.max_lag,
    }


__all__ = ["confidence_ic_stats_hac"]
