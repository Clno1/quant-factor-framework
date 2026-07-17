#!/usr/bin/env python3
"""Securely validate and store the Discord momentum-alert webhook locally."""
from __future__ import annotations

import getpass
import os
from pathlib import Path
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.alerts.discord import DiscordNotifier, is_discord_snowflake  # noqa: E402


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
    fd, temporary = tempfile.mkstemp(prefix=".env.local.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
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


def main() -> int:
    print("Discord Momentum Alerts 配置")
    print("Webhook 输入不会显示，也不会写入日志。")
    webhook = getpass.getpass("Discord Webhook URL: ").strip()
    if not webhook:
        print("未输入 Webhook，配置已取消。")
        return 1
    role_id = input("提醒角色 ID（可留空，之后再配）: ").strip()
    if role_id and not is_discord_snowflake(role_id):
        print("角色 ID 必须是纯数字。")
        return 1
    try:
        notifier = DiscordNotifier(webhook, max_rate_limit_retries=0)
        result = notifier.send({
            "username": "Momentum Alerts",
            "content": "Discord 正式提醒通道配置成功。下一步将先运行影子扫描。",
            "allowed_mentions": {"parse": []},
        })
    except Exception as exc:  # noqa: BLE001
        print(f"Webhook 测试失败：{exc}")
        return 1

    _update_env_file(ROOT / ".env.local", {
        "DISCORD_WEBHOOK_URL": webhook,
        "DISCORD_ALERT_ROLE_ID": role_id,
    })
    print(f"Webhook 测试成功（HTTP {result.get('status')}）。")
    print(f"已安全写入 {ROOT / '.env.local'}，文件权限为 600。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
