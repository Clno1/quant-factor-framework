"""FMP one-minute cache, opening-range levels and intraday bar aggregation."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import PROJECT_ROOT
from src.utils.io import is_cache_fresh, read_parquet, write_parquet
from src.utils.logger import get_logger

log = get_logger(__name__)

INTRADAY_INTERVALS = (1, 5, 15, 30, 60)
_CACHE_DIR = PROJECT_ROOT / "data" / "raw" / "intraday" / "1min"


def _cache_path(ticker: str) -> Path:
    return _CACHE_DIR / f"{ticker.upper().strip()}.parquet"


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    required = ["open", "high", "low", "close", "volume"]
    if frame is None or frame.empty or any(c not in frame.columns for c in required):
        return pd.DataFrame(columns=required)
    out = frame.copy()
    out.index = pd.to_datetime(out.index, errors="coerce")
    out = out.loc[~out.index.isna()].sort_index()
    for col in required:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close"])
    return out[required].loc[lambda x: ~x.index.duplicated(keep="last")]


def load_intraday_1min(
    ticker: str,
    *,
    refresh: bool = True,
    end: str | date | None = None,
    lookback_days: int = 14,
) -> tuple[pd.DataFrame, str]:
    """Return one-minute bars and a source label (live-cache/cache)."""
    ticker = ticker.upper().strip()
    path = _cache_path(ticker)
    cached = _normalize(read_parquet(path)) if path.exists() else pd.DataFrame()
    if not refresh or (path.exists() and is_cache_fresh(path, 2.0 / 1440.0)):
        return cached, "cache"

    end_date = pd.Timestamp(end or date.today()).date()
    start_date = end_date - timedelta(days=max(2, int(lookback_days)))
    try:
        from src.data.fmp import get_intraday_ohlcv

        incoming = get_intraday_ohlcv(
            ticker,
            interval="1min",
            start=start_date.isoformat(),
            end=end_date.isoformat(),
        )
        incoming = _normalize(incoming if incoming is not None else pd.DataFrame())
        if not incoming.empty:
            combined = _normalize(pd.concat([cached, incoming]))
            write_parquet(combined, path)
            return combined, "live-cache"
    except Exception as exc:  # noqa: BLE001
        log.warning("Intraday refresh failed for %s: %s", ticker, exc)

    if not cached.empty:
        return cached, "cache-fallback"
    return pd.DataFrame(), "unavailable"


def _regular_sessions(frame: pd.DataFrame) -> pd.DataFrame:
    data = _normalize(frame)
    if data.empty:
        return data
    times = data.index.time
    return data[(times >= pd.Timestamp("09:30").time()) & (times < pd.Timestamp("16:00").time())]


def _select_session(sessions: pd.DataFrame, session_date: str | date | None) -> pd.DataFrame:
    if sessions.empty:
        return sessions
    if session_date is None:
        target = max(sessions.index.date)
    else:
        target = pd.Timestamp(session_date).date()
    return sessions[sessions.index.date == target]


def _aggregate_session(session: pd.DataFrame, interval: int) -> pd.DataFrame:
    if interval not in INTRADAY_INTERVALS:
        raise ValueError(f"Unsupported interval {interval}. Choose from {INTRADAY_INTERVALS}.")
    if session.empty or interval == 1:
        out = session.copy()
    else:
        out = session.resample(
            f"{interval}min",
            origin=session.index[0],
            label="left",
            closed="left",
        ).agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna(subset=["open", "high", "low", "close"])
    return out


def _aggregate_sessions(sessions: pd.DataFrame, interval: int) -> pd.DataFrame:
    """Aggregate each regular session separately, then roll MAs across sessions."""
    if sessions.empty:
        return sessions
    pieces = [
        _aggregate_session(group, interval)
        for _, group in sessions.groupby(sessions.index.date, sort=True)
    ]
    out = pd.concat(pieces).sort_index()
    for window in (10, 20, 50):
        out[f"ma{window}"] = out["close"].rolling(window).mean()
    return out


def build_intraday_snapshot(
    frame: pd.DataFrame,
    *,
    interval: int = 5,
    session_date: str | date | None = None,
) -> dict[str, Any]:
    sessions = _regular_sessions(frame)
    session = _select_session(sessions, session_date)
    if session.empty:
        return {"session_date": None, "bars": [], "opening_ranges": {}, "error": "没有常规交易时段分钟数据"}

    start = pd.Timestamp(session.index[0]).normalize() + pd.Timedelta(hours=9, minutes=30)
    opening_ranges: dict[str, dict[str, Any]] = {}
    last_price = float(session["close"].iloc[-1])
    for minutes in (1, 5, 30, 60):
        range_end = start + pd.Timedelta(minutes=minutes)
        formed_at = range_end - pd.Timedelta(minutes=1)
        if session.index[-1] < formed_at:
            continue
        segment = session[(session.index >= start) & (session.index < range_end)]
        if segment.empty:
            continue
        high = float(segment["high"].max())
        low = float(segment["low"].min())
        post_range = session[session.index >= range_end]
        triggered = not post_range.empty and float(post_range["high"].max()) >= high
        opening_ranges[str(minutes)] = {
            "high": high,
            "low": low,
            "triggered": triggered,
            "current_above": last_price >= high,
        }

    all_bars = _aggregate_sessions(sessions, int(interval))
    target_date = pd.Timestamp(session.index[0]).date()
    bars = all_bars[all_bars.index.date == target_date]
    records: list[dict[str, Any]] = []
    for timestamp, row in bars.iterrows():
        records.append({
            "date": pd.Timestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S"),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
            "ma10": None if pd.isna(row["ma10"]) else float(row["ma10"]),
            "ma20": None if pd.isna(row["ma20"]) else float(row["ma20"]),
            "ma50": None if pd.isna(row["ma50"]) else float(row["ma50"]),
        })

    day_low = float(session["low"].min())
    stop_width = (last_price / day_low - 1.0) * 100.0 if day_low > 0 else None
    return {
        "session_date": pd.Timestamp(session.index[0]).strftime("%Y-%m-%d"),
        "interval": int(interval),
        "last_timestamp": pd.Timestamp(session.index[-1]).strftime("%Y-%m-%d %H:%M:%S"),
        "last_price": last_price,
        "day_low": day_low,
        "day_high": float(session["high"].max()),
        "stop_width": stop_width,
        "opening_ranges": opening_ranges,
        "bars": records,
        "error": None,
    }


__all__ = [
    "INTRADAY_INTERVALS",
    "build_intraday_snapshot",
    "load_intraday_1min",
]
