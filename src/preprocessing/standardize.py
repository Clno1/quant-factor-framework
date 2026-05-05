"""
横截面标准化：Z-score
"""
from __future__ import annotations

import pandas as pd


def zscore_cs(factor_df: pd.DataFrame) -> pd.DataFrame:
    """
    按日横截面 Z-score 标准化：(x - mean_d) / std_d
    """
    if factor_df.empty:
        return factor_df.copy()
    mu = factor_df.mean(axis=1)
    sd = factor_df.std(axis=1)
    # 广播：row-wise 减均值除标准差
    out = factor_df.sub(mu, axis=0).div(sd.replace(0, pd.NA), axis=0)
    return out


__all__ = ["zscore_cs"]
