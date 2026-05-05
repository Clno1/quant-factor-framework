"""
中性化（Industry / Market-Cap）。

MVP 声明接口但不实现——后续可用 statsmodels 做截面 OLS：
    factor_t = Σ β_i * Industry_i + γ * log(mcap) + ε
    取残差 ε 作为中性化后的因子值。
"""
from __future__ import annotations

import pandas as pd


def neutralize_industry(
    factor_df: pd.DataFrame,
    sector_map: pd.Series | None = None,
    mcap_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    行业 / 市值中性化。

    Parameters
    ----------
    factor_df : date x ticker 因子值宽表
    sector_map : ticker -> sector Series
    mcap_df   : date x ticker 市值宽表（用于市值中性化）

    Returns
    -------
    中性化后的因子宽表（MVP 先透传，不做实际回归）。
    """
    # TODO: 实现截面 OLS 残差（行业哑变量 + log 市值）
    return factor_df.copy()


__all__ = ["neutralize_industry"]
