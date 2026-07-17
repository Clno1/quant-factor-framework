#!/usr/bin/env python3
"""Securely configure the two Discord Incoming Webhooks used before market open."""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.alerts.config import load_local_env  # noqa: E402
from src.alerts.discord import (  # noqa: E402
    DiscordNotifier,
    discord_webhook_identity,
    is_discord_snowflake,
)


def _update_env_file(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    target_keys = set(updates)
    written: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        candidate = stripped[7:].strip() if stripped.startswith("export ") else stripped
        key = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
        if key in target_keys:
            if key not in written:
                output.append(f"{key}={updates[key]}")
                written.add(key)
            continue
        output.append(line)
    if output and output[-1].strip():
        output.append("")
    output.extend(
        f"{key}={value}" for key, value in updates.items() if key not in written
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".premarket-env.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output).rstrip() + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _secret(prompt: str, existing: str) -> str:
    value = getpass.getpass(prompt).strip()
    return value or existing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.local")
    parser.add_argument(
        "--test-send",
        action="store_true",
        help="Send one non-mentioning configuration test to each channel.",
    )
    args = parser.parse_args(argv)
    load_local_env(args.env_file)
    print("Discord 盘前双频道配置（输入不会回显或写入日志）")
    momentum = _secret(
        "#momentum-alerts Webhook URL（留空保留现值）: ",
        os.environ.get("DISCORD_MOMENTUM_WEBHOOK_URL", "").strip()
        or os.environ.get("DISCORD_WEBHOOK_URL", "").strip(),
    )
    rotation = _secret(
        "#sector-rotation Webhook URL（留空保留现值）: ",
        os.environ.get("DISCORD_SECTOR_ROTATION_WEBHOOK_URL", "").strip(),
    )
    if not momentum or not rotation:
        print("两个频道都必须配置独立 Webhook；未写入任何更改。")
        return 1
    if discord_webhook_identity(momentum) == discord_webhook_identity(rotation):
        print("两个频道不能使用同一个 Webhook；未写入任何更改。")
        return 1
    momentum_role = input("动量提醒角色 ID（可留空）: ").strip()
    rotation_role = input("板块日报角色 ID（可留空）: ").strip()
    if any(
        value and not is_discord_snowflake(value)
        for value in (momentum_role, rotation_role)
    ):
        print("角色 ID 必须是纯数字；未写入任何更改。")
        return 1
    try:
        notifiers = {
            "momentum": DiscordNotifier(momentum, max_rate_limit_retries=0),
            "sector": DiscordNotifier(rotation, max_rate_limit_retries=0),
        }
        if args.test_send:
            notifiers["momentum"].send(
                {
                    "username": "Premarket Momentum",
                    "content": "盘前动量日报通道配置成功（测试消息，不触发角色提醒）。",
                    "allowed_mentions": {"parse": []},
                }
            )
            notifiers["sector"].send(
                {
                    "username": "Sector Rotation",
                    "content": "板块/行业强弱日报通道配置成功（测试消息，不触发角色提醒）。",
                    "allowed_mentions": {"parse": []},
                }
            )
    except Exception as exc:  # notifier exceptions are token-safe
        print(f"Webhook 校验或测试失败：{exc}")
        return 1
    _update_env_file(
        args.env_file,
        {
            "PREMARKET_DIGEST_ENABLED": "true",
            "DISCORD_MOMENTUM_WEBHOOK_URL": momentum,
            "DISCORD_SECTOR_ROTATION_WEBHOOK_URL": rotation,
            "DISCORD_MOMENTUM_ROLE_ID": momentum_role,
            "DISCORD_SECTOR_ROTATION_ROLE_ID": rotation_role,
        },
    )
    print(f"配置已写入 {args.env_file}，权限为 600。")
    if not args.test_send:
        print("尚未发送测试消息；需要时重新运行并添加 --test-send。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
