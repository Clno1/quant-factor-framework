"""Daily momentum-breakout scanner built on published market-data versions."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import math
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from src.data.foundation import DataFoundationError, MarketDataReader
from src.data.universe_ids import US_LIQUID_5M, resolve_market_data_universe
from src.utils.logger import get_logger

log = get_logger(__name__)

@dataclass(frozen=True)
class BreakoutFilters:
    """User-facing thresholds. Percentage values are expressed as 0-100."""

    min_return_20d: float = 20.0
    min_adr_20d: float = 6.0
    min_dollar_volume: float = 10_000_000.0
    min_avg_dollar_volume: float = 10_000_000.0
    min_consolidation_days: int = 9
    max_distance_ma50: float = 35.0
    pivot_proximity: float = 3.0
    max_results: int = 200

    def normalized(self) -> "BreakoutFilters":
        return BreakoutFilters(
            min_return_20d=max(-99.0, float(self.min_return_20d)),
            min_adr_20d=max(0.0, float(self.min_adr_20d)),
            min_dollar_volume=max(0.0, float(self.min_dollar_volume)),
            min_avg_dollar_volume=max(0.0, float(self.min_avg_dollar_volume)),
            min_consolidation_days=max(1, int(self.min_consolidation_days)),
            max_distance_ma50=max(0.0, float(self.max_distance_ma50)),
            pivot_proximity=max(0.0, float(self.pivot_proximity)),
            max_results=min(1000, max(1, int(self.max_results))),
        )


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _normalize_daily(frame: pd.DataFrame) -> pd.DataFrame:
    required = ["open", "high", "low", "close", "volume"]
    if frame is None or frame.empty or any(c not in frame.columns for c in required):
        return pd.DataFrame(columns=required)
    out = frame.copy()
    out.index = pd.to_datetime(out.index, errors="coerce")
    out = out.loc[~out.index.isna()].sort_index()
    # Keep adjusted close from the published version even though the breakout
    # calculation currently consumes only the five base OHLCV columns.
    preserved = [*required, "adj_close"] if "adj_close" in out.columns else required
    for col in preserved:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["high", "low", "close", "volume"])
    out = out[(out["high"] > 0) & (out["low"] > 0) & (out["close"] > 0)]
    return out[preserved]


def load_daily_frames(
    tickers: Iterable[str],
    *,
    data_universe: str = US_LIQUID_5M,
    dataset_version_id: str | None = None,
) -> dict[str, pd.DataFrame]:
    """Load all requested symbols from one immutable published version."""
    normalized = list(
        dict.fromkeys(
            str(ticker).strip().upper()
            for ticker in tickers
            if str(ticker).strip()
        )
    )
    resolved = resolve_market_data_universe(data_universe)
    try:
        bars = MarketDataReader().load_bars(
            resolved,
            tickers=normalized,
            version=dataset_version_id,
        )
    except DataFoundationError as exc:
        log.warning("Published breakout data unavailable for %s: %s", resolved, exc)
        return {}

    frames: dict[str, pd.DataFrame] = {}
    for ticker, group in bars.groupby("ticker", sort=False):
        frame = (
            group.set_index("date")[
                ["open", "high", "low", "close", "adj_close", "volume"]
            ]
            .sort_index()
        )
        normalized_frame = _normalize_daily(frame)
        if not normalized_frame.empty:
            frames[str(ticker).upper()] = normalized_frame
    return frames


def load_daily_frame(
    ticker: str,
    *,
    data_universe: str = US_LIQUID_5M,
    dataset_version_id: str | None = None,
) -> pd.DataFrame:
    ticker = ticker.upper().strip()
    return load_daily_frames(
        [ticker],
        data_universe=data_universe,
        dataset_version_id=dataset_version_id,
    ).get(ticker, pd.DataFrame())


def refresh_daily_frame(
    ticker: str,
    *,
    end: str | pd.Timestamp,
    data_universe: str = US_LIQUID_5M,
    dataset_version_id: str | None = None,
) -> tuple[pd.DataFrame, str]:
    """Return published data and report whether it covers ``end``."""
    ticker = ticker.upper().strip()
    target = pd.Timestamp(end).normalize()
    cached = load_daily_frame(
        ticker,
        data_universe=data_universe,
        dataset_version_id=dataset_version_id,
    )
    if not cached.empty and pd.Timestamp(cached.index.max()).normalize() >= target:
        return cached, "published"
    return cached, "published-stale" if not cached.empty else "unavailable"


def _latest_covered_date(
    frames: Mapping[str, pd.DataFrame],
    requested: str | pd.Timestamp | None,
) -> pd.Timestamp:
    if requested:
        return pd.Timestamp(requested).normalize()
    if not frames:
        raise ValueError("No daily OHLCV cache is available for this universe.")

    coverage: Counter[pd.Timestamp] = Counter()
    for frame in frames.values():
        for date in pd.DatetimeIndex(frame.index[-15:]).normalize().unique():
            coverage[pd.Timestamp(date)] += 1
    threshold = max(1, math.ceil(len(frames) * 0.80))
    well_covered = [date for date, count in coverage.items() if count >= threshold]
    if well_covered:
        return max(well_covered)

    latest_dates = [pd.Timestamp(frame.index.max()).normalize() for frame in frames.values()]
    return Counter(latest_dates).most_common(1)[0][0]


def _runup_pct(frame: pd.DataFrame, lookback: int = 80) -> float:
    history = frame.iloc[-lookback:-1]
    if history.empty:
        return float("nan")
    running_low = history["low"].cummin().replace(0, np.nan)
    runups = history["high"] / running_low - 1.0
    return float(runups.max() * 100.0)


def _true_range(frame: pd.DataFrame) -> pd.Series:
    previous_close = frame["close"].shift(1)
    return pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def evaluate_daily_setup(
    frame: pd.DataFrame,
    *,
    ticker: str,
    filters: BreakoutFilters | None = None,
    asof: str | pd.Timestamp | None = None,
    name: str = "",
    sector: str = "",
) -> dict[str, Any] | None:
    """Compute one symbol's screen fields and interpretable setup checks."""
    filters = (filters or BreakoutFilters()).normalized()
    data = _normalize_daily(frame)
    if asof is not None:
        data = data.loc[data.index <= pd.Timestamp(asof)]
    if len(data) < 65:
        return None

    close = data["close"]
    high = data["high"]
    low = data["low"]
    volume = data["volume"]
    dollar_volume = close * volume

    ma10s = close.rolling(10).mean()
    ma20s = close.rolling(20).mean()
    ma50s = close.rolling(50).mean()
    ma10 = float(ma10s.iloc[-1])
    ma20 = float(ma20s.iloc[-1])
    ma50 = float(ma50s.iloc[-1])
    last_close = float(close.iloc[-1])
    last_high = float(high.iloc[-1])
    last_low = float(low.iloc[-1])

    return_20d = float((last_close / close.iloc[-21] - 1.0) * 100.0)
    adr_daily = (high / low - 1.0) * 100.0
    adr20 = float(adr_daily.iloc[-20:].mean())
    atr20 = float((_true_range(data).iloc[-20:] / close.shift(1).iloc[-20:]).mean() * 100.0)
    latest_dollar_volume = float(dollar_volume.iloc[-1])
    avg_dollar_volume20 = float(dollar_volume.iloc[-20:].mean())

    ma10_slope = float((ma10 / ma10s.iloc[-6] - 1.0) * 100.0)
    ma20_slope = float((ma20 / ma20s.iloc[-6] - 1.0) * 100.0)
    distance_ma10 = float((last_close / ma10 - 1.0) * 100.0)
    distance_ma20 = float((last_close / ma20 - 1.0) * 100.0)
    distance_ma50 = float((last_close / ma50 - 1.0) * 100.0)

    prior_highs = high.iloc[-61:-1]
    peak_date = pd.Timestamp(prior_highs.idxmax())
    peak_position = int(data.index.get_loc(peak_date))
    consolidation_days = len(data) - 1 - peak_position
    prior_peak = float(prior_highs.loc[peak_date])
    prior_move = _runup_pct(data)

    pivot = float(high.iloc[-21:-1].max())
    pivot_distance = float((last_close / pivot - 1.0) * 100.0)
    range3 = float(adr_daily.iloc[-3:].mean())
    tightness = float(range3 / adr20) if adr20 > 0 else float("nan")

    lows5 = low.iloc[-5:].to_numpy(dtype=float)
    higher_low_slope = float(np.polyfit(np.arange(len(lows5)), lows5, 1)[0] / np.mean(lows5) * 100.0)
    recent_volume = float(volume.iloc[-5:-1].mean())
    baseline_volume = float(volume.iloc[-21:-5].mean())
    volume_dryup = recent_volume / baseline_volume if baseline_volume > 0 else float("nan")
    stop_width = float((last_close / last_low - 1.0) * 100.0)

    base_checks = {
        "return_20d": return_20d >= filters.min_return_20d,
        "adr_20d": adr20 >= filters.min_adr_20d,
        "dollar_volume": latest_dollar_volume >= filters.min_dollar_volume,
        "avg_dollar_volume": avg_dollar_volume20 >= filters.min_avg_dollar_volume,
    }
    setup_checks = {
        "prior_move": prior_move >= 30.0,
        "consolidation": consolidation_days >= filters.min_consolidation_days,
        "ma50_distance": -5.0 <= distance_ma50 <= filters.max_distance_ma50,
        "ma_trend": last_close >= ma20 * 0.98 and ma10 >= ma20 * 0.98 and ma20_slope > 0,
        "tight_range": tightness <= 0.55,
        "higher_lows": higher_low_slope > 0,
        "volume_dryup": volume_dryup <= 0.85,
        "near_pivot": pivot_distance >= -filters.pivot_proximity,
        "stop_within_adr": stop_width <= adr20,
    }

    score = 0
    score += 15 if setup_checks["prior_move"] else 0
    score += 10 if setup_checks["consolidation"] else 0
    score += 10 if setup_checks["ma50_distance"] else 0
    score += 15 if setup_checks["ma_trend"] else 0
    score += 15 if setup_checks["tight_range"] else 0
    score += 10 if setup_checks["higher_lows"] else 0
    score += 5 if setup_checks["volume_dryup"] else 0
    score += 10 if setup_checks["near_pivot"] else 0
    score += 5 if setup_checks["stop_within_adr"] else 0
    score += 5 if last_close >= pivot else 0

    core_ready = all(
        setup_checks[key]
        for key in ["prior_move", "consolidation", "ma50_distance", "ma_trend", "tight_range"]
    )
    if core_ready and last_close >= pivot:
        status = "BREAKOUT"
    elif core_ready and setup_checks["near_pivot"]:
        status = "READY"
    elif core_ready:
        status = "SETUP"
    else:
        status = "FORMING"

    return {
        "ticker": ticker.upper(),
        "name": name,
        "sector": sector,
        "data_date": pd.Timestamp(data.index[-1]).strftime("%Y-%m-%d"),
        "close": last_close,
        "return_20d": return_20d,
        "adr_20d": adr20,
        "atr_20d": atr20,
        "dollar_volume": latest_dollar_volume,
        "avg_dollar_volume_20d": avg_dollar_volume20,
        "ma10": ma10,
        "ma20": ma20,
        "ma50": ma50,
        "ma10_slope_5d": ma10_slope,
        "ma20_slope_5d": ma20_slope,
        "distance_ma10": distance_ma10,
        "distance_ma20": distance_ma20,
        "distance_ma50": distance_ma50,
        "prior_move": prior_move,
        "prior_peak": prior_peak,
        "peak_date": peak_date.strftime("%Y-%m-%d"),
        "consolidation_days": consolidation_days,
        "pivot": pivot,
        "pivot_distance": pivot_distance,
        "range3": range3,
        "tightness": tightness,
        "higher_low_slope": higher_low_slope,
        "volume_dryup": volume_dryup,
        "stop_width": stop_width,
        "base_pass": all(base_checks.values()),
        "setup_qualified": core_ready,
        "status": status,
        "score": score,
        "base_checks": base_checks,
        "setup_checks": setup_checks,
    }


