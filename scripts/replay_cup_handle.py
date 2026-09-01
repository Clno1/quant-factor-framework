#!/usr/bin/env python3
"""Replay cup-handle signals from canonical daily data and local minute Parquet."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.breakouts.daily_data import load_breakout_daily_dataset  # noqa: E402
from src.breakouts.live.cup_handle_replay import replay_cup_handle  # noqa: E402
from src.breakouts.live.settings import IntradayMonitorSettings  # noqa: E402
from src.utils.io import atomic_save_json, read_parquet  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", action="append", required=True)
    parser.add_argument(
        "--minute-dir",
        type=Path,
        default=ROOT / "data" / "raw" / "intraday" / "1min",
        help="Read-only directory containing TICKER.parquet one-minute files.",
    )
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    tickers = list(dict.fromkeys(
        str(value).strip().upper() for value in args.ticker if str(value).strip()
    ))
    minute_frames = {
        ticker: read_parquet(args.minute_dir / f"{ticker}.parquet")
        for ticker in tickers
        if (args.minute_dir / f"{ticker}.parquet").exists()
    }
    missing = sorted(set(tickers) - set(minute_frames))
    if missing:
        parser.error(
            "historical minute Parquet is required; provider fallback is forbidden: "
            + ", ".join(missing)
        )
    dataset = load_breakout_daily_dataset(
        requested_universe="US_ACTIVE",
        ticker_selector=lambda _source: tickers,
        end=args.end,
    )
    daily_frames = {ticker: dataset.frame(ticker) for ticker in tickers}
    result = replay_cup_handle(
        daily_frames,
        minute_frames,
        settings=IntradayMonitorSettings.load(),
        start=args.start,
        end=args.end,
    )
    output = args.output or (
        ROOT
        / "outputs"
        / "intraday_momentum_monitor"
        / "cup_handle"
        / "replays"
        / f"replay_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    atomic_save_json(result, output)
    print(json.dumps({
        "output": str(output),
        "signal_count": result["signal_count"],
        "false_positive_rate_proxy": result["false_positive_rate_proxy"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
