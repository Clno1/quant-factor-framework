"""
Adhoc 回测合成：给定任意 ticker 列表，即时构造 wide 表 + 现算因子 + 合成。

  与 composer.py 的区别：
    - composer 依赖预先发布的命名股票池因子矩阵
    - adhoc 接收 MarketDataReader 返回的版本化 wide 表，在内存里跑
      factor.compute_from_wide() + preprocess，不自行触网

用途：
  - Watchlist（用户自定义 ticker 组合）跑回测时调用
  - 不污染 outputs/universes/，不需要预热 pipeline
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import pandas as pd

from src.factors import FACTOR_REGISTRY, get_factor
from src.preprocessing.pipeline import preprocess_factor
from src.preprocessing.standardize import zscore_cs
from src.strategies.definition import StrategyComponent
from src.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class AdhocResult:
    composite: pd.DataFrame                 # date × ticker（zscore 加权合成）
    returns: pd.DataFrame                   # date × ticker 日收益（喂给 quintile_backtest）
    open_prices: pd.DataFrame               # date × ticker 复权开盘价（execution=next_open 用）
    prices: pd.DataFrame                    # date × ticker 复权收盘价（可交易过滤用）
    volumes: pd.DataFrame                   # date × ticker 成交量（可交易过滤用）
    components: list[StrategyComponent]
    normalized_weights: dict[str, float]
    date_range: tuple[str, str]
    tickers_used: list[str]
    tickers_missing: list[str]              # 拉不到数据的票
    warnings: list[str] = field(default_factory=list)
    factor_raw: dict[str, pd.DataFrame] = field(default_factory=dict)
    factor_clean: dict[str, pd.DataFrame] = field(default_factory=dict)
    factor_inputs: dict[str, pd.DataFrame] = field(default_factory=dict)
    factor_contributions: dict[str, pd.DataFrame] = field(default_factory=dict)


# ----------------------------------------------------------------
# 主入口：adhoc_compose
# ----------------------------------------------------------------

def _normalize(components: list[StrategyComponent]) -> dict[str, float]:
    total = sum(abs(c.weight) for c in components)
    if total <= 0:
        raise ValueError("策略权重不能全为 0")
    return {c.factor_id: c.weight / total for c in components}


def adhoc_compose(
    components: list[StrategyComponent],
    tickers: Iterable[str],
    *,
    start: str | None = None,
    end: str | None = None,
    wide: dict[str, pd.DataFrame],
) -> AdhocResult:
    """
    给定任意 ticker 列表 + 策略成分，现算因子并合成。

    Parameters
    ----------
    components : 策略的因子 + 原始权重
    tickers    : 股票列表
    start/end  : 可选日期裁剪
    wide       : MarketDataReader 从一个已发布版本构造的宽表

    Returns
    -------
    AdhocResult
    """
    if not components:
        raise ValueError("components 为空")

    # 校验 factor_id 都已注册
    for c in components:
        if c.factor_id not in FACTOR_REGISTRY:
            raise KeyError(f"未注册的因子 id: {c.factor_id}")

    requested = [
        str(ticker).strip().upper()
        for ticker in tickers
        if str(ticker).strip()
    ]
    norm = _normalize(components)
    log.info("[adhoc] compose on %d tickers with %d factors (%s)",
             len(requested), len(components),
             {k: round(v, 3) for k, v in norm.items()})
    # 1) 使用调用方固定的数据版本；本模块没有网络或旧缓存读取能力。
    wide = {key: value.copy() for key, value in wide.items()}
    prices = wide.get("adj_close")
    if prices is None or prices.empty:
        raise ValueError("Published wide tables contain no adj_close prices")
    observed = set(str(column).upper() for column in prices.columns)
    used = sorted(set(requested) & observed)
    missing = sorted(set(requested) - observed)
    if missing:
        raise ValueError(
            f"Published custom universe is missing requested tickers: {missing}"
        )
    for key, values in list(wide.items()):
        if isinstance(values, pd.DataFrame) and isinstance(
            values.index,
            pd.DatetimeIndex,
        ):
            wide[key] = values.sort_index().reindex(columns=used)
    log.info("[adhoc] wide table built: shape=%s, used=%d, missing=%d",
             wide["adj_close"].shape, len(used), len(missing))
    if missing:
        log.warning("[adhoc] missing tickers (skipped): %s", missing)

    # 2) 对每个因子现算 + preprocess
    raw_matrices: dict[str, pd.DataFrame] = {}
    clean_matrices: dict[str, pd.DataFrame] = {}
    for c in components:
        fac = get_factor(c.factor_id)
        raw = fac.compute_from_wide(wide)
        if raw is None or raw.empty:
            raise ValueError(f"因子 {c.factor_id} compute 结果为空")
        clean = preprocess_factor(
            raw,
            sector_map=wide.get("sector"),
            mcap_df=wide.get("market_cap"),
        )
        raw_matrices[c.factor_id] = raw
        clean_matrices[c.factor_id] = clean

    # 3) 日期交集 + Zscore 加权合成
    date_ranges: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for fid, df in clean_matrices.items():
        non_empty = df.dropna(how="all")
        if non_empty.empty:
            raise ValueError(f"因子 {fid} 预处理后全空")
        date_ranges[fid] = (non_empty.index.min(), non_empty.index.max())

    common_start = max(r[0] for r in date_ranges.values())
    common_end = min(r[1] for r in date_ranges.values())
    if start is not None:
        common_start = max(common_start, pd.Timestamp(start))
    if end is not None:
        common_end = min(common_end, pd.Timestamp(end))
    if common_start >= common_end:
        raise ValueError(
            f"因子日期交集为空或过短：{common_start} ~ {common_end}，"
            "可能是股票池过小或上市时间晚"
        )

    tickers_cols = sorted(set().union(*(df.columns for df in clean_matrices.values())))
    composite: pd.DataFrame | None = None
    factor_inputs: dict[str, pd.DataFrame] = {}
    factor_contributions: dict[str, pd.DataFrame] = {}
    for fid, df in clean_matrices.items():
        aligned = df.loc[common_start:common_end].reindex(columns=tickers_cols)
        z = zscore_cs(aligned)
        weighted = z * norm[fid]
        factor_inputs[fid] = z
        factor_contributions[fid] = weighted
        composite = weighted if composite is None else composite + weighted

    assert composite is not None
    composite = composite.dropna(how="all")
    if composite.empty:
        raise ValueError("合成因子矩阵为空")
    final_index = composite.index
    final_columns = composite.columns

    # 4) 返回 returns（裁剪到同日期范围和 ticker 集合）
    returns_df = wide["returns"].loc[composite.index.min():composite.index.max(),
                                     composite.columns]
    open_df_out = wide.get("open")
    if open_df_out is None or open_df_out.empty:
        open_df_out = pd.DataFrame(index=composite.index, columns=composite.columns)
    else:
        open_df_out = open_df_out.reindex(
            index=returns_df.index, columns=composite.columns,
        )

    return AdhocResult(
        composite=composite,
        returns=returns_df,
        open_prices=open_df_out,
        prices=wide["adj_close"].reindex(index=returns_df.index, columns=composite.columns),
        volumes=wide["volume"].reindex(index=returns_df.index, columns=composite.columns),
        components=list(components),
        normalized_weights=norm,
        date_range=(
            composite.index.min().strftime("%Y-%m-%d"),
            composite.index.max().strftime("%Y-%m-%d"),
        ),
        tickers_used=used,
        tickers_missing=missing,
        factor_raw={
            fid: values.reindex(index=final_index, columns=final_columns)
            for fid, values in raw_matrices.items()
        },
        factor_clean={
            fid: values.reindex(index=final_index, columns=final_columns)
            for fid, values in clean_matrices.items()
        },
        factor_inputs={
            fid: values.reindex(index=final_index, columns=final_columns)
            for fid, values in factor_inputs.items()
        },
        factor_contributions={
            fid: values.reindex(index=final_index, columns=final_columns)
            for fid, values in factor_contributions.items()
        },
    )


__all__ = [
    "AdhocResult",
    "adhoc_compose",
]
