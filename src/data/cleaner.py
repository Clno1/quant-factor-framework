"""
数据清洗与宽表构建。

从单股票 OHLCV 集合构造统一宽表（索引=date，列=ticker）：
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

_PROCESSED_DIR = (
    Path(CONFIG.data.processed_dir)
    if Path(CONFIG.data.processed_dir).is_absolute()
    else PROJECT_ROOT / CONFIG.data.processed_dir
)

_WIDE_FILES = {
    "close":     _PROCESSED_DIR / "close.parquet",
    "adj_close": _PROCESSED_DIR / "adj_close.parquet",
    "volume":    _PROCESSED_DIR / "volume.parquet",
    "returns":   _PROCESSED_DIR / "returns.parquet",
    "sector":    _PROCESSED_DIR / "sector.parquet",
}


def _pivot_one(series_map: dict[str, pd.Series]) -> pd.DataFrame:
    """{ticker: pd.Series(index=date)} -> 宽表 DataFrame。"""
    if not series_map:
        return pd.DataFrame()
    df = pd.concat(series_map, axis=1)
    df.columns.name = "ticker"
    df.index.name = "date"
    return df.sort_index()


def build_wide_tables(
    tickers: Iterable[str] | None = None,
    force: bool = False,
) -> dict[str, pd.DataFrame]:
    """
    构造 close/adj_close/volume/returns/sector 宽表，落盘到 data/processed/。
    若 force=False 且缓存新鲜，直接读取。
    """
    ensure_dir(_PROCESSED_DIR)
    cache_days = float(CONFIG.data.cache_days)

    if not force and all(is_cache_fresh(p, cache_days) for p in _WIDE_FILES.values()):
        log.info("Processed wide tables are fresh, loading from cache.")
        return {k: read_parquet(p) for k, p in _WIDE_FILES.items()}

    if tickers is None:
        tickers = get_universe()["ticker"].tolist()
    tickers = list(tickers)
    log.info("Building wide tables from %d tickers ...", len(tickers))

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

    # 日收益（以复权收盘价计算）
    returns_df = adj_df.pct_change()

    # 行业映射（Series -> 1 列 DataFrame 写盘，便于 read_parquet 统一处理）
    sector_series = get_sector_map()
    # 只保留当前有价格数据的 ticker
    sector_series = sector_series.reindex(close_df.columns)
    sector_df = sector_series.rename("sector").to_frame()

    # 写盘
    write_parquet(close_df,   _WIDE_FILES["close"])
    write_parquet(adj_df,     _WIDE_FILES["adj_close"])
    write_parquet(vol_df,     _WIDE_FILES["volume"])
    write_parquet(returns_df, _WIDE_FILES["returns"])
    write_parquet(sector_df,  _WIDE_FILES["sector"])

    log.info(
        "Wide tables built: shape=%s, date range=%s -> %s, tickers=%d",
        close_df.shape, close_df.index.min(), close_df.index.max(), close_df.shape[1],
    )

    return {
        "close": close_df,
        "adj_close": adj_df,
        "volume": vol_df,
        "returns": returns_df,
        "sector": sector_df,
    }


def load_wide_tables() -> dict[str, pd.DataFrame]:
    """仅从缓存读取（不触发网络）。若缓存不存在，抛 FileNotFoundError。"""
    missing = [k for k, p in _WIDE_FILES.items() if not p.exists()]
    if missing:
        raise FileNotFoundError(
            f"Processed wide tables missing: {missing}. "
            "Run build_wide_tables() first."
        )
    return {k: read_parquet(p) for k, p in _WIDE_FILES.items()}


__all__ = ["build_wide_tables", "load_wide_tables"]
