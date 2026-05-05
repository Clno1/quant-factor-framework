"""
去极值（截面 winsorization）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _winsorize_mad_row(row: pd.Series, n: float) -> pd.Series:
    """对单截面做 MAD 去极值。"""
    x = row.astype("float64").copy()
    med = x.median()
    mad = (x - med).abs().median()
    if pd.isna(mad) or mad == 0:
        return x
    k = 1.4826  # 使 MAD 近似等于 std（正态分布假设）
    upper = med + n * k * mad
    lower = med - n * k * mad
    return x.clip(lower=lower, upper=upper)


def winsorize_mad(factor_df: pd.DataFrame, n: float = 3.0) -> pd.DataFrame:
    """
    按日横截面做 MAD 去极值（默认 ±3 MAD）。
    输入/输出：date x ticker 宽表。
    """
    if factor_df.empty:
        return factor_df.copy()
    return factor_df.apply(lambda r: _winsorize_mad_row(r, n), axis=1)


def winsorize_3sigma(factor_df: pd.DataFrame, n: float = 3.0) -> pd.DataFrame:
    """按日横截面做 μ±nσ 去极值。"""
    if factor_df.empty:
        return factor_df.copy()
    mu = factor_df.mean(axis=1)
    sd = factor_df.std(axis=1)
    lower = (mu - n * sd)
    upper = (mu + n * sd)
    lower_b = pd.DataFrame(np.broadcast_to(lower.values[:, None], factor_df.shape),
                           index=factor_df.index, columns=factor_df.columns)
    upper_b = pd.DataFrame(np.broadcast_to(upper.values[:, None], factor_df.shape),
                           index=factor_df.index, columns=factor_df.columns)
    return factor_df.clip(lower=lower_b, upper=upper_b, axis=None)


__all__ = ["winsorize_mad", "winsorize_3sigma"]
