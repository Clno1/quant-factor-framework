"""
Financial Modeling Prep (FMP) API 客户端。

文档：https://site.financialmodelingprep.com/developer/docs

支持的 endpoint：
  - /stable/sp500-constituent              成分股 + sector + subSector
  - /stable/historical-price-eod/dividend-adjusted   日线 OHLCV（含分红/拆股复权 close）
  - /stable/historical-price-eod/full      日线 OHLCV（仅拆股复权）
  - /stable/profile                        公司基本资料（备用 sector 来源）

API Key 加载优先级（高 → 低）：
  1. 环境变量 FMP_API_KEY
  2. 配置文件 configs/default.yaml 里 data.fmp.api_key
"""
from __future__ import annotations

import os
import time
from typing import Any, Iterable

import pandas as pd
import requests

from src.config import CONFIG
from src.utils.logger import get_logger

log = get_logger(__name__)

_BASE_URL = "https://financialmodelingprep.com/stable"


# ============================================================
# 配置访问
# ============================================================

def get_api_key() -> str:
    """获取 FMP API Key（环境变量优先 → 配置文件兜底）。"""
    key = os.environ.get("FMP_API_KEY", "").strip()
    if key:
        return key
    fmp_cfg = getattr(CONFIG.data, "fmp", None)
    if fmp_cfg is not None:
        cfg_key = str(getattr(fmp_cfg, "api_key", "") or "").strip()
        if cfg_key and cfg_key != "YOUR_FMP_API_KEY":
            return cfg_key
    raise RuntimeError(
        "FMP API key not configured. "
        "Set env FMP_API_KEY=xxx, or fill data.fmp.api_key in configs/default.yaml."
    )


def _request_timeout() -> float:
    fmp_cfg = getattr(CONFIG.data, "fmp", None)
    return float(getattr(fmp_cfg, "request_timeout", 30) or 30) if fmp_cfg else 30.0


def _request_retry() -> int:
    fmp_cfg = getattr(CONFIG.data, "fmp", None)
    return int(getattr(fmp_cfg, "retry", 3) or 3) if fmp_cfg else 3


# ============================================================
# 通用 HTTP
# ============================================================

