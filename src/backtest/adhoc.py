"""
Adhoc 回测合成：给定任意 ticker 列表，即时构造 wide 表 + 现算因子 + 合成。

与 composer.py 的区别：
  - composer 依赖预先落盘的 outputs/universes/<U>/factors/<F>/factor_values.parquet
  - adhoc 直接用 src.data.load_or_download 按 ticker 缓存 拉 OHLCV，
    然后在内存里跑 factor.compute_from_wide() + preprocess → 不落盘

用途：
  - Watchlist（用户自定义 ticker 组合）跑回测时调用
  - 不污染 outputs/universes/，不需要预热 pipeline
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import pandas as pd

from src.data.loader import load_or_download
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


# ----------------------------------------------------------------
# 构造 wide 表（内存版）
# ----------------------------------------------------------------

def _build_wide_inmem(
    tickers: Iterable[str],
    start: str | None = None,
    end: str | None = None,
) -> tuple[dict[str, pd.DataFrame], list[str], list[str]]:
    """按 ticker 逐只从缓存/网络加载 OHLCV，拼成 adj_close/volume/returns 宽表。

    返回：(wide_dict, tickers_used, tickers_missing)
    """
    tickers = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if not tickers:
        raise ValueError("tickers 为空")

    data = load_or_download(tickers, start=start, end=end)

    adj_map: dict[str, pd.Series] = {}
    open_map: dict[str, pd.Series] = {}
    vol_map: dict[str, pd.Series] = {}
    for t in tickers:
        df = data.get(t)
        if df is None or df.empty or "adj_close" not in df.columns:
            continue
        adj_map[t] = df["adj_close"]
        if "open" in df.columns:
            open_map[t] = df["open"]
        if "volume" in df.columns:
            vol_map[t] = df["volume"]

    tickers_used = sorted(adj_map.keys())
    tickers_missing = sorted(set(tickers) - set(tickers_used))
    if not tickers_used:
        raise ValueError(
            f"所有 ticker 都没有可用的价格数据：{tickers}"
        )

    adj_df = pd.concat(adj_map, axis=1).sort_index()
    adj_df.columns.name = "ticker"
    adj_df.index.name = "date"

    open_df = (
        pd.concat(open_map, axis=1).sort_index()
        if open_map else pd.DataFrame(index=adj_df.index, columns=adj_df.columns)
    )
    if not open_df.empty:
        open_df.columns.name = "ticker"
        open_df.index.name = "date"
        # 对齐到 adj_df 的列
        open_df = open_df.reindex(columns=adj_df.columns)

    vol_df = (
        pd.concat(vol_map, axis=1).sort_index()
        if vol_map else pd.DataFrame(index=adj_df.index)
    )
    if not vol_df.empty:
        vol_df.columns.name = "ticker"
        vol_df.index.name = "date"
    returns_df = adj_df.pct_change()

    wide = {
        "adj_close": adj_df,
        "close": adj_df,       # 部分因子 compute_from_wide 会找 close，用 adj 兜底
        "open": open_df,
        "volume": vol_df,
        "returns": returns_df,
        "sector": pd.DataFrame(index=adj_df.columns),  # 空 sector；preprocess 会跳过
    }
    return wide, tickers_used, tickers_missing


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
) -> AdhocResult:
    """
    给定任意 ticker 列表 + 策略成分，现算因子并合成。

    Parameters
    ----------
    components : 策略的因子 + 原始权重
    tickers    : 股票列表
    start/end  : 可选日期裁剪（不传则读配置默认 5Y）

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

    norm = _normalize(components)
    log.info("[adhoc] compose on %d tickers with %d factors (%s)",
             len(list(tickers)), len(components),
             {k: round(v, 3) for k, v in norm.items()})

    # 1) 构造 wide
    wide, used, missing = _build_wide_inmem(tickers, start=start, end=end)
    log.info("[adhoc] wide table built: shape=%s, used=%d, missing=%d",
             wide["adj_close"].shape, len(used), len(missing))
    if missing:
        log.warning("[adhoc] missing tickers (skipped): %s", missing)

    # 2) 对每个因子现算 + preprocess
    clean_matrices: dict[str, pd.DataFrame] = {}
    for c in components:
        fac = get_factor(c.factor_id)
        raw = fac.compute_from_wide(wide)
        if raw is None or raw.empty:
            raise ValueError(f"因子 {c.factor_id} compute 结果为空")
        # 空 sector 时 preprocess_factor 会跳过 sector 中性化
        clean = preprocess_factor(raw, sector_map=None)
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
    for fid, df in clean_matrices.items():
        aligned = df.loc[common_start:common_end].reindex(columns=tickers_cols)
        z = zscore_cs(aligned)
        weighted = z * norm[fid]
        composite = weighted if composite is None else composite.add(weighted, fill_value=0.0)

    assert composite is not None
    composite = composite.dropna(how="all")
    if composite.empty:
        raise ValueError("合成因子矩阵为空")

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
    )


__all__ = [
    "AdhocResult",
    "adhoc_compose",
]
