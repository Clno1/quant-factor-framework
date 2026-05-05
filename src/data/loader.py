"""
行情下载器（数据源：Financial Modeling Prep）。

策略：
  - 通过 src.data.fmp 模块调用 FMP API
  - 每只 ticker 独立缓存到 data/raw/ohlcv/<ticker>.parquet
  - 进度条 chunk 仅用于显示，不再做真正批量
  - 失败的 ticker 记录到 logs/failed_tickers.log，不中断流程
"""
from __future__ import annotations

import time
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


def _provider_name() -> str:
    return str(getattr(CONFIG.data, "provider", "fmp")).lower()


def download_ohlcv(
    tickers: Iterable[str],
    start: str,
    end: str,
    chunk_size: int | None = None,
    sleep_between: float | None = None,
    retry: int | None = None,
    force: bool = False,
) -> dict[str, Path]:
    """逐只下载 OHLCV 到 Parquet 缓存。返回 {ticker: 缓存路径}。"""
    from src.data.fmp import get_historical_ohlcv

    provider = _provider_name()
    if provider != "fmp":
        raise ValueError(
            f"Unsupported data provider: {provider!r}. Only 'fmp' is supported now."
        )

    tickers = [t for t in tickers if t]
    chunk_size = int(chunk_size or getattr(CONFIG.data, "chunk_size", 100))
    sleep_between = float(
        sleep_between if sleep_between is not None else getattr(CONFIG.data, "sleep_between", 0.0)
    )
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

    log.info(
        "OHLCV [%s]: %d cached, %d to download",
        provider, len(results), len(to_fetch),
    )

    if not to_fetch:
        return results

    failed: list[str] = []
    chunks = [to_fetch[i:i + chunk_size] for i in range(0, len(to_fetch), chunk_size)]

    with tqdm(total=len(to_fetch), desc="Downloading", unit="ticker") as pbar:
        for idx, chunk in enumerate(chunks):
            for t in chunk:
                df = get_historical_ohlcv(t, start, end, dividend_adjusted=True)
                if df is None or df.empty:
                    failed.append(t)
                else:
                    p = _ticker_cache_path(t)
                    write_parquet(df, p)
                    results[t] = p
                pbar.update(1)
            if idx < len(chunks) - 1 and sleep_between > 0:
                time.sleep(sleep_between)

    if failed:
        log.warning("Failed to download %d tickers (first 10 shown): %s", len(failed), failed[:10])
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
    start = start or CONFIG.date_range.start
    end = end or CONFIG.date_range.end
    paths = download_ohlcv(tickers, start=start, end=end, force=force)
    out: dict[str, pd.DataFrame] = {}
    for t, p in paths.items():
        try:
            out[t] = read_parquet(p)
        except Exception as e:  # noqa: BLE001
            log.warning("Read cache fail for %s: %s", t, e)
    return out


__all__ = ["download_ohlcv", "load_or_download"]
