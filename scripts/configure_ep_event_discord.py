#!/usr/bin/env python3
"""Configure only the EP AI commentary route; never sends a message or starts a timer."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.alerts.ep_event import webhook_channel
from src.breakouts.ep.event_worker import WorkerConfig
from src.utils.io import atomic_save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--reuse-momentum", action="store_true", help="Read the existing canonical momentum webhook, never sector rotation")
    args = parser.parse_args()
    config = WorkerConfig.model_validate_json(args.config.read_text())
    target = Path(config.webhook_file)
    if not config.webhook_file or target.exists() or target.is_symlink():
        raise ValueError("NEW_PRIVATE_EP_WEBHOOK_FILE_REQUIRED")
    sector_url = None
    if args.reuse_momentum:
        from dotenv import dotenv_values
        values = dotenv_values("/etc/quant/premarket-digest.env")
        webhook = values.get("DISCORD_MOMENTUM_WEBHOOK_URL") or ""
        sector_url = values.get("DISCORD_SECTOR_ROTATION_WEBHOOK_URL")
    else:
        webhook = getpass.getpass("EP Discord webhook (hidden): ").strip()
    channel = webhook_channel(webhook)
    if sector_url and webhook_channel(sector_url) == channel:
        raise ValueError("MOMENTUM_AND_SECTOR_CHANNELS_MUST_DIFFER")
    updated = config.model_dump()
    updated.update(delivery_enabled=True, allow_unreviewed_ai=True, expected_channel_id=channel)
    WorkerConfig.model_validate(updated)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(webhook + "\n")
        output.flush()
        os.fsync(output.fileno())
    atomic_save_json(updated, args.config)
    print(json.dumps({"configured": True, "channel_id": channel, "messages_sent": 0, "timer_started": False,
                      "mode": "UNVERIFIED_AI_COMMENTARY"}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"configured": False, "reason": "CHECK_CONFIG_NEW_PRIVATE_FILE_AND_DISCORD_CHANNEL"}), file=sys.stderr)
        sys.exit(1)
