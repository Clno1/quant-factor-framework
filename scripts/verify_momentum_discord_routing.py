#!/usr/bin/env python3
"""Fail closed when a momentum worker points at the wrong Discord webhook."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.alerts.discord import DiscordNotifier, discord_webhook_identity  # noqa: E402


class DiscordRoutingError(ValueError):
    """A sanitized routing mismatch that never contains webhook credentials."""


def _read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise DiscordRoutingError(f"required environment file is missing: {path}")
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
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _webhook_identity(values: dict[str, str], key: str, label: str):
    webhook = str(values.get(key) or "").strip()
    if not webhook:
        raise DiscordRoutingError(f"{label} webhook is not configured")
    try:
        DiscordNotifier(webhook)
    except (TypeError, ValueError):
        raise DiscordRoutingError(f"{label} webhook is invalid") from None
    return discord_webhook_identity(webhook)


def _strict_bool(values: dict[str, str], key: str, *, default: bool) -> bool:
    raw = values.get(key)
    if raw is None:
        return default
    normalized = str(raw).strip().casefold()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise DiscordRoutingError(f"{key} must be true or false")


def verify_routing(
    component: str,
    *,
    hourly_env: Path,
    premarket_env: Path,
    intraday_env: Path,
) -> None:
    premarket = _read_env(premarket_env)
    canonical = _webhook_identity(
        premarket,
        "DISCORD_MOMENTUM_WEBHOOK_URL",
        "canonical momentum",
    )

    if component in {"premarket", "all"}:
        sector_enabled = _strict_bool(
            premarket,
            "PREMARKET_SECTOR_ROTATION_ENABLED",
            default=False,
        )
        sector = str(premarket.get("DISCORD_SECTOR_ROTATION_WEBHOOK_URL") or "").strip()
        if sector_enabled and not sector:
            raise DiscordRoutingError(
                "sector rotation webhook is required when the channel is enabled"
            )
        if sector and _webhook_identity(
            premarket,
            "DISCORD_SECTOR_ROTATION_WEBHOOK_URL",
            "sector rotation",
        ) == canonical:
            raise DiscordRoutingError(
                "momentum and sector rotation webhooks must be independent"
            )

    if component in {"hourly", "all"}:
        hourly = _read_env(hourly_env)
        actual = _webhook_identity(hourly, "DISCORD_WEBHOOK_URL", "hourly momentum")
        if actual != canonical:
            raise DiscordRoutingError(
                "hourly momentum webhook does not match the canonical momentum channel"
            )

    if component in {"intraday", "all"}:
        intraday = _read_env(intraday_env)
        delivery_enabled = _strict_bool(
            intraday,
            "INTRADAY_MOMENTUM_DISCORD_ENABLED",
            default=False,
        )
        configured = bool(
            str(intraday.get("INTRADAY_MOMENTUM_DISCORD_WEBHOOK_URL") or "").strip()
        )
        if delivery_enabled or configured:
            actual = _webhook_identity(
                intraday,
                "INTRADAY_MOMENTUM_DISCORD_WEBHOOK_URL",
                "intraday momentum",
            )
            if actual != canonical:
                raise DiscordRoutingError(
                    "intraday momentum webhook does not match the canonical momentum channel"
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--component",
        choices=("hourly", "premarket", "intraday", "all"),
        required=True,
    )
    parser.add_argument(
        "--hourly-env",
        type=Path,
        default=Path("/etc/quant/momentum-alerts.env"),
    )
    parser.add_argument(
        "--premarket-env",
        type=Path,
        default=Path("/etc/quant/premarket-digest.env"),
    )
    parser.add_argument(
        "--intraday-env",
        type=Path,
        default=Path("/etc/quant/intraday-momentum-monitor.env"),
    )
    args = parser.parse_args()
    try:
        verify_routing(
            args.component,
            hourly_env=args.hourly_env,
            premarket_env=args.premarket_env,
            intraday_env=args.intraday_env,
        )
    except DiscordRoutingError as exc:
        print(json.dumps({
            "component": args.component,
            "discord_routing": "failed",
            "error": str(exc),
        }, sort_keys=True))
        return 2
    print(json.dumps({
        "component": args.component,
        "discord_routing": "ok",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
