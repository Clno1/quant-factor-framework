"""
横截面因子中性化。

这里的“中性化”是每天做一次截面回归：

    factor_i = const + industry_dummies_i + log_mcap_i + residual_i

然后用 residual_i 作为新的因子值。这样可以剥离行业、市值等已知风险暴露。
如果某一天有效股票太少，或者缺少 sector / market cap 数据，则不会强行回归。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.config import CONFIG
from src.utils.logger import get_logger

log = get_logger(__name__)


def _sector_series(sector_map: pd.Series | pd.DataFrame | None) -> pd.Series | None:
    if sector_map is None:
        return None
    if isinstance(sector_map, pd.DataFrame):
        if sector_map.empty:
            return None
        if "sector" in sector_map.columns:
            s = sector_map["sector"]
        else:
            s = sector_map.iloc[:, 0]
    else:
        s = sector_map
    s = s.dropna().astype(str)
    return s if not s.empty else None


def _neutralize_row(
    factor_row: pd.Series,
    sector: pd.Series | None,
    mcap_row: pd.Series | None,
    *,
    use_industry: bool,
    use_mcap: bool,
    min_obs: int,
) -> tuple[pd.Series, bool]:
    """
    对单个交易日做一次截面中性化。

    Returns
    -------
    (neutralized_row, applied)
        applied=True 表示这一天真的完成了回归并返回残差。
        applied=False 表示数据不足/配置不可用，返回原始截面。
    """
    y = factor_row.astype("float64")
    valid = y.notna()
    parts: list[pd.DataFrame | pd.Series] = []

    if use_industry and sector is not None:
        sec = sector.reindex(y.index)
        valid &= sec.notna()
        dummies = pd.get_dummies(sec, dtype="float64")
        if dummies.shape[1] > 1:
            parts.append(dummies.iloc[:, 1:])

    if use_mcap and mcap_row is not None:
        mcap = mcap_row.reindex(y.index).astype("float64")
        mcap = mcap.where(mcap > 0)
        valid &= mcap.notna()
        parts.append(np.log(mcap).rename("log_mcap"))

    if not parts:
        return y, False

    X = pd.concat(parts, axis=1).loc[valid]
    yy = y.loc[valid]
    if len(yy) < max(min_obs, X.shape[1] + 2):
        return y, False

    X = pd.concat(
        [pd.Series(1.0, index=X.index, name="const"), X.astype("float64")],
        axis=1,
    )
    try:
        beta, *_ = np.linalg.lstsq(X.to_numpy(), yy.to_numpy(), rcond=None)
    except np.linalg.LinAlgError:
        return y, False
    resid = yy - X.to_numpy().dot(beta)
    out = pd.Series(np.nan, index=y.index, dtype="float64")
    out.loc[valid] = resid
    return out, True


def neutralize_industry(
    factor_df: pd.DataFrame,
    sector_map: pd.Series | pd.DataFrame | None = None,
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
    中性化后的因子宽表。
    """
    if factor_df.empty:
        return factor_df.copy()

    use_industry = bool(getattr(CONFIG.preprocessing, "neutralize_industry", False))
    use_mcap = bool(getattr(CONFIG.preprocessing, "neutralize_mcap", False))
    if not use_industry and not use_mcap:
        return factor_df.copy()

    sector = _sector_series(sector_map) if use_industry else None
    if use_industry and sector is None:
        log.warning("Industry neutralization requested but sector_map is missing.")

    has_mcap = mcap_df is not None and not mcap_df.empty
    if use_mcap and not has_mcap:
        log.warning("Market-cap neutralization requested but mcap_df is missing.")

    active_industry = bool(use_industry and sector is not None)
    active_mcap = bool(use_mcap and has_mcap)
    if not active_industry and not active_mcap:
        log.warning(
            "Neutralization requested but no usable exposure data is available "
            "(industry=%s, mcap=%s). Returning factor unchanged.",
            use_industry,
            use_mcap,
        )
        return factor_df.copy()

    min_obs = int(getattr(CONFIG.preprocessing, "neutralize_min_obs", 30))
    rows = []
    applied_count = 0
    for dt, row in factor_df.iterrows():
        mcap_row = mcap_df.loc[dt] if has_mcap and dt in mcap_df.index else None
        neutralized, applied = _neutralize_row(
            row,
            sector,
            mcap_row,
            use_industry=active_industry,
            use_mcap=active_mcap,
            min_obs=min_obs,
        )
        rows.append(neutralized)
        applied_count += int(applied)
    skipped = len(rows) - applied_count
    log.info(
        "Neutralization finished: applied=%d skipped=%d "
        "(industry=%s, mcap=%s, min_obs=%d)",
        applied_count,
        skipped,
        active_industry,
        active_mcap,
        min_obs,
    )
    return pd.DataFrame(rows, index=factor_df.index, columns=factor_df.columns)


__all__ = ["neutralize_industry"]
