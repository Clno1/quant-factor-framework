#!/usr/bin/env python3
"""Build two XNYS premarket digests and optionally deliver them to Discord."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.premarket_digest import (  # noqa: E402
    DigestChannel,
    PremarketDigestService,
    load_premarket_digest_settings,
)
from src.alerts.config import load_local_env  # noqa: E402


def _channels(value: str) -> list[DigestChannel]:
    if value == "all":
        return list(DigestChannel)
    return [DigestChannel(value)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--send",
        action="store_true",
        help="Send to Discord. Without this flag the command never contacts Discord.",
    )
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Require an XNYS session and the 09:20-09:29 America/New_York window.",
    )
    parser.add_argument("--session", help="Upcoming opening session (YYYY-MM-DD).")
    parser.add_argument(
        "--channel",
        choices=["all", *[channel.value for channel in DigestChannel]],
        default="all",
    )
    parser.add_argument(
        "--retry-unknown",
        action="store_true",
        help="Explicitly resend an uncertain delivery after manually checking the channel.",
    )
    parser.add_argument(
        "--allow-historical-send",
        action="store_true",
        help="Required with --send --session to prevent accidental old-report delivery.",
    )
    parser.add_argument(
        "--allow-outside-window",
        action="store_true",
        help="Explicitly permit a manual --send without the scheduled premarket window.",
    )
    parser.add_argument(
        "--rebuild-failed",
        action="store_true",
        help="Rebuild a frozen FAILED payload; SENT and UNKNOWN rows remain immutable.",
    )
    parser.add_argument(
        "--no-write-preview",
        action="store_true",
        help="Do not persist JSON/Markdown files for a dry-run.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Safely load KEY=VALUE settings without shell-sourcing the file.",
    )
    args = parser.parse_args(argv)
    if args.scheduled and not args.send:
        parser.error("--scheduled requires --send")
    if args.scheduled and args.allow_outside_window:
        parser.error("--scheduled and --allow-outside-window are mutually exclusive")
    if args.scheduled and args.session:
        parser.error("--scheduled derives today's XNYS session and cannot use --session")
    if args.allow_outside_window and not args.send:
        parser.error("--allow-outside-window requires --send")
    if args.send and not args.scheduled and not args.allow_outside_window:
        parser.error("manual --send requires --allow-outside-window")
    if args.send and args.session and not args.allow_historical_send:
        parser.error(
            "--send --session requires both --allow-historical-send and "
            "--allow-outside-window"
        )
    if args.allow_historical_send and (not args.send or not args.session):
        parser.error("--allow-historical-send requires --send --session")
    if args.retry_unknown and not args.send:
        parser.error("--retry-unknown requires --send")
    if args.retry_unknown and args.channel == "all":
        parser.error("--retry-unknown requires an explicit single --channel")
    if args.rebuild_failed and not args.send:
        parser.error("--rebuild-failed requires --send")
    if args.rebuild_failed and args.channel == "all":
        parser.error("--rebuild-failed requires an explicit single --channel")
    if args.rebuild_failed and args.retry_unknown:
        parser.error("--rebuild-failed and --retry-unknown are mutually exclusive")
    if (args.retry_unknown or args.rebuild_failed) and not args.session:
        parser.error(
            "--retry-unknown and --rebuild-failed require an explicit --session"
        )

    try:
        if args.env_file is not None:
            if load_local_env(args.env_file) is None:
                raise FileNotFoundError("the requested environment file does not exist")
        settings = load_premarket_digest_settings(load_env=args.env_file is None)
        service = PremarketDigestService(settings)
        summary = service.run(
            send=args.send,
            scheduled=args.scheduled,
            requested_session=args.session,
            channels=_channels(args.channel),
            retry_unknown=args.retry_unknown,
            rebuild_failed=args.rebuild_failed,
            write_preview=not args.no_write_preview,
        )
    except Exception as exc:  # configuration only; never serialize secret values
        summary = {
            "mode": "send" if args.send else "dry-run",
            "status": "FAILED_CONFIGURATION",
            "error_code": "STARTUP_FAILED",
            "error_type": type(exc).__name__,
            "exit_code": 2,
        }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str))
    return int(summary.get("exit_code", 1))


if __name__ == "__main__":
    raise SystemExit(main())