def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    """带超时与指数退避重试的 GET。返回解析后的 JSON。"""
    params = dict(params or {})
    params["apikey"] = get_api_key()
    url = f"{_BASE_URL}{path}"

    timeout = _request_timeout()
    retry = _request_retry()
    last_exc: Exception | None = None

    for attempt in range(retry + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            # 限流
            if r.status_code == 429:
                wait = 2.0 * (2 ** attempt)
                log.warning("FMP rate limited (429). Sleeping %.0fs ...", wait)
                time.sleep(wait)
                continue
            # 4xx（除 429）不重试：通常是 ticker 不存在 / 参数错误
            if 400 <= r.status_code < 500 and r.status_code != 429:
                raise requests.HTTPError(
                    f"{r.status_code} {r.reason} for {path}",
                    response=r,
                )
            r.raise_for_status()
            data = r.json()
            # FMP 偶发返回 {"Error Message": "..."} 而不是 list/dict
            if isinstance(data, dict) and "Error Message" in data:
                raise RuntimeError(f"FMP error: {data['Error Message']}")
            return data
        except requests.HTTPError as e:
            # 4xx 直接透传，不重试
            if e.response is not None and 400 <= e.response.status_code < 500 \
                    and e.response.status_code != 429:
                raise
            last_exc = e
            if attempt < retry:
                wait = 1.5 * (2 ** attempt)
                log.warning(
                    "FMP %s attempt %d/%d failed: %s. Sleep %.1fs ...",
                    path, attempt + 1, retry + 1, e, wait,
                )
                time.sleep(wait)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt < retry:
                wait = 1.5 * (2 ** attempt)
                log.warning(
                    "FMP %s attempt %d/%d failed: %s. Sleep %.1fs ...",
                    path, attempt + 1, retry + 1, e, wait,
                )
                time.sleep(wait)
    raise RuntimeError(f"FMP request to {path} failed after {retry + 1} attempts: {last_exc}")


# ============================================================
# 成分股
# ============================================================

def get_sp500_constituents() -> pd.DataFrame:
    """
    返回 S&P 500 成分股 DataFrame：ticker / name / sector / sub_industry。
    FMP 直接带 sector，无需再爬 Wikipedia。
    """
    log.info("Fetching S&P 500 constituents from FMP ...")
    data = _get("/sp500-constituent")
    if not isinstance(data, list) or not data:
        raise RuntimeError("FMP sp500-constituent returned empty / unexpected payload")

    df = pd.DataFrame(data)
    rename = {
        "symbol":    "ticker",
        "name":      "name",
        "sector":    "sector",
        "subSector": "sub_industry",
    }
    df = df.rename(columns=rename)
    cols = [c for c in ["ticker", "name", "sector", "sub_industry"] if c in df.columns]
    df = df[cols].dropna(subset=["ticker"])
    df["ticker"] = df["ticker"].astype(str).str.strip().str.replace(".", "-", regex=False)
    df = df.drop_duplicates(subset=["ticker"]).reset_index(drop=True)
    log.info("FMP returned %d S&P 500 tickers.", len(df))
    return df


# ============================================================
# 日线 OHLCV
# ============================================================

_REQUIRED_COLS = ["open", "high", "low", "close", "adj_close", "volume"]


def get_historical_ohlcv(
    symbol: str,
    start: str,
    end: str,
    *,
    dividend_adjusted: bool = True,
) -> pd.DataFrame | None:
    """
    拉取单只股票日线。返回 6 列规范化 DataFrame，索引为 date；失败返回 None。

    Parameters
    ----------
    symbol : str
        股票代码
    start, end : str
        YYYY-MM-DD
    dividend_adjusted : bool
        True  → 用 /historical-price-eod/dividend-adjusted（含分红 + 拆股复权，对应 yfinance Adj Close）
        False → 用 /historical-price-eod/full（仅拆股复权）
    """
    path = (
        "/historical-price-eod/dividend-adjusted"
        if dividend_adjusted
        else "/historical-price-eod/full"
    )
    try:
        data = _get(path, params={"symbol": symbol, "from": start, "to": end})
    except Exception as e:  # noqa: BLE001
        log.debug("FMP fetch %s failed: %s", symbol, e)
        return None

    # FMP 当前 stable 直接返回数组；兼容 legacy 返回 {"symbol":..., "historical":[...]}
    rows = None
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("historical") or data.get("data")
    if not rows:
        return None

    df = pd.DataFrame(rows)
    if df.empty or "date" not in df.columns:
        return None

    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    # 字段映射（不同 endpoint 字段名不同）：
    #   /historical-price-eod/dividend-adjusted → adjOpen/adjHigh/adjLow/adjClose/volume
    #   /historical-price-eod/full              → open/high/low/close/volume (+ 可能的 adjClose)
    #
    # 我们的目标格式：open / high / low / close / adj_close / volume
    cols = set(df.columns)
    if "adjClose" in cols and "adjOpen" in cols:
        # dividend-adjusted endpoint：所有 OHLC 都是已复权的，没有原始价
        df = df.rename(columns={
            "adjOpen": "open",
            "adjHigh": "high",
            "adjLow": "low",
            "adjClose": "close",
        })
        # adj_close 与 close 一致（都是已分红/拆股复权）
        df["adj_close"] = df["close"]
    else:
        # full endpoint：有原始 OHLC，可能附带 adjClose
        rename_map: dict[str, str] = {}
        if "adjClose" in cols:
            rename_map["adjClose"] = "adj_close"
        elif "adjusted_close" in cols:
            rename_map["adjusted_close"] = "adj_close"
        df = df.rename(columns=rename_map)
        if "adj_close" not in df.columns and "close" in df.columns:
            df["adj_close"] = df["close"]

    missing = [c for c in _REQUIRED_COLS if c not in df.columns]
    if missing:
        log.debug("FMP %s missing columns: %s. Got: %s", symbol, missing, list(df.columns))
        return None

    df = df[_REQUIRED_COLS].astype("float64").sort_index()
    df.index.name = "date"
    df = df.dropna(how="all")
    return df if not df.empty else None


def batch_historical_ohlcv(
    symbols: Iterable[str],
    start: str,
    end: str,
    *,
    sleep_between: float = 0.0,
    dividend_adjusted: bool = True,
) -> dict[str, pd.DataFrame]:
    """
    串行拉取多只股票（FMP 没有真正的 batch endpoint，多 symbol 时逐只请求）。
    """
    out: dict[str, pd.DataFrame] = {}
    for s in symbols:
        df = get_historical_ohlcv(s, start, end, dividend_adjusted=dividend_adjusted)
        if df is not None:
            out[s] = df
        if sleep_between > 0:
            time.sleep(sleep_between)
    return out


# ============================================================
# Ticker 搜索与校验（给 Watchlist 前端用）
# ============================================================

def search_symbol(query: str, limit: int = 20) -> list[dict[str, str]]:
    """
    模糊搜索 ticker / 公司名。

    FMP stable 端点把搜索拆成了两个：
      - /search-symbol?query=xxx → 按 ticker（前缀）搜
      - /search-name?query=xxx   → 按公司名搜

    这里同时调两个，合并去重，让用户输入 "apple" / "AAPL" 都能出结果。
    只保留美股主板交易所，避免海外 / OTC 噪声。

    返回：[{ticker, name, exchange, currency}, ...]
    """
    q = (query or "").strip()
    if not q:
        return []

    results: list[dict] = []
    for path in ("/search-symbol", "/search-name"):
        try:
            data = _get(path, {"query": q, "limit": int(limit)})
            if isinstance(data, list):
                results.extend(d for d in data if isinstance(d, dict))
        except Exception as e:
            log.warning("FMP %s failed for query=%r: %s", path, q, e)

    seen: set[str] = set()
    out: list[dict[str, str]] = []
    allow_exch = {"NASDAQ", "NYSE", "AMEX", "NYSEARCA", "BATS"}
    for item in results:
        sym = str(item.get("symbol") or "").strip().upper()
        if not sym or sym in seen:
            continue
        exch = str(
            item.get("exchangeShortName") or item.get("exchange") or ""
        ).strip()
        if exch and exch not in allow_exch:
            continue
        seen.add(sym)
        out.append({
            "ticker": sym,
            "name": str(item.get("name") or "").strip(),
            "exchange": exch,
            "currency": str(item.get("currency") or "USD").strip(),
        })
        if len(out) >= limit:
            break
    return out


def verify_ticker(ticker: str) -> dict[str, str] | None:
    """
    校验 ticker 是否存在并返回基础信息（公司名等）。

    - 使用 FMP stable /profile?symbol=xxx
    - 若 profile 查不到，回退 /quote?symbol=xxx
    - 存在返回 {ticker, name, exchange, currency}
    - 不存在返回 None
    """
    t = (ticker or "").strip().upper().replace(".", "-")
    if not t:
        return None

    row: dict | None = None
    try:
        data = _get("/profile", {"symbol": t})
        if isinstance(data, list) and data:
            row = data[0]
        elif isinstance(data, dict) and data:
            row = data
    except requests.HTTPError:
        # 4xx：通常就是 ticker 不存在，静默尝试 quote 兜底
        pass
    except Exception as e:
        log.warning("verify_ticker(%s) /profile failed: %s", t, e)

    if not row:
        try:
            data = _get("/quote", {"symbol": t})
            if isinstance(data, list) and data:
                row = data[0]
            elif isinstance(data, dict) and data:
                row = data
        except Exception:
            return None

    if not isinstance(row, dict):
        return None
    sym = str(row.get("symbol") or "").strip().upper()
    if not sym:
        return None
    return {
        "ticker": sym,
        "name": str(row.get("companyName") or row.get("name") or "").strip(),
        "exchange": str(
            row.get("exchangeShortName") or row.get("exchange") or ""
        ).strip(),
        "currency": str(row.get("currency") or "USD").strip(),
    }


__all__ = [
    "get_api_key",
    "get_sp500_constituents",
    "get_historical_ohlcv",
    "batch_historical_ohlcv",
    "search_symbol",
    "verify_ticker",
]
