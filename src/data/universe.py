"""
股票池管理。

当前支持：
  - SP500: 从 Wikipedia 抓取 S&P 500 成分股列表（附带 GICS Sector / Sub-Industry）
  - CUSTOM: 从配置读取用户自定义列表

输出统一为 pandas.DataFrame，包含列：ticker, name, sector, sub_industry
"""
from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from src.config import CONFIG, PROJECT_ROOT
from src.utils.io import ensure_dir, is_cache_fresh
from src.utils.logger import get_logger

log = get_logger(__name__)

_CACHE_DIR = PROJECT_ROOT / "data" / "raw" / "universe"

# 伪装成常见桌面浏览器，避免 Wikipedia 对默认 urllib/pandas UA 返回 403
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# 兜底数据源：datahub 维护的 S&P 500 成分股 CSV（社区常用镜像）
_FALLBACK_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "main/data/constituents.csv"
)


def _sp500_cache_path() -> Path:
    return _CACHE_DIR / "sp500.parquet"


def _fetch_sp500_from_wikipedia(url: str) -> pd.DataFrame:
    """从 Wikipedia 抓取 S&P 500 成分股（第一张表）。

    说明：pandas.read_html 默认 UA 会被 Wikipedia 403 拦截，
    所以先用 requests 带浏览器 UA 拿到 HTML，再交给 pandas 解析。
    若 Wikipedia 失败，自动降级到 datahub 的备用 CSV。
    """
    log.info("Fetching S&P 500 constituents from Wikipedia ...")
    try:
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=30)
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text), header=0)
        df = tables[0].copy()
    except Exception as e:  # noqa: BLE001
        log.warning("Wikipedia fetch failed (%s). Falling back to datahub CSV.", e)
        resp = requests.get(_FALLBACK_CSV_URL, headers=_BROWSER_HEADERS, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))

    # 列名标准化（Wikipedia 偶尔会改列名）
    rename_map = {}
    for c in df.columns:
        cl = c.lower()
        if "symbol" in cl:
            rename_map[c] = "ticker"
        elif "security" in cl:
            rename_map[c] = "name"
        elif "gics sector" in cl:
            rename_map[c] = "sector"
        elif "gics sub" in cl or "sub-industry" in cl or "sub industry" in cl:
            rename_map[c] = "sub_industry"
    df = df.rename(columns=rename_map)

    keep_cols = [c for c in ["ticker", "name", "sector", "sub_industry"] if c in df.columns]
    df = df[keep_cols].dropna(subset=["ticker"])

    # yfinance 对含点号的股票（如 BRK.B）需替换为 '-'
    df["ticker"] = df["ticker"].astype(str).str.strip().str.replace(".", "-", regex=False)
    df = df.drop_duplicates(subset=["ticker"]).reset_index(drop=True)
    log.info("Fetched %d S&P 500 tickers.", len(df))
    return df


def get_universe(force_refresh: bool = False) -> pd.DataFrame:
    """
    返回当前启用的股票池 DataFrame。列：ticker, name, sector, sub_industry（后三列可能缺失）。
    """
    name = str(CONFIG.universe.name).upper()

    if name == "CUSTOM":
        tickers = list(CONFIG.universe.custom_tickers or [])
        if not tickers:
            raise ValueError("CUSTOM universe requires non-empty `universe.custom_tickers`.")
        return pd.DataFrame({
            "ticker": [t.upper() for t in tickers],
            "name": [None] * len(tickers),
            "sector": [None] * len(tickers),
            "sub_industry": [None] * len(tickers),
        })

    if name != "SP500":
        raise ValueError(f"Unsupported universe: {name}")

    cache = _sp500_cache_path()
    ensure_dir(cache)

    if not force_refresh and is_cache_fresh(cache, CONFIG.universe.cache_days):
        log.info("Loading S&P 500 universe from cache: %s", cache)
        return pd.read_parquet(cache)

    df = _fetch_sp500_from_wikipedia(CONFIG.universe.sp500_url)
    df.to_parquet(cache)
    log.info("Saved S&P 500 universe to %s (fetched at %s)", cache, datetime.now().isoformat(timespec="seconds"))
    return df


def get_sector_map() -> pd.Series:
    """返回 ticker -> sector 的映射 Series。"""
    df = get_universe()
    if "sector" not in df.columns:
        return pd.Series(dtype="object", name="sector")
    return df.set_index("ticker")["sector"]


__all__ = ["get_universe", "get_sector_map"]
