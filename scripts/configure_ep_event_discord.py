#!/usr/bin/env python3
"""Configure only the EP AI commentary route; never sends a message or starts a timer."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.alerts.ep_event import webhook_channel
from src.breakouts.ep.event_worker import WorkerConfig
from src.utils.io import atomic_save_json
from src.utils.file_lock import file_lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--reuse-momentum", action="store_true", help="Read the existing canonical momentum webhook, never sector rotation")
    parser.add_argument('--replace', action='store_true', help='Switch only EP to a new, explicitly confirmed channel')
    parser.add_argument('--channel-id', help='Expected new channel ID; prompted when replacing if omitted')
    args = parser.parse_args()
    config = WorkerConfig.model_validate_json(args.config.read_text())
    original = args.config.read_bytes()
    target = Path(config.webhook_file)
    if args.replace and args.reuse_momentum:
        raise ValueError('REPLACEMENT_MUST_USE_INDEPENDENT_CHANNEL')
    if args.replace:
        if not config.webhook_file:
            raise ValueError('EXISTING_EP_WEBHOOK_PATH_REQUIRED')
        target = target.with_name('ep-event-discord-' + uuid4().hex + '.key')
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
    if args.replace:
        import re
        expected = args.channel_id or input('New Discord channel ID (not webhook ID): ').strip()
        if not re.fullmatch(r'[0-9]{1,30}', expected) or channel != expected:
            raise ValueError('NEW_CHANNEL_ID_MISMATCH')
        if channel == config.expected_channel_id:
            raise ValueError('NEW_CHANNEL_MUST_DIFFER_FROM_OLD_CHANNEL')
    if sector_url and webhook_channel(sector_url) == channel:
        raise ValueError("MOMENTUM_AND_SECTOR_CHANNELS_MUST_DIFFER")
    updated = config.model_dump()
    updated.update(delivery_enabled=True, allow_unreviewed_ai=True, expected_channel_id=channel,
                   webhook_file=str(target))
    WorkerConfig.model_validate(updated)
    with file_lock(Path(config.database).with_suffix('.event-worker.lock')):
        if args.config.read_bytes() != original:
            raise ValueError('CONFIG_CHANGED_RETRY_CONFIGURATION')
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'w') as output:
                output.write(webhook + '\n')
                output.flush()
                os.fsync(output.fileno())
            atomic_save_json(updated, args.config)
            args.config.chmod(0o600)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    print(json.dumps({"configured": True, "channel_id": channel, "messages_sent": 0, "timer_started": False,
                      'replaced': args.replace, 'old_channel_history_preserved': True,
                      "mode": "UNVERIFIED_AI_COMMENTARY"}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"configured": False, "reason": "CHECK_CONFIG_NEW_PRIVATE_FILE_AND_DISCORD_CHANNEL"}), file=sys.stderr)
        sys.exit(1)
