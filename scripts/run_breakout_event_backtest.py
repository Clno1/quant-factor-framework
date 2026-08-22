#!/usr/bin/env python
"""Run the version-bound event-level breakout / cup-handle backtest."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.breakouts.historical_backtest import (  # noqa: E402
    BreakoutBacktestConfig,
    backtest_breakouts,
)


def _csv_values(raw: str) -> list[str]:
    return [value.strip().upper() for value in str(raw).split(",") if value.strip()]


def _int_values(raw: str) -> tuple[int, ...]:
    return tuple(int(value.strip()) for value in str(raw).split(",") if value.strip())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Historical event-level momentum-breakout / cup-handle study"
    )
    parser.add_argument("--tickers", required=True, help="Comma-separated tickers")
    parser.add_argument("--start", required=True, help="First signal date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="Last signal date YYYY-MM-DD")
    parser.add_argument("--data-universe", default="US_LIQUID_5M")
    parser.add_argument("--dataset-version-id", default=None)
    parser.add_argument("--trigger-statuses", default="BREAKOUT")
    parser.add_argument("--horizons", default="1,5,20")
    parser.add_argument("--cooldown-sessions", type=int, default=20)
    parser.add_argument("--warmup-sessions", type=int, default=80)
    parser.add_argument("--round-trip-cost-bps", type=float, default=20.0)
    parser.add_argument(
        "--output-dir",
        default="outputs/breakouts/historical_event_backtest",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    tickers = _csv_values(args.tickers)
    config = BreakoutBacktestConfig(
        trigger_statuses=tuple(_csv_values(args.trigger_statuses)),
        horizons=_int_values(args.horizons),
        cooldown_sessions=args.cooldown_sessions,
        warmup_sessions=args.warmup_sessions,
        round_trip_cost_bps=args.round_trip_cost_bps,
    )
    result = backtest_breakouts(
        tickers,
        start=args.start,
        end=args.end,
        data_universe=args.data_universe,
        dataset_version_id=args.dataset_version_id,
        config=config,
    )

    root = Path(args.output_dir)
    root.mkdir(parents=True, exist_ok=True)
    result.events.to_csv(root / "events.csv", index=False)
    if not result.by_year.empty:
        result.by_year.to_csv(root / "by_year.csv")
    if not result.by_regime.empty:
        result.by_regime.to_csv(root / "by_regime.csv")
    payload = {
        "summary": result.summary,
        "config": result.config,
        "data_contract": result.data_contract,
    }
    (root / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    print(f"events={len(result.events)} output={root.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
