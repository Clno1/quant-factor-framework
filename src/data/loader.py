"""
yfinance 行情下载器。

策略（v2，对抗 Yahoo Rate Limit）：
  - 使用 yfinance.download() 的 **批量模式**：一次请求拉多只股票，
    比循环逐只请求效率高一个数量级，也更不容易触发限流。
  - 将待下载 ticker 切成固定大小的 chunk（默认 50 只/批）串行下发，
    chunk 之间插入小睡眠；每个 chunk 失败时做指数退避重试。
  - 每只 ticker 独立缓存到 data/raw/ohlcv/<ticker>.parquet
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

_REQUIRED_COLS = ["open", "high", "low", "close", "adj_close", "volume"]


def _ticker_cache_path(ticker: str) -> Path:
    return _RAW_DIR / f"{ticker}.parquet"


def _normalize_single(df: pd.DataFrame) -> pd.DataFrame | None:
    """把 yfinance 返回的单只股票 DataFrame 规范化为我们要的 6 列格式。"""
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower).rename(columns={"adj close": "adj_close"})
    missing = [c for c in _REQUIRED_COLS if c not in df.columns]
    if missing:
        return None
    df = df[_REQUIRED_COLS].astype("float64").sort_index()
    df.index.name = "date"
    # 清掉整行全 NaN（yfinance 偶发）
    df = df.dropna(how="all")
    return df if not df.empty else None


def _download_batch(
    tickers: list[str],
    start: str,
    end: str,
    retry: int,
) -> dict[str, pd.DataFrame]:
    """批量下载一个 chunk；失败做指数退避。返回 {ticker: df}，缺失的 ticker 表示下载失败。"""
    import yfinance as yf  # 延迟导入

    last_exc: Exception | None = None
    for attempt in range(retry + 1):
        try:
            raw = yf.download(
                tickers=tickers,
                start=start,
                end=end,
                progress=False,
                auto_adjust=False,
                threads=False,           # 串行、批量
                group_by="ticker",
                rounding=False,
            )
            if raw is None or raw.empty:
                raise ValueError("empty response")

            out: dict[str, pd.DataFrame] = {}
            if isinstance(raw.columns, pd.MultiIndex):
                # 批量模式：columns = (ticker, field)
                got = set(raw.columns.get_level_values(0))
                for t in tickers:
                    if t not in got:
                        continue
                    sub = raw[t]
                    norm = _normalize_single(sub)
                    if norm is not None:
                        out[t] = norm
            else:
                # 单 ticker fallback
                norm = _normalize_single(raw)
                if norm is not None and len(tickers) == 1:
                    out[tickers[0]] = norm
            return out
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt < retry:
                sleep_s = 3.0 * (2 ** attempt)  # 3s, 6s, 12s ...
                log.warning(
                    "Batch download failed (attempt %d/%d): %s. Sleeping %.0fs then retry.",
                    attempt + 1, retry + 1, e, sleep_s,
                )
                time.sleep(sleep_s)
    log.error("Batch download gave up after %d attempts: %s", retry + 1, last_exc)
    return {}


def download_ohlcv(
    tickers: Iterable[str],
    start: str,
    end: str,
    chunk_size: int | None = None,
    sleep_between: float | None = None,
    retry: int | None = None,
    force: bool = False,
) -> dict[str, Path]:
    """批量下载多只股票 OHLCV 到 Parquet 缓存。返回 {ticker: 缓存路径}。"""
    tickers = [t for t in tickers if t]
    chunk_size = int(chunk_size or getattr(CONFIG.data, "chunk_size", 50))
    sleep_between = float(
        sleep_between if sleep_between is not None else getattr(CONFIG.data, "sleep_between", 1.5)
    )
    retry = int(retry if retry is not None else CONFIG.data.retry_times)
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
        "OHLCV: %d cached, %d to download (chunk=%d, sleep=%.1fs, retry=%d)",
        len(results), len(to_fetch), chunk_size, sleep_between, retry,
    )

    if not to_fetch:
        return results

    failed: list[str] = []
    chunks = [to_fetch[i:i + chunk_size] for i in range(0, len(to_fetch), chunk_size)]

    with tqdm(total=len(to_fetch), desc="Downloading", unit="ticker") as pbar:
        for idx, chunk in enumerate(chunks):
            got = _download_batch(chunk, start=start, end=end, retry=retry)
            for t in chunk:
                df = got.get(t)
                if df is None:
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
    """获取多只股票的 OHLCV DataFrame 字典，缓存缺失时自动下载。"""
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
