#!/usr/bin/env python3
"""Replay synthetic intraday load without network access or Discord delivery."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import resource
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.breakouts.live.detector import BreakoutDetector  # noqa: E402
from src.breakouts.live.models import (  # noqa: E402
    DailyCandidate,
    MonitorSymbolState,
    QuoteSnapshot,
)
from src.breakouts.live.rolling import RollingIntradayBars  # noqa: E402
from src.breakouts.live.selector import select_active_pool  # noqa: E402
from src.breakouts.live.settings import IntradayMonitorSettings  # noqa: E402
from src.breakouts.live.state import IntradayMonitorState  # noqa: E402


NEW_YORK = ZoneInfo("America/New_York")


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _rss_mb() -> float:
    raw = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return raw / (1024.0 * 1024.0) if sys.platform == "darwin" else raw / 1024.0


def _candidate(index: int) -> DailyCandidate:
    price = 10.0 + index * 0.01
    return DailyCandidate(
        ticker=f"S{index:04d}",
        name=f"Synthetic {index}",
        sector="Synthetic",
        setup_score=50 + index % 50,
        daily_pivot=price * 0.995,
        previous_high=price * 0.99,
        adr20=7.0,
        avg_dollar_volume20=20_000_000.0,
        source_data_date="2026-07-17",
        setup_qualified=index % 3 != 0,
        daily_status="READY" if index % 3 else "FORMING",
        return_reference_close=price / 1.25,
        adr_sum_19=120.0,
        forced_watch=index < 5,
    )


def _quote(candidate: DailyCandidate, now: datetime, minute: int) -> QuoteSnapshot:
    price = max(candidate.breakout_level, 1.0) * (1.0 + minute / 100_000.0)
    return QuoteSnapshot(
        ticker=candidate.ticker,
        timestamp=now - timedelta(seconds=3),
        price=price,
        cumulative_volume=2_000_000.0 + minute * 10_000.0,
        day_high=price * 1.001,
        day_low=price / 1.08,
        open=price / 1.02,
        previous_close=price / 1.03,
        change_percentage=3.0 + minute / 100.0,
    )


def _bar_chunk(
    candidate: DailyCandidate,
    session_date: str,
    start_minute: int,
    end_minute: int,
) -> pd.DataFrame:
    indices = [
        pd.Timestamp(f"{session_date} 09:30") + pd.Timedelta(minutes=minute)
        for minute in range(start_minute, end_minute + 1)
    ]
    base = max(candidate.breakout_level, 1.0)
    closes = [
        base * (0.985 + minute * 0.00008)
        for minute in range(start_minute, end_minute + 1)
    ]
    return pd.DataFrame({
        "open": [close * 0.999 for close in closes],
        "high": [close * 1.001 for close in closes],
        "low": [close * 0.998 for close in closes],
        "close": closes,
        "volume": [100_000.0] * len(closes),
    }, index=pd.DatetimeIndex(indices))


def run_benchmark(
    *,
    days: int,
    candidate_count: int,
    active_count: int,
) -> dict:
    settings = IntradayMonitorSettings(
        active_max_symbols=active_count,
        active_hard_limit=max(active_count, 60),
    ).validate()
    detector = BreakoutDetector(settings)
    candidates = [_candidate(index) for index in range(candidate_count)]
    candidate_map = {candidate.ticker: candidate for candidate in candidates}
    rolling = {
        candidate.ticker: RollingIntradayBars(candidate.ticker)
        for candidate in candidates[:active_count]
    }
    active_tickers = list(rolling)
    timings: list[float] = []
    detector_checks = 0
    state_rows = 0
    broad_cycles = 0
    sessions = pd.bdate_range("2026-07-20", periods=days)
    total_started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temporary:
        state = IntradayMonitorState(Path(temporary) / "state.sqlite3")
        for session in sessions:
            session_date = session.strftime("%Y-%m-%d")
            last_sync_minute = -1
            for minute in range(390):
                cycle_started = time.perf_counter()
                now = (
                    datetime.combine(
                        session.date(),
                        datetime.min.time(),
                        tzinfo=NEW_YORK,
                    )
                    + timedelta(hours=9, minutes=31 + minute, seconds=8)
                )
                if minute % settings.broad_refresh_minutes == 0:
                    broad_quotes = {
                        candidate.ticker: _quote(candidate, now, minute)
                        for candidate in candidates
                    }
                    selected = select_active_pool(
                        candidates,
                        broad_quotes,
                        max_symbols=active_count,
                        previous_tickers=active_tickers,
                    )
                    active_tickers = [
                        item.candidate.ticker for item in selected
                    ]
                    for ticker in active_tickers:
                        rolling.setdefault(ticker, RollingIntradayBars(ticker))
                        rolling[ticker].merge(_bar_chunk(
                            candidate_map[ticker],
                            session_date,
                            last_sync_minute + 1,
                            minute,
                        ))
                    last_sync_minute = minute
                    broad_cycles += 1

                active_quotes = {
                    ticker: _quote(candidate_map[ticker], now, minute)
                    for ticker in active_tickers
                }
                state_updates = []
                for ticker in active_tickers:
                    metrics = rolling[ticker].metrics(
                        now=now,
                        session_date=session_date,
                        interval=settings.detector_interval_minutes,
                    )
                    armed = detector.should_confirm(
                        candidate_map[ticker],
                        active_quotes[ticker],
                        metrics,
                        now=now,
                        session_date=session_date,
                    )
                    state_updates.append((
                        ticker,
                        (
                            MonitorSymbolState.ARMED
                            if armed
                            else MonitorSymbolState.WATCHING
                        ),
                        {"last_bar": metrics.get("last_timestamp")},
                    ))
                    detector_checks += 1
                state.set_symbol_states(
                    session_date=session_date,
                    algorithm_version="benchmark",
                    rows=state_updates,
                )
                state_rows += len(state_updates)
                timings.append(time.perf_counter() - cycle_started)

    total_seconds = time.perf_counter() - total_started
    result = {
        "days": days,
        "candidate_count": candidate_count,
        "active_count": active_count,
        "minute_cycles": len(timings),
        "broad_cycles": broad_cycles,
        "detector_checks": detector_checks,
        "sqlite_state_rows": state_rows,
        "total_seconds": round(total_seconds, 3),
        "cycle_ms_p50": round(_percentile(timings, 0.50) * 1000.0, 3),
        "cycle_ms_p95": round(_percentile(timings, 0.95) * 1000.0, 3),
        "cycle_ms_max": round(max(timings, default=0.0) * 1000.0, 3),
        "peak_rss_mb": round(_rss_mb(), 1),
        "stored_bars": sum(item.stored_bars for item in rolling.values()),
    }
    result["passes"] = {
        "cycle_p95_under_1s": result["cycle_ms_p95"] < 1000.0,
        "rss_under_600mb": result["peak_rss_mb"] < 600.0,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--candidates", type=int, default=600)
    parser.add_argument("--active", type=int, default=60)
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args()
    result = run_benchmark(
        days=max(1, args.days),
        candidate_count=max(1, args.candidates),
        active_count=max(1, min(60, args.active)),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.enforce and not all(result["passes"].values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
