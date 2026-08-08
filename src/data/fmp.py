"""
Financial Modeling Prep (FMP) API 客户端。

文档：https://site.financialmodelingprep.com/developer/docs

支持的 endpoint：
  - /stable/sp500-constituent              成分股 + sector + subSector
  - /stable/company-screener               美股活跃股票 / ETF 筛选
  - /stable/historical-price-eod/dividend-adjusted   日线 OHLCV（含分红/拆股复权 close）
  - /stable/historical-price-eod/full      日线 OHLCV（仅拆股复权）
  - /stable/profile                        公司基本资料（备用 sector 来源）
  - /stable/historical-chart/{interval}   分钟 / 小时 OHLCV

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
    url = f"{_BASE_URL}{path}"
    headers = {"apikey": get_api_key()}

    timeout = _request_timeout()
    retry = _request_retry()
    last_exc: Exception | None = None

    for attempt in range(retry + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
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


def get_historical_sp500_constituent_changes() -> pd.DataFrame:
    """
    Return FMP's S&P 500 addition/removal event history.

    This endpoint does *not* return complete membership snapshots.  A row may
    represent a paired replacement, an addition-only event, or a removal-only
    event.  Snapshot reconstruction therefore lives in the market-regime
    research domain, where the current constituent set and event consistency
    can be validated together.
    """
    log.info("Fetching historical S&P 500 constituent changes from FMP ...")
    data = _get("/historical-sp500-constituent")
    if not isinstance(data, list) or not data:
        raise RuntimeError(
            "FMP historical-sp500-constituent returned empty / unexpected payload"
        )

    frame = pd.DataFrame(data)
    required = {
        "date",
        "symbol",
        "addedSecurity",
        "removedTicker",
        "removedSecurity",
    }
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(
            "FMP historical-sp500-constituent missing fields: "
            f"{sorted(missing)}"
        )
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    if frame["date"].isna().any():
        raise RuntimeError(
            "FMP historical-sp500-constituent contains invalid effective dates"
        )
    return frame.sort_values(
        ["date", "symbol", "removedTicker"],
        ascending=[False, True, True],
    ).reset_index(drop=True)


def get_us_active_equities(
    *,
    min_current_dollar_volume: float = 0.0,
    limit: int = 10_000,
    include_etfs: bool = False,
) -> pd.DataFrame:
    """Return active securities listed on the main US exchanges."""
    payloads: list[pd.DataFrame] = []
    asset_types = [False, True] if include_etfs else [False]
    for exchange in ("NASDAQ", "NYSE", "AMEX"):
        for is_etf in asset_types:
            data = _get("/company-screener", params={
                "exchange": exchange,
                "isActivelyTrading": True,
                "isEtf": is_etf,
                "isFund": False,
                "limit": max(1, int(limit)),
            })
            if not isinstance(data, list):
                raise RuntimeError("FMP company-screener returned unexpected payload")
            if not data:
                continue
            payload = pd.DataFrame(data)
            if "isEtf" not in payload.columns:
                payload["isEtf"] = is_etf
            payloads.append(payload)
    if not payloads:
        raise RuntimeError("FMP company-screener returned empty payload")

    df = pd.concat(payloads, ignore_index=True).rename(columns={
        "symbol": "ticker",
        "companyName": "name",
        "industry": "sub_industry",
        "exchangeShortName": "exchange_short",
        "marketCap": "market_cap",
    })
    required = {"ticker", "price", "volume"}
    if not required.issubset(df.columns):
        raise RuntimeError(f"FMP company-screener missing fields: {sorted(required - set(df.columns))}")

    df["ticker"] = (
        df["ticker"].astype(str).str.strip().str.upper().str.replace(".", "-", regex=False)
    )
    exchange = df.get("exchange_short", pd.Series(index=df.index, dtype="object"))
    exchange = exchange.fillna(df.get("exchange", pd.Series(index=df.index, dtype="object")))
    df["exchange"] = exchange.astype(str).str.upper()
    for column in ("price", "volume", "market_cap"):
        if column not in df.columns:
            df[column] = float("nan")
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["current_dollar_volume"] = df["price"] * df["volume"]

    def _boolean_flag(column: str, default: bool = False) -> pd.Series:
        if column not in df.columns:
            return pd.Series(default, index=df.index, dtype="bool")
        return df[column].fillna(default).astype(str).str.lower().isin({"true", "1"})

    is_etf = _boolean_flag("isEtf")
    is_fund = _boolean_flag("isFund")
    df["asset_type"] = is_etf.map({True: "ETF", False: "STOCK"})
    df = df[~is_fund]
    if not include_etfs:
        df = df[~is_etf]
    if "isActivelyTrading" in df.columns:
        active = df["isActivelyTrading"].fillna(True).astype(str).str.lower().isin({"true", "1"})
        df = df[active]

    df = df[
        df["exchange"].isin({"NASDAQ", "NYSE", "AMEX"})
        & df["ticker"].str.match(r"^[A-Z][A-Z0-9.-]{0,11}$", na=False)
        & (df["price"] > 0)
        & (df["volume"] > 0)
        & (df["current_dollar_volume"] >= max(0.0, float(min_current_dollar_volume)))
    ].copy()
    for column in ("name", "sector", "sub_industry"):
        if column not in df.columns:
            df[column] = ""
        df[column] = df[column].fillna("").astype(str)

    columns = [
        "ticker", "name", "sector", "sub_industry", "asset_type", "exchange",
        "market_cap", "price", "volume", "current_dollar_volume",
    ]
    return (
        df[columns]
        .drop_duplicates(subset=["ticker"])
        .sort_values("current_dollar_volume", ascending=False)
        .reset_index(drop=True)
    )


def get_security_profile(ticker: str) -> dict[str, Any] | None:
    """Return normalized profile metadata, including an explicit asset type."""
    symbol = str(ticker or "").strip().upper().replace(".", "-")
    if not symbol:
        return None
    payload = _get("/profile", {"symbol": symbol})
    if isinstance(payload, list):
        row = payload[0] if payload else None
    else:
        row = payload if isinstance(payload, dict) else None
    if not isinstance(row, dict) or not row.get("symbol"):
        return None

    def _flag(name: str, default: bool = False) -> bool:
        return str(row.get(name, default)).strip().lower() in {"true", "1"}

    if _flag("isEtf"):
        asset_type = "ETF"
    elif _flag("isFund"):
        asset_type = "FUND"
    else:
        asset_type = "STOCK"
    return {
        "ticker": str(row.get("symbol") or symbol).strip().upper(),
        "name": str(row.get("companyName") or row.get("name") or "").strip(),
        "sector": str(row.get("sector") or "").strip(),
        "sub_industry": str(row.get("industry") or "").strip(),
        "asset_type": asset_type,
        "exchange": str(
            row.get("exchangeShortName") or row.get("exchange") or ""
        ).strip().upper(),
        "currency": str(row.get("currency") or "USD").strip().upper(),
        "is_actively_trading": _flag("isActivelyTrading", default=True),
    }


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


def get_historical_ohlcv_complete(
    symbol: str,
    start: str,
    end: str,
    *,
    dividend_adjusted: bool = True,
    chunk_years: int = 10,
) -> pd.DataFrame:
    """
    Strictly download a complete date range in bounded chunks.

    FMP's stable EOD endpoint currently caps one response at 5,000 rows.  A
    seemingly valid request for 1990-present can therefore begin around 2006
    without an error.  This wrapper keeps each request comfortably below that
    cap, merges the chunks, and fails when any requested chunk returns no data.

    Callers should choose a start date on or after the instrument's inception.
    A short gap for weekends/holidays is expected; an entirely empty chunk is
    treated as a data-contract failure rather than silently skipped.
    """
    start_ts = pd.Timestamp(start).normalize()
    end_ts = pd.Timestamp(end).normalize()
    if pd.isna(start_ts) or pd.isna(end_ts) or start_ts > end_ts:
        raise ValueError("start/end must define a valid inclusive date range")
    if int(chunk_years) < 1:
        raise ValueError("chunk_years must be positive")

    frames: list[pd.DataFrame] = []
    cursor = start_ts
    while cursor <= end_ts:
        chunk_end = min(
            cursor + pd.DateOffset(years=int(chunk_years)) - pd.Timedelta(days=1),
            end_ts,
        )
        frame = get_historical_ohlcv(
            symbol,
            cursor.strftime("%Y-%m-%d"),
            chunk_end.strftime("%Y-%m-%d"),
            dividend_adjusted=dividend_adjusted,
        )
        if frame is None or frame.empty:
            raise RuntimeError(
                f"FMP returned no {symbol} EOD data for requested chunk "
                f"{cursor.date()}..{chunk_end.date()}"
            )
        if len(frame) >= 5_000:
            raise RuntimeError(
                f"FMP returned {len(frame)} rows for {symbol} in one chunk; "
                "the response may be truncated. Reduce chunk_years."
            )
        frames.append(frame)
        cursor = chunk_end + pd.Timedelta(days=1)

    combined = pd.concat(frames).sort_index()
    combined = combined.loc[~combined.index.duplicated(keep="last")]
    if combined.empty or combined.index.has_duplicates:
        raise RuntimeError(f"Unable to build a unique complete EOD series for {symbol}")
    return combined


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
# 分钟 / 小时 OHLCV
# ============================================================

_INTRADAY_INTERVALS = {"1min", "5min", "15min", "30min", "1hour", "4hour"}


def get_intraday_ohlcv(
    symbol: str,
    *,
    interval: str = "5min",
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame | None:
    """拉取 FMP 稳定版分钟/小时 OHLCV，时间索引使用交易所本地时间。"""
    interval = str(interval).strip().lower()
    if interval not in _INTRADAY_INTERVALS:
        raise ValueError(
            f"Unsupported FMP intraday interval: {interval}. "
            f"Choose from {sorted(_INTRADAY_INTERVALS)}"
        )
    params: dict[str, Any] = {"symbol": symbol.upper().strip()}
    if start:
        params["from"] = start
    if end:
        params["to"] = end
    data = _get(f"/historical-chart/{interval}", params=params)
    rows = data if isinstance(data, list) else data.get("historical") if isinstance(data, dict) else None
    if not rows:
        return None
    df = pd.DataFrame(rows)
    required = ["date", "open", "high", "low", "close", "volume"]
    if df.empty or any(col not in df.columns for col in required):
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    for col in required[1:]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[required[1:]].dropna(subset=["open", "high", "low", "close"])
    df.index.name = "date"
    return df if not df.empty else None


def get_batch_quotes(
    symbols: Iterable[str],
    *,
    chunk_size: int = 100,
) -> pd.DataFrame:
    """Fetch normalized real-time quote snapshots in bounded symbol chunks."""
    normalized = list(dict.fromkeys(
        str(symbol).strip().upper()
        for symbol in symbols
        if str(symbol).strip()
    ))
    if not normalized:
        return pd.DataFrame()
    chunk_size = min(500, max(1, int(chunk_size)))
    rows: list[dict[str, Any]] = []
    for offset in range(0, len(normalized), chunk_size):
        chunk = normalized[offset:offset + chunk_size]
        payload = _get("/batch-quote", {"symbols": ",".join(chunk)})
        if isinstance(payload, list):
            rows.extend(item for item in payload if isinstance(item, dict))
    if not rows:
        return pd.DataFrame()

    frame = pd.DataFrame(rows)
    if "symbol" not in frame.columns:
        return pd.DataFrame()
    frame["ticker"] = frame["symbol"].astype(str).str.strip().str.upper()
    for column in (
        "price", "changePercentage", "change", "volume", "dayLow", "dayHigh",
        "marketCap", "priceAvg50", "priceAvg200", "open", "previousClose", "timestamp",
    ):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return (
        frame.dropna(subset=["ticker"])
        .drop_duplicates(subset=["ticker"], keep="last")
        .set_index("ticker", drop=False)
        .sort_index()
    )


def get_exchange_market_hours(exchange: str = "NASDAQ") -> dict[str, Any]:
    """Return FMP's current market-hours snapshot for one exchange."""
    exchange = str(exchange or "NASDAQ").strip().upper()
    payload = _get("/exchange-market-hours", {"exchange": exchange})
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise RuntimeError(f"FMP exchange-market-hours returned no data for {exchange}")
    item = dict(payload[0])
    raw_open = item.get("isMarketOpen", False)
    item["isMarketOpen"] = str(raw_open).strip().lower() in {"true", "1"}
    return item


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
    "get_historical_sp500_constituent_changes",
    "get_us_active_equities",
    "get_security_profile",
    "get_historical_ohlcv",
    "get_historical_ohlcv_complete",
    "batch_historical_ohlcv",
    "get_batch_quotes",
    "get_exchange_market_hours",
    "get_intraday_ohlcv",
    "search_symbol",
    "verify_ticker",
]
