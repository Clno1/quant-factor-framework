"""
数据清洗与宽表构建。

从单股票 OHLCV 集合构造统一宽表（索引=date，列=ticker），
按 universe 名称分别缓存到 data/processed/<UNIVERSE>/：
  - close.parquet       : 原始收盘价
  - adj_close.parquet   : 复权收盘价（因子计算用）
  - volume.parquet      : 成交量
  - returns.parquet     : 日收益（基于 adj_close）
  - sector.parquet      : ticker -> sector 映射
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd

from src.config import CONFIG, PROJECT_ROOT
from src.data.loader import load_or_download
from src.data.universe import get_sector_map, get_universe
from src.utils.io import ensure_dir, is_cache_fresh, read_parquet, write_parquet
from src.utils.logger import get_logger

log = get_logger(__name__)

_PROCESSED_BASE = (
    Path(CONFIG.data.processed_dir)
    if Path(CONFIG.data.processed_dir).is_absolute()
    else PROJECT_ROOT / CONFIG.data.processed_dir
)


def _wide_files_for(universe: str) -> dict[str, Path]:
    base = _PROCESSED_BASE / universe
    return {
        "close":     base / "close.parquet",
        "adj_close": base / "adj_close.parquet",
        "volume":    base / "volume.parquet",
        "returns":   base / "returns.parquet",
        "sector":    base / "sector.parquet",
    }


def _pivot_one(series_map: dict[str, pd.Series]) -> pd.DataFrame:
    if not series_map:
        return pd.DataFrame()
    df = pd.concat(series_map, axis=1)
    df.columns.name = "ticker"
    df.index.name = "date"
    return df.sort_index()


def build_wide_tables(
    tickers: Iterable[str] | None = None,
    *,
    universe: str = "SP500",
    force: bool = False,
) -> dict[str, pd.DataFrame]:
    """
    构造 close/adj_close/volume/returns/sector 宽表，落盘到
    data/processed/<UNIVERSE>/。
    若 force=False 且缓存新鲜，直接读取。
    """
    files = _wide_files_for(universe)
    base_dir = _PROCESSED_BASE / universe
    ensure_dir(base_dir / ".touch")  # 创建目录
    cache_days = float(CONFIG.data.cache_days)

    if not force and all(is_cache_fresh(p, cache_days) for p in files.values()):
        cached = {k: read_parquet(p) for k, p in files.items()}
        adj = cached.get("adj_close", pd.DataFrame())
        if not adj.empty and adj.shape[1] > 0:
            log.info(
                "[%s] Processed wide tables are fresh, loading from cache. shape=%s",
                universe, adj.shape,
            )
            return cached
        log.warning("[%s] Cached wide tables are empty (shape=%s). Rebuilding ...",
                    universe, adj.shape)

    if tickers is None:
        tickers = get_universe(name=universe)["ticker"].tolist()
    tickers = list(tickers)
    log.info("[%s] Building wide tables from %d tickers ...", universe, len(tickers))

    data = load_or_download(tickers)

    close_map: dict[str, pd.Series] = {}
    adj_map: dict[str, pd.Series] = {}
    vol_map: dict[str, pd.Series] = {}
    for t, df in data.items():
        if df is None or df.empty:
            continue
        close_map[t] = df["close"]
        adj_map[t] = df["adj_close"]
        vol_map[t] = df["volume"]

    close_df = _pivot_one(close_map)
    adj_df = _pivot_one(adj_map)
    vol_df = _pivot_one(vol_map)
    returns_df = adj_df.pct_change()

    sector_series = get_sector_map(name=universe)
    sector_series = sector_series.reindex(close_df.columns)
    sector_df = sector_series.rename("sector").to_frame()

    write_parquet(close_df,   files["close"])
    write_parquet(adj_df,     files["adj_close"])
    write_parquet(vol_df,     files["volume"])
    write_parquet(returns_df, files["returns"])
    write_parquet(sector_df,  files["sector"])

    log.info(
        "[%s] Wide tables built: shape=%s, date range=%s -> %s, tickers=%d",
        universe, close_df.shape,
        close_df.index.min(), close_df.index.max(), close_df.shape[1],
    )

    return {
        "close": close_df,
        "adj_close": adj_df,
        "volume": vol_df,
        "returns": returns_df,
        "sector": sector_df,
    }


def load_wide_tables(universe: str = "SP500") -> dict[str, pd.DataFrame]:
    """仅从缓存读取（不触发网络）。若缓存不存在，抛 FileNotFoundError。"""
    files = _wide_files_for(universe)
    missing = [k for k, p in files.items() if not p.exists()]
    if missing:
        raise FileNotFoundError(
            f"[{universe}] Processed wide tables missing: {missing}. "
            "Run build_wide_tables() first."
        )
    return {k: read_parquet(p) for k, p in files.items()}


__all__ = ["build_wide_tables", "load_wide_tables"]
