#!/usr/bin/env python3
"""Refresh active US securities and their daily momentum cache."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / "data" / "cache" / "matplotlib"))

from src.breakouts.scanner import load_daily_frame, refresh_daily_frame  # noqa: E402
from src.data.universe import get_universe  # noqa: E402

_NEW_YORK = ZoneInfo("America/New_York")


def _latest_business_day() -> pd.Timestamp:
    now_et = pd.Timestamp.now(tz=_NEW_YORK)
    target = now_et.tz_localize(None).normalize()
    if now_et.hour < 18:
        target -= pd.offsets.BDay(1)
    return pd.offsets.BDay().rollback(target)


def _refresh_one(ticker: str, target: pd.Timestamp) -> tuple[str, str, str | None]:
    cached = load_daily_frame(ticker)
    if not cached.empty and pd.Timestamp(cached.index.max()).normalize() >= target:
        return ticker, "current", str(pd.Timestamp(cached.index.max()).date())
    frame, source = refresh_daily_frame(ticker, end=target)
    latest = str(pd.Timestamp(frame.index.max()).date()) if not frame.empty else None
    return ticker, source, latest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--min-current-dollar-volume-m",
        type=float,
        default=0.0,
        help="Only refresh symbols whose screener dollar volume meets this USD millions floor.",
    )
    parser.add_argument("--force-universe", action="store_true")
    parser.add_argument(
        "--stocks-only",
        action="store_true",
        help="Refresh only rows classified as STOCK; intended for the default alert server.",
    )
    parser.add_argument(
        "--market-symbol",
        action="append",
        default=[],
        help="Also refresh a market-regime benchmark (default: QQQ); repeatable.",
    )
    parser.add_argument("--skip-precompute", action="store_true")
    args = parser.parse_args()

    universe = get_universe("US_ACTIVE", force_refresh=args.force_universe)
    if args.stocks_only:
        if "asset_type" not in universe.columns:
            raise RuntimeError("US_ACTIVE is missing asset_type; refusing an unsafe stocks-only refresh")
        universe = universe[
            universe["asset_type"].fillna("").astype(str).str.upper().eq("STOCK")
        ].copy()
    liquidity_floor = max(0.0, args.min_current_dollar_volume_m) * 1_000_000
    if liquidity_floor > 0 and "current_dollar_volume" in universe.columns:
        universe = universe[
            pd.to_numeric(universe["current_dollar_volume"], errors="coerce") >= liquidity_floor
        ]
    tickers = universe["ticker"].astype(str).str.upper().tolist()
    if args.limit:
        tickers = tickers[: max(1, args.limit)]
    market_symbols = [
        symbol.strip().upper()
        for value in (args.market_symbol or ["QQQ"])
        for symbol in value.split(",")
        if symbol.strip()
    ]
    tickers = list(dict.fromkeys([*tickers, *market_symbols]))
    target = _latest_business_day()
    print(
        f"US_ACTIVE refresh: {len(tickers)} symbols, target={target.date()}, "
        f"workers={args.workers}, assets={'stocks' if args.stocks_only else 'stocks+etfs'}, "
        f"liquidity_floor=${liquidity_floor / 1_000_000:.1f}M, "
        f"market_symbols={','.join(market_symbols)}"
    )

    counts: dict[str, int] = {}
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(_refresh_one, ticker, target): ticker for ticker in tickers}
        for completed, future in enumerate(as_completed(futures), start=1):
            ticker = futures[future]
            try:
                _, source, latest = future.result()
                counts[source] = counts.get(source, 0) + 1
                if latest is None:
                    failures.append(ticker)
            except Exception:  # noqa: BLE001
                failures.append(ticker)
                counts["error"] = counts.get("error", 0) + 1
            if completed % 100 == 0 or completed == len(futures):
                print(f"progress={completed}/{len(futures)} sources={counts} failures={len(failures)}")

    print(f"done sources={counts} failures={len(failures)}")
    if failures:
        print("missing=" + ",".join(failures[:100]))
    if args.limit is None:
        from src.breakouts.scan_cache import clear_scan_cache

        removed = clear_scan_cache()
        print(f"cleared_scan_cache={removed}")
        if not args.skip_precompute:
            from src.webapp.routes_v2 import _get_breakout_scan

            print("precomputing default momentum scan ...")
            scan = _get_breakout_scan(
                universe="US_ACTIVE",
                asof=None,
                min_return_20d=20.0,
                min_adr_20d=6.0,
                min_dollar_volume_m=10.0,
                min_avg_dollar_volume_m=10.0,
                min_consolidation_days=9,
                max_distance_ma50=35.0,
                pivot_proximity=3.0,
                market_symbol="QQQ",
                view="all",
                force=True,
            )
            print(
                "precomputed "
                f"universe={scan['universe_count']} candidates={scan['candidate_count']} asof={scan['asof']}"
            )


if __name__ == "__main__":
    main()
