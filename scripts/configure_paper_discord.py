#!/usr/bin/env python3
"""Securely configure the paper-trading Discord channel on one host."""
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

from src.alerts.discord import (  # noqa: E402
    DiscordNotifier,
    discord_webhook_identity,
)
from src.papertrading.notifications import (  # noqa: E402
    PaperNotificationService,
    PaperNotificationSettings,
)
from src.utils.env import load_local_env  # noqa: E402


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
    descriptor, temporary = tempfile.mkstemp(
        prefix=".paper-notifications.",
        dir=path.parent,
    )
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


def _read_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _assert_independent_webhook(webhook: str, *, env_file: Path) -> None:
    selected = discord_webhook_identity(webhook)
    candidate_files = {
        env_file,
        Path("/etc/quant/momentum-alerts.env"),
        Path("/etc/quant/intraday-momentum-monitor.env"),
        Path("/etc/quant/premarket-digest.env"),
        ROOT / ".env.local",
    }
    keys = {
        "DISCORD_WEBHOOK_URL",
        "INTRADAY_MOMENTUM_DISCORD_WEBHOOK_URL",
        "DISCORD_MOMENTUM_WEBHOOK_URL",
        "DISCORD_SECTOR_ROTATION_WEBHOOK_URL",
    }
    for path in candidate_files:
        for key, existing in _read_env_values(path).items():
            if key not in keys or not existing:
                continue
            if discord_webhook_identity(existing) == selected:
                raise ValueError(
                    "模拟交易频道必须使用独立 Webhook，不能复用动量或板块频道"
                )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=ROOT / ".env.local",
    )
    parser.add_argument(
        "--dashboard-base-url",
        default="",
        help="Public main-site URL used in message links.",
    )
    parser.add_argument(
        "--test-send",
        action="store_true",
        help="Send one non-mentioning configuration test.",
    )
    args = parser.parse_args(argv)
    load_local_env(args.env_file)
    existing = os.environ.get("PAPER_DISCORD_WEBHOOK_URL", "").strip()
    print("Discord 模拟交易频道配置")
    print("Webhook 输入不会显示，也不会写入日志。")
    webhook = getpass.getpass(
        "#模拟交易 Incoming Webhook URL（留空保留现值）: "
    ).strip() or existing
    if not webhook:
        print("必须提供 Webhook；未写入任何更改。")
        return 1
    try:
        _assert_independent_webhook(webhook, env_file=args.env_file)
        notifier = DiscordNotifier(webhook, max_rate_limit_retries=0)
        baseline_env = dict(os.environ)
        baseline_env["PAPER_DISCORD_DELIVERY_ENABLED"] = "false"
        baseline_env["PAPER_DISCORD_WEBHOOK_URL"] = ""
        baseline_settings = PaperNotificationSettings.from_env(baseline_env)
        baseline_service = PaperNotificationService(baseline_settings)
        with baseline_service.state.run_lock():
            baselined = baseline_service.reconcile_fills(baseline=True)
        if args.test_send:
            notifier.send({
                "username": "Paper Trading",
                "content": "模拟交易频道配置成功。该频道只发送模拟成交与每日账户日结。",
                "allowed_mentions": {"parse": []},
            })
    except Exception as exc:  # noqa: BLE001
        print(f"Webhook 验证失败：{exc}")
        return 1
    dashboard = (
        args.dashboard_base_url.strip().rstrip("/")
        or os.environ.get("PAPER_DASHBOARD_BASE_URL", "").strip().rstrip("/")
    )
    updates = {
        "PAPER_DISCORD_WEBHOOK_URL": webhook,
        "PAPER_DISCORD_DELIVERY_ENABLED": "true",
        "PAPER_NOTIFICATION_STATE_PATH": str(baseline_settings.state_path),
        "PAPER_DASHBOARD_BASE_URL": dashboard,
        "PAPER_DISCORD_MAX_ATTEMPTS": "3",
        "PAPER_DISCORD_BATCH_LIMIT": "25",
    }
    _update_env_file(args.env_file, updates)
    print(f"配置已安全写入 {args.env_file}，文件权限为 600。")
    print(f"已将 {baselined} 笔尚未登记的历史成交设为 baseline，不会补发。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
