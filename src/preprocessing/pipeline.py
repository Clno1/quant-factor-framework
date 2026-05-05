"""
预处理管道：去极值 -> 标准化 -> 中性化（可选）
"""
from __future__ import annotations

import pandas as pd

from src.config import CONFIG
from src.preprocessing.neutralize import neutralize_industry
from src.preprocessing.standardize import zscore_cs
from src.preprocessing.winsorize import winsorize_3sigma, winsorize_mad
from src.utils.logger import get_logger

log = get_logger(__name__)


def preprocess_factor(
    factor_df: pd.DataFrame,
    sector_map: pd.Series | None = None,
    mcap_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    按配置执行完整预处理。返回同 shape 的 DataFrame。
    """
    if factor_df.empty:
        return factor_df.copy()

    cfg = CONFIG.preprocessing
    method = str(cfg.winsorize_method).lower()
    n = float(cfg.winsorize_n)

    log.debug("Winsorize with method=%s n=%s", method, n)
    if method == "mad":
        out = winsorize_mad(factor_df, n=n)
    elif method in ("3sigma", "sigma"):
        out = winsorize_3sigma(factor_df, n=n)
    else:
        raise ValueError(f"Unknown winsorize method: {method}")

    if bool(cfg.standardize):
        log.debug("Z-score standardize (cross-sectional)")
        out = zscore_cs(out)

    if bool(cfg.neutralize_industry):
        log.debug("Industry neutralize (placeholder)")
        out = neutralize_industry(out, sector_map=sector_map, mcap_df=mcap_df)

    return out


__all__ = ["preprocess_factor"]