def scan_breakouts(
    tickers: Iterable[str],
    *,
    filters: BreakoutFilters | None = None,
    asof: str | pd.Timestamp | None = None,
    names: Mapping[str, str] | None = None,
    sectors: Mapping[str, str] | None = None,
    data_universe: str = US_LIQUID_5M,
    dataset_version_id: str | None = None,
) -> dict[str, Any]:
    filters = (filters or BreakoutFilters()).normalized()
    normalized_tickers = list(dict.fromkeys(str(t).strip().upper() for t in tickers if str(t).strip()))
    frames = load_daily_frames(
        normalized_tickers,
        data_universe=data_universe,
        dataset_version_id=dataset_version_id,
    )
    decision_date = _latest_covered_date(frames, asof)

    rows: list[dict[str, Any]] = []
    stale: list[str] = []
    insufficient: list[str] = []
    for ticker in normalized_tickers:
        frame = frames.get(ticker)
        if frame is None:
            insufficient.append(ticker)
            continue
        row = evaluate_daily_setup(
            frame,
            ticker=ticker,
            filters=filters,
            asof=decision_date,
            name=str((names or {}).get(ticker) or ""),
            sector=str((sectors or {}).get(ticker) or ""),
        )
        if row is None:
            insufficient.append(ticker)
            continue
        lag_days = (decision_date - pd.Timestamp(row["data_date"])).days
        if lag_days > 7:
            stale.append(ticker)
            continue
        if row["base_pass"]:
            rows.append(row)

    if rows:
        ranking = pd.Series({row["ticker"]: row["return_20d"] for row in rows}).rank(pct=True)
        for row in rows:
            row["relative_strength_pct"] = float(ranking.loc[row["ticker"]] * 100.0)
        status_order = {"BREAKOUT": 0, "READY": 1, "SETUP": 2, "FORMING": 3}
        rows.sort(key=lambda r: (status_order.get(r["status"], 9), -r["score"], -r["return_20d"]))
    rows = rows[: filters.max_results]

    return {
        "asof": decision_date.strftime("%Y-%m-%d"),
        "filters": asdict(filters),
        "universe_count": len(normalized_tickers),
        "loaded_count": len(frames),
        "candidate_count": len(rows),
        "breakout_count": sum(row["status"] == "BREAKOUT" for row in rows),
        "ready_count": sum(row["status"] == "READY" for row in rows),
        "setup_count": sum(row["setup_qualified"] for row in rows),
        "stale_tickers": stale,
        "missing_tickers": insufficient,
        "data_universe": resolve_market_data_universe(data_universe),
        "dataset_version_id": dataset_version_id,
        "rows": rows,
    }


