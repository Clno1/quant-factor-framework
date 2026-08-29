#!/usr/bin/env python3
"""Run or inspect the isolated, promotion-gated intraday momentum monitor."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from datetime import datetime
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.alerts.discord import DiscordNotifier  # noqa: E402
from src.breakouts.live.service import IntradayMomentumMonitor  # noqa: E402
from src.breakouts.live.cup_handle import CUP_HANDLE_ALGORITHM_VERSION  # noqa: E402
from src.breakouts.live.session import previous_xnys_sessions  # noqa: E402
from src.breakouts.live.settings import IntradayMonitorSettings  # noqa: E402
from src.breakouts.live.state import IntradayMonitorState  # noqa: E402
from src.utils.env import load_local_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--shadow", action="store_true", help="Run without Discord delivery.")
    mode.add_argument(
        "--live",
        action="store_true",
        help="Deliver minute signals after the five-session promotion gate passes.",
    )
    mode.add_argument(
        "--auto",
        action="store_true",
        help="Run shadow until the promotion gate passes, then start live delivery.",
    )
    mode.add_argument(
        "--prepare-candidates",
        action="store_true",
        help="Build the daily candidate snapshot without quotes or delivery.",
    )
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
    reference_date = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")
    expected_sessions = previous_xnys_sessions(
        reference_date,
        settings.required_shadow_sessions,
    )
    promotion = state.promotion_status(expected_sessions)
    cup_expected_sessions = previous_xnys_sessions(
        reference_date,
        settings.cup_required_shadow_sessions,
    )
    cup_promotion = state.cup_handle_promotion_status(
        cup_expected_sessions,
        algorithm_version=CUP_HANDLE_ALGORITHM_VERSION,
    )
    if args.status:
        status = state.status()
        status["promotion"] = promotion
        status["cup_handle_promotion"] = cup_promotion
        status["configured_mode"] = "auto"
        status["delivery_armed"] = settings.delivery_enabled
        status["effective_auto_mode"] = (
            "live"
            if settings.delivery_enabled and promotion["eligible"]
            else "shadow"
        )
        status["effective_cup_handle_mode"] = (
            "live"
            if (
                settings.cup_handle_delivery_enabled
                and settings.delivery_enabled
                and cup_promotion["eligible"]
            )
            else "shadow"
        )
        print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if not settings.enabled:
        parser.error("intraday_momentum_monitor.enabled must be true")
    if not args.shadow and not args.live and not args.auto and not args.prepare_candidates:
        parser.error(
            "one of --shadow, --live, --auto or --prepare-candidates is required"
        )
    notifier = None
    delivery_mode = "shadow"
    wants_live = args.live or (
        args.auto and settings.delivery_enabled and promotion["eligible"]
    )
    if args.auto and settings.delivery_enabled and not settings.discord_webhook_url:
        parser.error(
            "INTRADAY_MOMENTUM_DISCORD_WEBHOOK_URL is required for auto promotion"
        )
    if wants_live:
        if not settings.delivery_enabled:
            parser.error("INTRADAY_MOMENTUM_DISCORD_ENABLED must be true for --live")
        if not settings.discord_webhook_url:
            parser.error(
                "INTRADAY_MOMENTUM_DISCORD_WEBHOOK_URL is required for --live"
            )
        if not promotion["eligible"]:
            parser.error(
                "five-session shadow promotion gate has not passed: "
                + json.dumps(promotion, ensure_ascii=False, sort_keys=True)
            )
        notifier = DiscordNotifier(settings.discord_webhook_url)
        delivery_mode = "live"

    monitor = IntradayMomentumMonitor(
        settings,
        state=state,
        delivery_mode=delivery_mode,
        cup_delivery_mode=(
            "live"
            if (
                wants_live
                and settings.cup_handle_delivery_enabled
                and cup_promotion["eligible"]
            )
            else "shadow"
        ),
        notifier=notifier,
    )
    if args.prepare_candidates:
        result = asyncio.run(monitor.prepare_candidates())
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
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
