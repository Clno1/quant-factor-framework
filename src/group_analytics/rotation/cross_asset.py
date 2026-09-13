"""Cross-asset ETF proxy bar.

Display only. Never writes production, never changes assign_priority.
These are ETF total-return proxies, not a dollar index or gold/oil spot.
"""
from __future__ import annotations

import math

import pandas as pd

from .engine import _return

CROSS_ASSET_VERSION = "etf-proxy-bar-v1"
CROSS_ASSET_NOTE = "ETF代理的绝对收益，不是美元指数或黄金/原油现货"
WINDOWS = (5, 20, 60)
PROXIES = (
    {"id": "tlt", "symbol": "TLT", "name": "长债 ETF 代理", "observes": "长期美债"},
    {"id": "ief", "symbol": "IEF", "name": "中债 ETF 代理", "observes": "中期美债"},
    {"id": "uup", "symbol": "UUP", "name": "美元 ETF 代理", "observes": "美元"},
    {"id": "gld", "symbol": "GLD", "name": "黄金 ETF 代理", "observes": "黄金"},
    {"id": "uso", "symbol": "USO", "name": "原油 ETF 代理", "observes": "原油"},
    {"id": "dbc", "symbol": "DBC", "name": "商品 ETF 代理", "observes": "商品"},
)


def cross_asset_symbols():
    return tuple(item["symbol"] for item in PROXIES)


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _abs_return(series, window):
    if series is None or getattr(series, "empty", True) or len(series) < window + 1:
        return None
    cleaned = pd.to_numeric(series, errors="coerce").where(lambda values: values > 0)
    values = _return(cleaned, window, strict=True)
    if values.empty:
        return None
    return _finite(values.iloc[-1])


def _item(spec, series=None):
    abs5 = _abs_return(series, 5)
    abs20 = _abs_return(series, 20)
    abs60 = _abs_return(series, 60)
    status = "available" if any(value is not None for value in (abs5, abs20, abs60)) else "unavailable"
    return {
        "id": spec["id"],
        "symbol": spec["symbol"],
        "name": spec["name"],
        "observes": spec["observes"],
        "status": status,
        "abs5": abs5,
        "abs20": abs20,
        "abs60": abs60,
    }


def unavailable_cross_asset(*, reason="UNAVAILABLE"):
    items = [_item(spec) for spec in PROXIES]
    return {
        "version": CROSS_ASSET_VERSION,
        "status": "unavailable",
        "reason": reason,
        "note": CROSS_ASSET_NOTE,
        "windows": list(WINDOWS),
        "available_count": 0,
        "total_count": len(PROXIES),
        "items": items,
    }


def build_cross_asset(prices, sessions):
    """Absolute 5/20/60-session returns for the six ETF proxies.

    Gaps are not bridged: a missing or non-positive bar inside a window
    blanks that window, matching production ``abs{k}`` strict returns.
    """
    index = pd.DatetimeIndex(sessions).tz_localize(None).normalize() if sessions is not None else pd.DatetimeIndex([])
    if prices is None or not isinstance(prices, pd.DataFrame) or index.empty:
        return unavailable_cross_asset(reason="NO_PRICES")
    table = prices.copy()
    table.index = pd.to_datetime(table.index)
    if table.index.tz is not None:
        table.index = table.index.tz_localize(None)
    table.index = table.index.normalize()
    table = table.reindex(index)
    items = []
    for spec in PROXIES:
        series = table[spec["symbol"]] if spec["symbol"] in table.columns else None
        items.append(_item(spec, series))
    available = sum(item["status"] == "available" for item in items)
    return {
        "version": CROSS_ASSET_VERSION,
        "status": "available" if available else "unavailable",
        "reason": None if available else "NO_PROXY_RETURNS",
        "note": CROSS_ASSET_NOTE,
        "windows": list(WINDOWS),
        "available_count": available,
        "total_count": len(PROXIES),
        "items": items,
    }
