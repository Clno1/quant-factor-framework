#!/usr/bin/env python3
"""Reconcile and deliver paper-trading Discord notifications."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.papertrading.notification_state import (  # noqa: E402
    KIND_DAILY_SUMMARY,
    KIND_FILL,
)
from src.papertrading.notifications import (  # noqa: E402
    PaperNotificationService,
    PaperNotificationSettings,
)
from src.utils.env import load_local_env  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--events", action="store_true", help="Stage and deliver new fills.")
    mode.add_argument("--daily", action="store_true", help="Stage and deliver one daily summary.")
    mode.add_argument(
        "--initialize-baseline",
        action="store_true",
        help="Mark every existing fill as historical without sending it.",
    )
    mode.add_argument("--status", action="store_true", help="Print sanitized outbox status.")
    mode.add_argument(
        "--check-config",
        action="store_true",
        help="Validate settings without touching Discord or the outbox.",
    )
    parser.add_argument("--target-session", help="Explicit YYYY-MM-DD for --daily.")
    parser.add_argument("--env-file", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.env_file is not None and load_local_env(args.env_file) is None:
        raise FileNotFoundError("the requested environment file does not exist")
    settings = PaperNotificationSettings.from_env()
    if args.check_config:
        print(json.dumps({
            "delivery_enabled": settings.delivery_enabled,
            "discord_configured": settings.discord_configured,
            "state_path": str(settings.state_path),
            "dashboard_configured": bool(settings.dashboard_base_url),
            "max_attempts": settings.max_attempts,
            "batch_limit": settings.batch_limit,
        }, ensure_ascii=False, indent=2))
        return 0

    service = PaperNotificationService(settings)
    with service.state.run_lock():
        if args.status:
            output = service.state.status()
        elif args.initialize_baseline:
            output = {
                "mode": "initialize_baseline",
                "baselined": service.reconcile_fills(baseline=True),
                "status": service.state.status(include_recent=False),
            }
        elif args.events:
            staged = service.reconcile_fills()
            output = {
                "mode": "events",
                "staged": staged,
                "delivery_enabled": settings.delivery_enabled,
                **service.drain(kinds={KIND_FILL, KIND_DAILY_SUMMARY}),
                "status": service.state.status(include_recent=False),
            }
        else:
            staged = service.stage_daily_summary(
                target_session=args.target_session,
            )
            output = {
                "mode": "daily",
                "staged": int(staged),
                "delivery_enabled": settings.delivery_enabled,
                **service.drain(kinds={KIND_DAILY_SUMMARY}),
                "status": service.state.status(include_recent=False),
            }
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