def load_market_regime(
    *,
    asof: str | pd.Timestamp,
    symbol: str = "QQQ",
    fetch_missing: bool = True,
    data_universe: str = US_LIQUID_5M,
    dataset_version_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate the Qullamaggie market filter on daily QQQ/IWM data."""
    symbol = symbol.upper().strip()
    target = pd.Timestamp(asof).normalize()
    frame = load_daily_frame(
        symbol,
        data_universe=data_universe,
        dataset_version_id=dataset_version_id,
    )
    covered = not frame.empty and pd.Timestamp(frame.index.max()).normalize() >= target

    # Kept in the public signature because callers decide whether stale data is
    # acceptable. Missing bars are fulfilled only by the centralized writer.
    del fetch_missing, covered

    frame = _normalize_daily(frame)
    frame = frame.loc[frame.index <= target]
    if len(frame) < 25:
        return {
            "symbol": symbol,
            "status": "UNKNOWN",
            "passed": None,
            "asof": target.strftime("%Y-%m-%d"),
            "reason": "缺少足够的指数日线数据",
        }

    close = frame["close"]
    ma10s = close.rolling(10).mean()
    ma20s = close.rolling(20).mean()
    ma10 = float(ma10s.iloc[-1])
    ma20 = float(ma20s.iloc[-1])
    ma10_rising = ma10 > float(ma10s.iloc[-6])
    ma20_rising = ma20 > float(ma20s.iloc[-6])
    passed = ma10 > ma20 and ma10_rising and ma20_rising
    return {
        "symbol": symbol,
        "status": "PASS" if passed else "FAIL",
        "passed": passed,
        "asof": pd.Timestamp(frame.index[-1]).strftime("%Y-%m-%d"),
        "close": float(close.iloc[-1]),
        "ma10": ma10,
        "ma20": ma20,
        "ma10_rising": ma10_rising,
        "ma20_rising": ma20_rising,
        "reason": "MA10 > MA20 且两条均线上升" if passed else "MA10/MA20 未同时满足多头条件",
    }


__all__ = [
    "BreakoutFilters",
    "evaluate_daily_setup",
    "load_daily_frame",
    "load_daily_frames",
    "load_market_regime",
    "refresh_daily_frame",
    "scan_breakouts",
]
