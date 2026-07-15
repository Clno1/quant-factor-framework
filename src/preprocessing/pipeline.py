"""
因子预处理管道。

成熟多因子研究里，单因子在进入 IC / 回测 / 多因子融合前通常会做：

  1. 去极值 winsorize
     先压住异常值，避免极端数据污染后续回归和标准化。

  2. 中性化 neutralize（可选）
     用截面回归剥离行业、市值等已知风险暴露，保留残差作为“更纯”的因子。

  3. 横截面 Z-score 标准化
     最后再标准化，让每一天的因子值均值约为 0、标准差约为 1。
     这一步必须放在中性化之后，否则中性化后的残差会重新失去统一尺度。

输入/输出都是 date x ticker 的宽表。每一行是一日截面，每一列是一只股票。
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
    sector_map: pd.Series | pd.DataFrame | None = None,
    mcap_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    按配置执行完整预处理，返回同 shape 的 clean factor。

    Parameters
    ----------
    factor_df
        原始因子值宽表：index=date, columns=ticker。
    sector_map
        ticker -> sector 的映射。开启行业中性化时使用。
    mcap_df
        date x ticker 的历史市值宽表。开启市值中性化时使用。

    Notes
    -----
    当前顺序是 winsorize -> neutralize -> zscore。
    这个顺序比“先 zscore 再 neutralize”更稳，因为最终进入下游的是标准化后的残差。
    """
    if factor_df.empty:
        return factor_df.copy()

    cfg = CONFIG.preprocessing
    method = str(cfg.winsorize_method).lower()
    n = float(cfg.winsorize_n)

    # 1) 去极值：对每一天的横截面单独处理异常因子值。
    log.debug("Winsorize with method=%s n=%s", method, n)
    if method == "mad":
        out = winsorize_mad(factor_df, n=n)
    elif method in ("3sigma", "sigma"):
        out = winsorize_3sigma(factor_df, n=n)
    else:
        raise ValueError(f"Unknown winsorize method: {method}")

    # 2) 中性化：如果配置开启，就通过截面回归剥离行业/市值暴露。
    #    注意：如果没有 sector_map 或 mcap_df，neutralize 模块会明确记录 warning。
    if bool(cfg.neutralize_industry) or bool(getattr(cfg, "neutralize_mcap", False)):
        log.debug("Neutralize factor (industry=%s, mcap=%s)",
                  bool(cfg.neutralize_industry),
                  bool(getattr(cfg, "neutralize_mcap", False)))
        out = neutralize_industry(out, sector_map=sector_map, mcap_df=mcap_df)

    # 3) 最终标准化：让所有因子在每个交易日拥有可比较的尺度。
    if bool(cfg.standardize):
        log.debug("Z-score standardize after winsorization/neutralization")
        out = zscore_cs(out)

    return out


__all__ = ["preprocess_factor"]
