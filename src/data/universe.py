"""
股票池管理。

支持的股票池：
  - SP500   : S&P 500 成分股（FMP 抓取，含 GICS sector / sub_industry）
  - NASDAQ100: NASDAQ-100 成分证券（FMP 抓取，发布前另做 Nasdaq 官方对账）
  - US_ACTIVE: NASDAQ/NYSE/AMEX 活跃挂牌股票与 ETF（含海外公司 ADR）
  - MAG7    : Magnificent 7（AAPL/MSFT/GOOGL/AMZN/META/NVDA/TSLA）
  - CUSTOM  : 从配置 universe.custom_tickers 读取自定义列表

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

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
_FALLBACK_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "main/data/constituents.csv"
)

# 已知股票池（除了 SP500，其它都是固定列表）
_BUILTIN_UNIVERSES: dict[str, list[tuple[str, str, str]]] = {
    # MAG7 = Magnificent 7（华尔街 2023 年起常用术语）
    "MAG7": [
        ("AAPL",  "Apple Inc.",          "Technology"),
        ("MSFT",  "Microsoft Corp.",     "Technology"),
        ("GOOGL", "Alphabet Inc. (A)",   "Communication Services"),
        ("AMZN",  "Amazon.com Inc.",     "Consumer Discretionary"),
        ("META",  "Meta Platforms Inc.", "Communication Services"),
        ("NVDA",  "NVIDIA Corp.",        "Technology"),
        ("TSLA",  "Tesla Inc.",          "Consumer Discretionary"),
    ],
}


def _sp500_cache_path() -> Path:
    return _CACHE_DIR / "sp500.parquet"


def _nasdaq100_cache_path() -> Path:
    return _CACHE_DIR / "nasdaq100.parquet"


def _us_active_cache_path() -> Path:
    return _CACHE_DIR / "us_active.parquet"


# ============================================================
# SP500
# ============================================================

def _fetch_sp500_from_wikipedia(url: str) -> pd.DataFrame:
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
    df["ticker"] = df["ticker"].astype(str).str.strip().str.replace(".", "-", regex=False)
    df = df.drop_duplicates(subset=["ticker"]).reset_index(drop=True)
    log.info("Fetched %d S&P 500 tickers.", len(df))
    return df


def _get_sp500(force_refresh: bool = False) -> pd.DataFrame:
    cache = _sp500_cache_path()
    ensure_dir(cache)
    if not force_refresh and is_cache_fresh(cache, CONFIG.universe.cache_days):
        log.info("Loading S&P 500 universe from cache: %s", cache)
        return pd.read_parquet(cache)

    provider = str(getattr(CONFIG.data, "provider", "fmp")).lower()
    df: pd.DataFrame | None = None
    if provider == "fmp":
        try:
            from src.data.fmp import get_sp500_constituents
            df = get_sp500_constituents()
        except Exception as e:  # noqa: BLE001
            log.warning("FMP universe fetch failed (%s). Falling back to Wikipedia.", e)
    if df is None or df.empty:
        df = _fetch_sp500_from_wikipedia(CONFIG.universe.sp500_url)

    df.to_parquet(cache)
    log.info("Saved S&P 500 universe to %s (fetched at %s)", cache, datetime.now().isoformat(timespec="seconds"))
    return df


def _get_nasdaq100(force_refresh: bool = False) -> pd.DataFrame:
    cache = _nasdaq100_cache_path()
    ensure_dir(cache)
    if not force_refresh and is_cache_fresh(cache, CONFIG.universe.cache_days):
        log.info("Loading NASDAQ-100 universe from cache: %s", cache)
        return pd.read_parquet(cache)

    from src.data.nasdaq100_pit import get_verified_nasdaq100_current_constituents

    frame = get_verified_nasdaq100_current_constituents()
    frame.to_parquet(cache)
    log.info("Saved %d NASDAQ-100 securities to %s", len(frame), cache)
    return frame


def _get_us_active(force_refresh: bool = False) -> pd.DataFrame:
    cache = _us_active_cache_path()
    ensure_dir(cache)
    cache_days = min(1.0, float(getattr(CONFIG.universe, "cache_days", 1) or 1))
    if not force_refresh and is_cache_fresh(cache, cache_days):
        log.info("Loading US active universe from cache: %s", cache)
        return pd.read_parquet(cache)

    try:
        from src.data.fmp import get_us_active_equities

        df = get_us_active_equities(
            min_current_dollar_volume=0.0,
            include_etfs=True,
        )
        df.to_parquet(cache)
        log.info("Saved %d US active securities to %s", len(df), cache)
        return df
    except Exception as exc:  # noqa: BLE001
        if cache.exists():
            log.warning("US active universe refresh failed; using stale cache: %s", exc)
            return pd.read_parquet(cache)
        raise


# ============================================================
# 内置静态股票池（MAG7 等）
# ============================================================

def _get_builtin(name: str) -> pd.DataFrame:
    rows = _BUILTIN_UNIVERSES[name]
    return pd.DataFrame({
        "ticker":       [r[0] for r in rows],
        "name":         [r[1] for r in rows],
        "sector":       [r[2] for r in rows],
        "sub_industry": [None] * len(rows),
    })


# ============================================================
# 公共入口
# ============================================================

def list_universe_names() -> list[str]:
    """框架已支持的所有股票池名（用于前端切换）。"""
    return ["SP500", "NASDAQ100", "US_ACTIVE"] + sorted(
        _BUILTIN_UNIVERSES.keys()
    )


def get_universe(name: str, force_refresh: bool = False) -> pd.DataFrame:
    """
    返回指定股票池 DataFrame：ticker / name / sector / sub_industry。

    Parameters
    ----------
    name : str
        股票池名（"SP500" / "MAG7" / "CUSTOM"）。
    """
    name = name.upper()

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

    if name == "SP500":
        return _get_sp500(force_refresh=force_refresh)

    if name == "NASDAQ100":
        return _get_nasdaq100(force_refresh=force_refresh)

    if name == "US_ACTIVE":
        return _get_us_active(force_refresh=force_refresh)

    if name in _BUILTIN_UNIVERSES:
        return _get_builtin(name)

    raise ValueError(
        f"Unsupported universe: {name}. "
        f"Available: {list_universe_names() + ['CUSTOM']}"
    )


__all__ = ["get_universe", "list_universe_names"]
