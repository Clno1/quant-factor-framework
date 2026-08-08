#!/usr/bin/env python3
"""Run or inspect the isolated intraday momentum monitor in shadow mode."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.breakouts.live.service import IntradayMomentumMonitor  # noqa: E402
from src.breakouts.live.settings import IntradayMonitorSettings  # noqa: E402
from src.breakouts.live.state import IntradayMonitorState  # noqa: E402
from src.utils.env import load_local_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shadow", action="store_true", help="Run without Discord delivery.")
    parser.add_argument("--once", action="store_true", help="Execute one monitor cycle.")
    parser.add_argument("--status", action="store_true", help="Print persisted monitor status.")
    parser.add_argument(
        "--allow-closed",
        action="store_true",
        help="Allow a read-only smoke cycle while the market is closed.",
    )
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()

    if args.env_file is not None:
        if load_local_env(args.env_file) is None:
            raise FileNotFoundError("the requested environment file does not exist")
    else:
        load_local_env()
    settings = IntradayMonitorSettings.load()
    state = IntradayMonitorState(settings.state_path)
    if args.status:
        print(json.dumps(state.status(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if not args.shadow:
        parser.error("--shadow is required; Discord delivery is intentionally gated")

    monitor = IntradayMomentumMonitor(settings, state=state)
    if args.once:
        result = asyncio.run(monitor.cycle(
            allow_closed=args.allow_closed,
            force_broad=True,
        ))
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    asyncio.run(monitor.run_forever())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
