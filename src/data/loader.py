"""
yfinance 行情下载器。

策略：
  - 每只 ticker 独立缓存到 data/raw/ohlcv/<ticker>.parquet
  - 二次运行时，若缓存新鲜则跳过网络
  - 失败的 ticker 记录到 logs/failed_tickers.log，不中断流程
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

import pandas as pd
from tqdm import tqdm

from src.config import CONFIG, PROJECT_ROOT
from src.utils.io import ensure_dir, is_cache_fresh, read_parquet, write_parquet
from src.utils.logger import get_logger

log = get_logger(__name__)

_RAW_DIR = PROJECT_ROOT / CONFIG.data.raw_dir / "ohlcv" \
    if not Path(CONFIG.data.raw_dir).is_absolute() \
    else Path(CONFIG.data.raw_dir) / "ohlcv"
_FAILED_LOG = PROJECT_ROOT / "logs" / "failed_tickers.log"


def _ticker_cache_path(ticker: str) -> Path:
    return _RAW_DIR / f"{ticker}.parquet"


def _download_one(ticker: str, start: str, end: str, retry: int) -> pd.DataFrame | None:
    """下载单个 ticker（带重试）。失败返回 None。"""
    import yfinance as yf  # 延迟导入，加速模块加载

    last_exc: Exception | None = None
    for attempt in range(retry + 1):
        try:
            df = yf.download(
                ticker,
                start=start,
                end=end,
                progress=False,
                auto_adjust=False,     # 保留原始 close 与 adj close
                threads=False,         # 外层已并发
            )
            if df is None or df.empty:
                raise ValueError("empty response")

            # yfinance 返回 MultiIndex 列时摊平
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            df = df.rename(columns=str.lower)  # open / high / low / close / adj close / volume
            df = df.rename(columns={"adj close": "adj_close"})

            need = ["open", "high", "low", "close", "adj_close", "volume"]
            missing = [c for c in need if c not in df.columns]
            if missing:
                raise ValueError(f"missing columns: {missing}")

            df = df[need].astype("float64").sort_index()
            df.index.name = "date"
            return df
        except Exception as e:
            last_exc = e
            if attempt < retry:
                time.sleep(1.0 + attempt)
    log.debug("Failed %s: %s", ticker, last_exc)
    return None


def download_ohlcv(
    tickers: Iterable[str],
    start: str,
    end: str,
    max_workers: int | None = None,
    retry: int | None = None,
    force: bool = False,
) -> dict[str, Path]:
    """
    并发下载多只股票 OHLCV，写入 Parquet 缓存。返回 {ticker: 缓存路径}。
    """
    tickers = [t for t in tickers if t]
    max_workers = max_workers or int(CONFIG.data.max_workers)
    retry = retry if retry is not None else int(CONFIG.data.retry_times)
    cache_days = float(CONFIG.data.cache_days)

    ensure_dir(_RAW_DIR)
    ensure_dir(_FAILED_LOG)

    results: dict[str, Path] = {}
    to_fetch: list[str] = []

    for t in tickers:
        p = _ticker_cache_path(t)
        if not force and is_cache_fresh(p, cache_days):
            results[t] = p
        else:
            to_fetch.append(t)

    log.info("OHLCV: %d cached, %d to download (workers=%d)", len(results), len(to_fetch), max_workers)

    failed: list[str] = []
    if to_fetch:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            future_map = {
                ex.submit(_download_one, t, start, end, retry): t
                for t in to_fetch
            }
            with tqdm(total=len(future_map), desc="Downloading", unit="ticker") as pbar:
                for fut in as_completed(future_map):
                    t = future_map[fut]
                    df = fut.result()
                    if df is None or df.empty:
                        failed.append(t)
                    else:
                        p = _ticker_cache_path(t)
                        write_parquet(df, p)
                        results[t] = p
                    pbar.update(1)

    if failed:
        log.warning("Failed to download %d tickers: %s", len(failed), failed[:10])
        with _FAILED_LOG.open("a", encoding="utf-8") as f:
            ts = pd.Timestamp.now().isoformat()
            for t in failed:
                f.write(f"{ts}\t{t}\n")

    return results


def load_or_download(
    tickers: Iterable[str],
    start: str | None = None,
    end: str | None = None,
    force: bool = False,
) -> dict[str, pd.DataFrame]:
    """
    获取多只股票的 OHLCV DataFrame 字典。必要时自动下载。
    """
    start = start or CONFIG.date_range.start
    end = end or CONFIG.date_range.end
    paths = download_ohlcv(tickers, start=start, end=end, force=force)
    out: dict[str, pd.DataFrame] = {}
    for t, p in paths.items():
        try:
            out[t] = read_parquet(p)
        except Exception as e:
            log.warning("Read cache fail for %s: %s", t, e)
    return out


__all__ = ["download_ohlcv", "load_or_download"]
