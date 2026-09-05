"""
Financial Modeling Prep (FMP) API 客户端。

文档：https://site.financialmodelingprep.com/developer/docs

支持的 endpoint：
  - /stable/sp500-constituent              成分股 + sector + subSector
  - /stable/nasdaq-constituent             NASDAQ-100 当前成分
  - /stable/historical-nasdaq-constituent  NASDAQ-100 历史变更事件
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
from io import StringIO
import re
import time
from typing import Any, Iterable

import numpy as np
import pandas as pd
import requests

from src.config import CONFIG
from src.utils.logger import get_logger

log = get_logger(__name__)

_BASE_URL = "https://financialmodelingprep.com/stable"


def _normalize_us_ticker(value: Any) -> str:
    """Normalize provider punctuation without changing the security class."""
    return (
        str(value or "")
        .strip()
        .upper()
        .replace(".", "-")
        .replace("/", "-")
    )


def infer_us_security_asset_type(
    *,
    ticker: Any,
    name: Any,
    is_adr: bool = False,
    is_etf: bool = False,
    is_fund: bool = False,
) -> str:
    """Classify FMP profile rows conservatively for broad-equity research.

    FMP marks many exchange-listed instruments as non-ETF/non-fund, which is
    not equivalent to ordinary common stock.  Names catch most special
    instruments; normalized US suffixes cover terse descriptions such as
    ``AAIC-PB`` and ``AAC-UN``.
    """
    if bool(is_etf):
        return "ETF"
    if bool(is_fund):
        return "FUND"
    if bool(is_adr):
        return "ADR"

    symbol = _normalize_us_ticker(ticker)
    label = str(name or "").strip().upper()
    if re.search(r"\bUNITS?\b", label):
        return "UNIT"
    if (
        symbol.endswith(("-UN", "-U"))
        or re.fullmatch(r"[A-Z0-9]{4,}U", symbol)
        or (
            re.search(r"\bACQUISITION (?:CORP|CORPORATION|CO)\b", label)
            and re.fullmatch(r"[A-Z0-9]{2,}U", symbol)
        )
    ):
        return "UNIT"
    if (
        re.search(r"\bWARRANTS?\b|\bWTS?\.?$", label)
        or symbol.endswith(("-WT", "-WTS"))
        or re.fullmatch(r"[A-Z0-9]{4,}W", symbol)
    ):
        return "WARRANT"
    if re.search(r"\bRIGHTS?\b", label) or re.fullmatch(
        r"[A-Z0-9]{4,}R", symbol
    ):
        return "RIGHT"
    if re.search(r"\bWHEN[- ]ISSUED\b|\bTEMPORARY\b", label):
        return "TEMPORARY"
    if (
        re.search(
            r"\bPFD\b|PREFERRED (?:STOCK|SHARES)|PREFERENCE SHARES|"
            r"DEPOSITARY SHARES.*(?:PREFERRED|PFD)",
            label,
        )
        or re.search(r"-P(?:R)?[A-Z0-9]{0,2}$", symbol)
        # Nasdaq uses a fifth-character ``P`` suffix for first-class
        # preferred issues.  FMP may expose only the issuer name, so OCCIP-
        # style records cannot be identified from the profile text alone.
        or re.fullmatch(r"[A-Z0-9]{4}P", symbol)
    ):
        return "PREFERRED"
    if re.search(
        r"\b(?:SENIOR |SUBORDINATED )?NOTES?\b|\bDEBENTURES?\b|"
        r"\b(?:SR|JR|JUNIOR|SUB|SB)(?:\s+(?:SUB|FXD|FLG|MA))*\s+"
        r"(?:NT|NTS|DB|DEB)\b",
        label,
    ):
        return "NOTE"
    return "STOCK"


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

def _request(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float | None = None,
    retry: int | None = None,
) -> requests.Response:
    """Execute one authenticated GET with bounded retries."""
    params = dict(params or {})
    url = f"{_BASE_URL}{path}"
    headers = {"apikey": get_api_key()}

    request_timeout = _request_timeout() if timeout is None else float(timeout)
    request_retry = _request_retry() if retry is None else int(retry)
    last_exc: Exception | None = None

    for attempt in range(request_retry + 1):
        try:
            r = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=request_timeout,
            )
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
            return r
        except requests.HTTPError as e:
            # 4xx 直接透传，不重试
            if e.response is not None and 400 <= e.response.status_code < 500 \
                    and e.response.status_code != 429:
                raise
            last_exc = e
            if attempt < request_retry:
                wait = 1.5 * (2 ** attempt)
                log.warning(
                    "FMP %s attempt %d/%d failed: %s. Sleep %.1fs ...",
                    path, attempt + 1, request_retry + 1, e, wait,
                )
                time.sleep(wait)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt < request_retry:
                wait = 1.5 * (2 ** attempt)
                log.warning(
                    "FMP %s attempt %d/%d failed: %s. Sleep %.1fs ...",
                    path, attempt + 1, request_retry + 1, e, wait,
                )
                time.sleep(wait)
    raise RuntimeError(
        f"FMP request to {path} failed after {request_retry + 1} attempts: "
        f"{last_exc}"
    )


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    """带超时与指数退避重试的 GET。返回解析后的 JSON。"""
    data = _request(path, params=params).json()
    # FMP 偶发返回 {"Error Message": "..."} 而不是 list/dict
    if isinstance(data, dict) and "Error Message" in data:
        raise RuntimeError(f"FMP error: {data['Error Message']}")
    return data


def _records_frame(payload: Any, *, endpoint: str) -> pd.DataFrame:
    """Normalize an FMP records payload while rejecting opaque responses."""
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        if "Error Message" in payload:
            raise RuntimeError(f"FMP error: {payload['Error Message']}")
        rows = payload.get("data") or payload.get("historical") or []
    else:
        raise RuntimeError(f"FMP {endpoint} returned unexpected payload")
    if not isinstance(rows, list):
        raise RuntimeError(f"FMP {endpoint} returned non-record data")
    return pd.DataFrame(rows)


def _response_records_frame(
    response: requests.Response,
    *,
    endpoint: str,
    csv_dtype: Any = None,
) -> pd.DataFrame:
    """Decode FMP bulk endpoints that may return either JSON or CSV."""
    content_type = str(response.headers.get("content-type", "")).lower()
    if "json" in content_type:
        return _records_frame(response.json(), endpoint=endpoint)
    text = str(response.text or "").lstrip("\ufeff").strip()
    if not text:
        raise RuntimeError(f"FMP {endpoint} returned an empty response")
    if text.startswith("[") or text.startswith("{"):
        try:
            return _records_frame(response.json(), endpoint=endpoint)
        except Exception:  # Some bulk responses have an incorrect media type.
            pass
    try:
        return pd.read_csv(StringIO(text), dtype=csv_dtype)
    except Exception as exc:  # noqa: BLE001 - preserve endpoint context.
        raise RuntimeError(f"FMP {endpoint} returned invalid CSV") from exc


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
    df["ticker"] = (
        df["ticker"].astype(str).str.strip()
        .str.replace(".", "-", regex=False)
        .str.replace("/", "-", regex=False)
    )
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


def get_nasdaq100_constituents() -> pd.DataFrame:
    """Return FMP's current NASDAQ-100 constituents with classifications."""
    log.info("Fetching NASDAQ-100 constituents from FMP ...")
    data = _get("/nasdaq-constituent")
    if not isinstance(data, list) or not data:
        raise RuntimeError(
            "FMP nasdaq-constituent returned empty / unexpected payload"
        )

    frame = pd.DataFrame(data).rename(
        columns={
            "symbol": "ticker",
            "subSector": "sub_industry",
        }
    )
    required = {"ticker", "name", "sector", "sub_industry"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(
            f"FMP nasdaq-constituent missing fields: {sorted(missing)}"
        )
    optional = [
        column
        for column in ("cik", "dateFirstAdded", "founded", "headQuarter")
        if column in frame.columns
    ]
    frame = frame[["ticker", "name", "sector", "sub_industry", *optional]].copy()
    frame["ticker"] = (
        frame["ticker"]
        .astype(str)
        .str.strip()
        .str.upper()
        .str.replace(".", "-", regex=False)
        .str.replace("/", "-", regex=False)
    )
    frame = frame[frame["ticker"].ne("")].drop_duplicates("ticker")
    if not 90 <= len(frame) <= 110:
        raise RuntimeError(
            f"FMP nasdaq-constituent returned implausible row count: {len(frame)}"
        )
    log.info("FMP returned %d NASDAQ-100 securities.", len(frame))
    return frame.sort_values("ticker").reset_index(drop=True)


def get_historical_nasdaq100_constituent_changes() -> pd.DataFrame:
    """
    Return the raw FMP NASDAQ-100 constituent event history.

    FMP's ``date`` is often the announcement date or the preceding Sunday.
    ``dateAdded`` is the provider's explicit effective date and is therefore
    retained separately for the PIT adapter to validate and normalize.
    """
    log.info("Fetching historical NASDAQ-100 constituent changes from FMP ...")
    data = _get("/historical-nasdaq-constituent")
    if not isinstance(data, list) or not data:
        raise RuntimeError(
            "FMP historical-nasdaq-constituent returned empty / unexpected payload"
        )

    frame = pd.DataFrame(data)
    required = {
        "date",
        "dateAdded",
        "symbol",
        "addedSecurity",
        "removedTicker",
        "removedSecurity",
    }
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(
            "FMP historical-nasdaq-constituent missing fields: "
            f"{sorted(missing)}"
        )
    provider_dates = pd.to_datetime(frame["date"], errors="coerce")
    effective_dates = pd.to_datetime(frame["dateAdded"], errors="coerce")
    if provider_dates.isna().any() or effective_dates.isna().any():
        raise RuntimeError(
            "FMP historical-nasdaq-constituent contains invalid dates"
        )
    order = pd.DataFrame(
        {
            "effective_date": effective_dates,
            "symbol": frame["symbol"].fillna("").astype(str),
            "removed": frame["removedTicker"].fillna("").astype(str),
        }
    ).sort_values(
        ["effective_date", "symbol", "removed"],
        ascending=[False, True, True],
    )
    return frame.loc[order.index].reset_index(drop=True)


def get_stock_list() -> pd.DataFrame:
    """Return FMP's broad symbol directory without treating it as a PIT pool."""
    frame = _records_frame(_get("/stock-list"), endpoint="stock-list")
    if frame.empty or not {"symbol", "companyName"}.issubset(frame.columns):
        raise RuntimeError("FMP stock-list returned empty or missing required fields")
    frame = frame.rename(columns={"symbol": "ticker", "companyName": "name"})
    frame["ticker"] = (
        frame["ticker"].fillna("").astype(str).str.strip().str.upper().str.replace(
            ".", "-", regex=False
        )
        .str.replace("/", "-", regex=False)
    )
    frame["name"] = frame["name"].fillna("").astype(str).str.strip()
    return (
        frame.loc[frame["ticker"].ne(""), ["ticker", "name"]]
        .drop_duplicates("ticker", keep="last")
        .sort_values("ticker")
        .reset_index(drop=True)
    )


def get_delisted_companies(*, page: int = 0, limit: int = 100) -> pd.DataFrame:
    """Return one normalized page of FMP's US delisted-company directory."""
    if int(page) < 0:
        raise ValueError("page must be non-negative")
    if not 1 <= int(limit) <= 100:
        raise ValueError("limit must be between 1 and 100")
    frame = _records_frame(
        _get("/delisted-companies", {"page": int(page), "limit": int(limit)}),
        endpoint="delisted-companies",
    )
    columns = [
        "ticker", "name", "exchange", "ipo_date", "delisted_date",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    required = {"symbol", "companyName", "exchange", "ipoDate", "delistedDate"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(
            f"FMP delisted-companies missing fields: {sorted(missing)}"
        )
    frame = frame.rename(columns={
        "symbol": "ticker",
        "companyName": "name",
        "ipoDate": "ipo_date",
        "delistedDate": "delisted_date",
    })
    frame["ticker"] = (
        frame["ticker"].astype(str).str.strip().str.upper().str.replace(
            ".", "-", regex=False
        )
        .str.replace("/", "-", regex=False)
    )
    frame["exchange"] = frame["exchange"].fillna("").astype(str).str.upper()
    frame["name"] = frame["name"].fillna("").astype(str).str.strip()
    for column in ("ipo_date", "delisted_date"):
        frame[column] = pd.to_datetime(frame[column], errors="coerce").dt.normalize()
    return (
        frame.loc[frame["ticker"].ne(""), columns]
        .drop_duplicates(["ticker", "delisted_date"], keep="last")
        .sort_values(["delisted_date", "ticker"], ascending=[False, True])
        .reset_index(drop=True)
    )


def get_symbol_changes(*, limit: int = 10_000) -> pd.DataFrame:
    """Return normalized provider symbol-change events."""
    if not 1 <= int(limit) <= 100_000:
        raise ValueError("limit must be between 1 and 100000")
    frame = _records_frame(
        _get("/symbol-change", {"limit": int(limit)}),
        endpoint="symbol-change",
    )
    columns = ["date", "old_ticker", "new_ticker", "company_name"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    required = {"date", "oldSymbol", "newSymbol", "companyName"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(f"FMP symbol-change missing fields: {sorted(missing)}")
    frame = frame.rename(columns={
        "oldSymbol": "old_ticker",
        "newSymbol": "new_ticker",
        "companyName": "company_name",
    })
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    for column in ("old_ticker", "new_ticker"):
        frame[column] = (
            frame[column].fillna("").astype(str).str.strip().str.upper()
            .str.replace(".", "-", regex=False)
            .str.replace("/", "-", regex=False)
        )
    frame["company_name"] = frame["company_name"].fillna("").astype(str).str.strip()
    return (
        frame.dropna(subset=["date"])
        .loc[lambda value: value["old_ticker"].ne("") & value["new_ticker"].ne("")]
        .loc[:, columns]
        .drop_duplicates(["date", "old_ticker", "new_ticker"], keep="last")
        .sort_values(["date", "old_ticker"], ascending=[False, True])
        .reset_index(drop=True)
    )


def get_ipo_calendar(*, start: str, end: str) -> pd.DataFrame:
    """Return normalized IPO calendar rows for an inclusive date range."""
    start_ts = pd.Timestamp(start).normalize()
    end_ts = pd.Timestamp(end).normalize()
    if pd.isna(start_ts) or pd.isna(end_ts) or start_ts > end_ts:
        raise ValueError("start/end must define a valid inclusive date range")
    frame = _records_frame(
        _get("/ipos-calendar", {
            "from": start_ts.date().isoformat(),
            "to": end_ts.date().isoformat(),
        }),
        endpoint="ipos-calendar",
    )
    columns = ["date", "ticker", "company_name", "exchange"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    required = {"date", "symbol", "company", "exchange"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(f"FMP ipos-calendar missing fields: {sorted(missing)}")
    frame = frame.rename(columns={
        "symbol": "ticker",
        "company": "company_name",
    })
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["ticker"] = (
        frame["ticker"].fillna("").astype(str).str.strip().str.upper().str.replace(
            ".", "-", regex=False
        )
        .str.replace("/", "-", regex=False)
    )
    frame["company_name"] = frame["company_name"].fillna("").astype(str).str.strip()
    frame["exchange"] = frame["exchange"].fillna("").astype(str).str.upper()
    return (
        frame.dropna(subset=["date"])
        .loc[lambda value: value["ticker"].ne(""), columns]
        .drop_duplicates(["date", "ticker"], keep="last")
        .sort_values(["date", "ticker"])
        .reset_index(drop=True)
    )


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
        df["ticker"].astype(str).str.strip().str.upper()
        .str.replace(".", "-", regex=False)
        .str.replace("/", "-", regex=False)
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
    symbol = _normalize_us_ticker(ticker)
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

    asset_type = infer_us_security_asset_type(
        ticker=row.get("symbol") or symbol,
        name=row.get("companyName") or row.get("name") or "",
        is_adr=_flag("isAdr"),
        is_etf=_flag("isEtf"),
        is_fund=_flag("isFund"),
    )
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
        "country": str(row.get("country") or "").strip().upper(),
        "cik": str(row.get("cik") or "").strip(),
        "isin": str(row.get("isin") or "").strip().upper(),
        "cusip": str(row.get("cusip") or "").strip().upper(),
        "listing_date": (
            parsed_listing.normalize()
            if not pd.isna(
                parsed_listing := pd.to_datetime(
                    row.get("ipoDate"), errors="coerce"
                )
            )
            else None
        ),
        "is_adr": _flag("isAdr"),
        "is_actively_trading": _flag("isActivelyTrading", default=True),
    }


def get_company_profiles_bulk(
    *,
    parts: Iterable[int] = (0, 1, 2, 3),
) -> pd.DataFrame:
    """Return identity-relevant fields from FMP's four profile bulk parts."""
    normalized_parts = list(dict.fromkeys(int(part) for part in parts))
    if not normalized_parts or any(part < 0 for part in normalized_parts):
        raise ValueError("parts must contain non-negative integers")
    frames: list[pd.DataFrame] = []
    for part in normalized_parts:
        raw = _response_records_frame(
            _request("/profile-bulk", {"part": part}),
            endpoint=f"profile-bulk part={part}",
            csv_dtype=str,
        )
        if raw.empty:
            continue
        required = {"symbol", "companyName", "exchange"}
        missing = required - set(raw.columns)
        if missing:
            raise RuntimeError(
                f"FMP profile-bulk part={part} missing fields: {sorted(missing)}"
            )
        keep = [
            "symbol", "companyName", "exchange", "country", "currency",
            "cik", "isin", "cusip", "ipoDate", "sector", "industry",
            "isActivelyTrading", "isAdr", "isEtf", "isFund",
        ]
        frame = raw.reindex(columns=keep).copy()
        frame["source_part"] = part
        frames.append(frame)
    if not frames:
        raise RuntimeError("FMP profile-bulk returned no rows")
    frame = pd.concat(frames, ignore_index=True).rename(columns={
        "symbol": "ticker",
        "companyName": "name",
        "industry": "sub_industry",
        "ipoDate": "listing_date",
    })

    def _flag(column: str, default: bool = False) -> pd.Series:
        values = frame[column] if column in frame.columns else default
        if not isinstance(values, pd.Series):
            values = pd.Series(values, index=frame.index)
        return values.fillna(default).astype(str).str.lower().isin({"true", "1"})

    frame["ticker"] = (
        frame["ticker"].fillna("").astype(str).str.strip().str.upper().str.replace(
            ".", "-", regex=False
        )
        .str.replace("/", "-", regex=False)
    )
    for column in ("name", "sector", "sub_industry"):
        frame[column] = frame[column].fillna("").astype(str).str.strip()
    for column in ("country", "currency", "cik", "isin", "cusip"):
        frame[column] = frame[column].fillna("").astype(str).str.strip().str.upper()
    exchange = frame["exchange"].fillna("").astype(str).str.strip().str.upper()
    frame["exchange"] = exchange.map(
        lambda value: (
            "NASDAQ" if "NASDAQ" in value
            else "AMEX" if "AMEX" in value
            else "NYSE" if "NYSE" in value
            else value
        )
    )
    frame["listing_date"] = pd.to_datetime(
        frame["listing_date"], errors="coerce"
    ).dt.normalize()
    frame["is_active"] = _flag("isActivelyTrading", default=False)
    frame["is_adr"] = _flag("isAdr")
    frame["is_etf"] = _flag("isEtf")
    frame["is_fund"] = _flag("isFund")
    frame["asset_type"] = [
        infer_us_security_asset_type(
            ticker=row.ticker,
            name=row.name,
            is_adr=bool(row.is_adr),
            is_etf=bool(row.is_etf),
            is_fund=bool(row.is_fund),
        )
        for row in frame.itertuples(index=False)
    ]
    frame["trading_status"] = frame["is_active"].map(
        {True: "ACTIVE", False: "INACTIVE"}
    )
    columns = [
        "ticker", "name", "asset_type", "exchange", "country", "currency",
        "cik", "isin", "cusip", "listing_date", "sector", "sub_industry",
        "trading_status", "is_active", "is_adr", "is_etf", "is_fund",
        "source_part",
    ]
    return (
        frame.loc[frame["ticker"].ne(""), columns]
        .drop_duplicates(["ticker", "cusip", "isin"], keep="last")
        .sort_values(["ticker", "source_part"])
        .reset_index(drop=True)
    )


# ============================================================
# 日线 OHLCV
# ============================================================

_REQUIRED_COLS = ["open", "high", "low", "close", "adj_close", "volume"]


def get_eod_bulk(session: str | pd.Timestamp) -> pd.DataFrame:
    """Return one market date of bulk EOD data, accepting FMP JSON or CSV."""
    session_ts = pd.Timestamp(session).normalize()
    if pd.isna(session_ts):
        raise ValueError("session must be a valid date")
    response = _request(
        "/eod-bulk",
        {"date": session_ts.date().isoformat()},
        timeout=max(60.0, _request_timeout()),
        retry=max(5, _request_retry()),
    )
    frame = _response_records_frame(response, endpoint="eod-bulk")
    if frame.empty:
        raise RuntimeError("FMP eod-bulk returned no rows")

    adjusted_close_aliases = {
        "adj_close",
        "adjClose",
        "adjustedClose",
        "adjusted_close",
    }
    has_adjusted_close = bool(adjusted_close_aliases.intersection(frame.columns))
    frame = frame.rename(columns={
        "symbol": "ticker",
        "adjClose": "adj_close",
        "adjustedClose": "adj_close",
        "adjusted_close": "adj_close",
    })
    required = {"ticker", "open", "high", "low", "close", "volume"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(f"FMP eod-bulk missing fields: {sorted(missing)}")
    raw_ticker = frame["ticker"]
    invalid_ticker_rows = int(
        (raw_ticker.isna() | raw_ticker.fillna("").astype(str).str.strip().eq(""))
        .sum()
    )
    frame["ticker"] = (
        raw_ticker.fillna("").astype(str).str.strip().str.upper().str.replace(
            ".", "-", regex=False
        )
        .str.replace("/", "-", regex=False)
    )
    if "date" not in frame.columns:
        frame["date"] = session_ts
    else:
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
        frame["date"] = frame["date"].fillna(session_ts)
    if not has_adjusted_close or "adj_close" not in frame.columns:
        raise RuntimeError(
            "FMP eod-bulk did not provide a dividend-adjusted close. Refusing "
            "to copy executable close into adj_close; use a canonical total-return "
            "source before publishing this session."
        )
    for column in _REQUIRED_COLS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    result = (
        frame.loc[
            frame["ticker"].ne(""),
            ["date", "ticker", *_REQUIRED_COLS],
        ]
        .drop_duplicates(["date", "ticker"], keep="last")
        .sort_values("ticker")
        .reset_index(drop=True)
    )
    result.attrs["invalid_ticker_rows"] = invalid_ticker_rows
    result.attrs["price_semantics_source"] = "FMP_EOD_BULK_WITH_ADJUSTED_CLOSE"
    if invalid_ticker_rows:
        log.warning(
            "FMP eod-bulk %s dropped %d rows without a symbol",
            session_ts.date().isoformat(),
            invalid_ticker_rows,
        )
    return result


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


def get_canonical_historical_ohlcv(
    symbol: str,
    start: str,
    end: str,
) -> pd.DataFrame | None:
    """Combine executable OHLCV with a dividend-adjusted return series.

    FMP's ``full`` endpoint is split-adjusted and keeps price/volume
    economically consistent for execution and dollar-volume calculations.
    The dividend-adjusted endpoint is a separate total-return series.  A
    canonical bar therefore uses OHLCV from ``full`` and only ``adj_close``
    from the dividend-adjusted close.
    """
    executable = get_historical_ohlcv(
        symbol,
        start,
        end,
        dividend_adjusted=False,
    )
    total_return = get_historical_ohlcv(
        symbol,
        start,
        end,
        dividend_adjusted=True,
    )
    if executable is None or executable.empty:
        return None
    if total_return is None or total_return.empty:
        return None
    executable = executable.sort_index()
    total_return = total_return.sort_index()
    if not executable.index.equals(total_return.index):
        executable_only = executable.index.difference(total_return.index)
        adjusted_only = total_return.index.difference(executable.index)
        log.warning(
            "FMP canonical %s date mismatch: executable_only=%d adjusted_only=%d",
            symbol,
            len(executable_only),
            len(adjusted_only),
        )
        return None
    canonical = executable.copy()
    canonical["adj_close"] = total_return["close"]
    if canonical[_REQUIRED_COLS].isna().any(axis=None):
        return None
    canonical.index.name = "date"
    canonical = canonical[_REQUIRED_COLS]
    canonical.attrs["price_semantics_source"] = (
        "FMP_FULL_PLUS_DIVIDEND_ADJUSTED"
    )
    return canonical


def get_unadjusted_historical_close(symbol: str, start: str, end: str) -> pd.Series:
    """Observed nominal prices for PIT dollar thresholds, never return prices.

    Source: FMP stable historical-price-eod/non-split-adjusted. Missing or
    malformed data is an error; split-adjusted close is not a substitute.
    """
    data = _get("/historical-price-eod/non-split-adjusted",
                params={"symbol": symbol, "from": start, "to": end})
    rows = data if isinstance(data, list) else (data.get("historical") or data.get("data") or []) if isinstance(data, dict) else []
    frame = pd.DataFrame(rows)
    if "close" not in frame and "adjClose" in frame:
        # Some stable chart responses retain the chart-family field names;
        # units here are defined by the non-split-adjusted endpoint itself.
        frame = frame.rename(columns={"adjClose": "close"})
    if frame.empty or not {"date", "close"}.issubset(frame.columns):
        raise ValueError(f"{symbol}: missing unadjusted historical close")
    dates = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    values = pd.to_numeric(frame["close"], errors="coerce")
    if dates.isna().any() or dates.duplicated().any() or values.isna().any() or not np.isfinite(values).all() or values.le(0).any():
        raise ValueError(f"{symbol}: invalid unadjusted historical close")
    series = pd.Series(values.to_numpy(), index=pd.DatetimeIndex(dates), name="unadjusted_close").sort_index()
    series.index.name = "date"
    return series.loc[pd.Timestamp(start):pd.Timestamp(end)]


def get_coverage_historical_ohlcv(symbol: str, start: str, end: str) -> pd.DataFrame | None:
    """Canonical return/execution bars plus independently sourced nominal close."""
    canonical = get_canonical_historical_ohlcv(symbol, start, end)
    if canonical is None or canonical.empty:
        return None
    nominal = get_unadjusted_historical_close(symbol, start, end)
    canonical = canonical.copy()
    canonical["unadjusted_close"] = nominal.reindex(canonical.index)
    if canonical["unadjusted_close"].isna().any():
        raise ValueError(f"{symbol}: unadjusted prices do not cover canonical history")
    return canonical


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
    t = _normalize_us_ticker(ticker)
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
    "get_nasdaq100_constituents",
    "get_historical_nasdaq100_constituent_changes",
    "get_stock_list",
    "get_delisted_companies",
    "get_symbol_changes",
    "get_ipo_calendar",
    "get_us_active_equities",
    "get_security_profile",
    "get_company_profiles_bulk",
    "get_eod_bulk",
    "get_historical_ohlcv",
    "get_canonical_historical_ohlcv",
    "get_historical_ohlcv_complete",
    "batch_historical_ohlcv",
    "get_batch_quotes",
    "get_exchange_market_hours",
    "get_intraday_ohlcv",
    "search_symbol",
    "verify_ticker",
]
