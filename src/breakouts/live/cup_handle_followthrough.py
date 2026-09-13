"""Conservative posthoc evidence for saved signals, never live-day acceptance."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def assess_followthrough(signal, rows, *, horizon_bars=6, target_return_pct=2.0):
    result = {"status": "UNRESOLVED", "false_positive_proxy": None,
              "horizon_bars": horizon_bars, "target_return_pct": target_return_pct}

    def unresolved(reason):
        return {**result, "reason": reason}

    try:
        entry = float(signal["price"])
        stop = float(signal["pattern"]["handle_low"])
        target = entry * (1 + float(target_return_pct) / 100)
        bar = pd.Timestamp(signal["bar_timestamp"])
        trigger = pd.Timestamp(signal["triggered_at"])
        if bar.tz is None or trigger.tz is None:
            return unresolved("NAIVE_SIGNAL_TIMESTAMP")
        bar = bar.tz_convert("America/New_York").tz_localize(None)
        trigger = trigger.tz_convert("America/New_York").tz_localize(None)
        start = bar + pd.Timedelta(minutes=5)
        if (horizon_bars < 1 or horizon_bars > 96 or not isinstance(horizon_bars, int)
                or not all(math.isfinite(x) for x in (entry, stop, target))
                or not 0 < stop < entry < target or bar != bar.floor("5min")
                or not start <= trigger < start + pd.Timedelta(minutes=5)):
            return unresolved("INVALID_SIGNAL_CONTRACT")
        expected = pd.date_range(start, periods=5 * horizon_bars, freq="min")
        if (start.date().isoformat() != signal["session_date"]
                or start.time() < pd.Timestamp("09:30").time()
                or expected[-1].time() >= pd.Timestamp("16:00").time()
                or expected[-1].date() != start.date()):
            return unresolved("HORIZON_OUTSIDE_REGULAR_SESSION")
        data = pd.DataFrame(rows)
        columns = ["open", "high", "low", "close", "volume"]
        if data.empty or any(c not in data for c in ["date", *columns]):
            return unresolved("MISSING_RESPONSE_FIELDS")
        index = pd.DatetimeIndex(pd.to_datetime(data["date"], errors="coerce"))
        if index.isna().any():
            return unresolved("INVALID_RESPONSE_TIMESTAMP")
        if index.tz is not None:
            index = index.tz_convert("America/New_York").tz_localize(None)
        data.index = index
        window = data.loc[(index >= start) & (index < expected[-1] + pd.Timedelta(minutes=1)), columns]
        if window.index.duplicated().any():
            return unresolved("DUPLICATE_MINUTE")
        result["missing_minutes"] = [str(x) for x in expected.difference(window.index)]
        if result["missing_minutes"] or not window.index.difference(expected).empty:
            return unresolved("INCOMPLETE_CONTIGUOUS_HORIZON")
        window = window.reindex(expected).apply(pd.to_numeric, errors="coerce")
        if not np.isfinite(window.to_numpy()).all():
            return unresolved("NONFINITE_OHLCV")
        if (window <= 0).any(axis=None):
            return unresolved("NONPOSITIVE_OHLCV")
        if ((window.high < window[["open", "low", "close"]].max(axis=1))
                | (window.low > window[["open", "high", "close"]].min(axis=1))).any():
            return unresolved("INVALID_OHLC_ORDER")
        bars = window.resample("5min").agg({"open": "first", "high": "max", "low": "min",
                                           "close": "last", "volume": "sum"})
        result.update(entry=entry, stop=stop, target=target, complete_minutes=len(window),
                      bars=[{"date": str(t), **r.to_dict()} for t, r in bars.iterrows()],
                      window_start=str(start), window_end=str(expected[-1] + pd.Timedelta(minutes=1)))
        # A minute containing the actual trigger cannot establish post-entry order.
        for timestamp, row in window.iterrows():
            hit_stop, hit_target = row.low <= stop, row.high >= target
            if timestamp < trigger and (hit_stop or hit_target):
                return unresolved("TRIGGER_MINUTE_ORDER_UNKNOWN")
            if hit_stop and hit_target:
                return unresolved("SAME_MINUTE_BARRIER_ORDER_UNKNOWN")
            if hit_stop or hit_target:
                return {**result, "status": "STOP_FIRST" if hit_stop else "TARGET_REACHED",
                        "false_positive_proxy": bool(hit_stop), "first_event_minute": str(timestamp)}
        return {**result, "status": "TARGET_NOT_REACHED", "false_positive_proxy": True}
    except (KeyError, TypeError, ValueError, OverflowError):
        return unresolved("INVALID_INPUT_CONTRACT")


def summarize_followthrough(results):
    resolved = [r for r in results if r.get("false_positive_proxy") is not None]
    false_count = sum(bool(r["false_positive_proxy"]) for r in resolved)
    return {"signal_count": len(results), "resolved_count": len(resolved),
            "unresolved_count": len(results) - len(resolved), "false_positive_proxy_count": false_count,
            "false_positive_proxy_pct_resolved": 100 * false_count / len(resolved) if resolved else None,
            "false_positive_proxy_pct_all": 100 * false_count / len(results)
            if results and len(resolved) == len(results) else None}
