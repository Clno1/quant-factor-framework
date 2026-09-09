#!/usr/bin/env python3
"""Prepare the protected intraday-monitor env from an existing momentum env."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.alerts.discord import DiscordNotifier, is_discord_snowflake  # noqa: E402


def _read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"source environment file does not exist: {path}")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _first(values: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = str(values.get(key) or "").strip()
        if value:
            return value
    return ""


def prepare_environment(
    source_paths: list[Path],
    destination: Path,
    *,
    delivery_enabled: bool,
) -> tuple[str, ...]:
    source: dict[str, str] = {}
    for path in source_paths:
        source.update(_read_env(path))

    fmp_key = _first(source, "FMP_API_KEY")
    webhook = _first(
        source,
        "INTRADAY_MOMENTUM_DISCORD_WEBHOOK_URL",
        "DISCORD_MOMENTUM_WEBHOOK_URL",
        "DISCORD_WEBHOOK_URL",
    )
    role_id = _first(
        source,
        "INTRADAY_MOMENTUM_DISCORD_ROLE_ID",
        "DISCORD_MOMENTUM_ROLE_ID",
        "DISCORD_ALERT_ROLE_ID",
    )
    dashboard = _first(source, "MOMENTUM_DASHBOARD_BASE_URL").rstrip("/")
    if not fmp_key:
        raise ValueError("FMP_API_KEY is missing from the source environment")
    if delivery_enabled and not webhook:
        raise ValueError("a Discord momentum webhook is required when delivery is enabled")
    if webhook:
        DiscordNotifier(webhook)
    if role_id and not is_discord_snowflake(role_id):
        role_id = ""

    updates = {
        "FMP_API_KEY": fmp_key,
        "INTRADAY_MOMENTUM_MONITOR_ENABLED": "true",
        "INTRADAY_MOMENTUM_DISCORD_ENABLED": (
            "true" if delivery_enabled else "false"
        ),
        "INTRADAY_MOMENTUM_DISCORD_WEBHOOK_URL": webhook,
        "INTRADAY_MOMENTUM_DISCORD_ROLE_ID": role_id,
        "MOMENTUM_DASHBOARD_BASE_URL": dashboard,
    }
    if any("\n" in value or "\r" in value for value in updates.values()):
        raise ValueError("environment values cannot contain line breaks")

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".intraday-momentum-env.",
        dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for key, value in updates.items():
                handle.write(f"{key}={value}\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return tuple(updates)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-env",
        action="append",
        type=Path,
        required=True,
        help="Existing protected env; later files override earlier files.",
    )
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument(
        "--enable-delivery",
        action="store_true",
        help="Arm Discord only after the separate five-session gate passes.",
    )
    args = parser.parse_args()
    keys = prepare_environment(
        args.source_env,
        args.env_file,
        delivery_enabled=args.enable_delivery,
    )
    print(f"prepared={args.env_file}")
    print(f"mode={'auto_live_after_gate' if args.enable_delivery else 'shadow_only'}")
    print("configured_keys=" + ",".join(keys))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
