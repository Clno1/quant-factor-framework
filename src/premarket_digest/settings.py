"""Strict non-secret and environment-only secret settings for digests."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Any, Mapping

from src.alerts.config import load_local_env
from src.alerts.discord import discord_webhook_identity
from src.config import CONFIG

from .models import DigestChannel


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _strict_bool(value: Any, *, default: bool, field_name: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    normalized = str(value).strip().casefold()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise ValueError(f"{field_name} must be a boolean")


@dataclass(frozen=True, slots=True)
class PremarketDigestSettings:
    enabled: bool = False
    timezone: str = "America/New_York"
    scheduled_window_start: str = "09:20"
    scheduled_window_end: str = "09:29"
    state_path: Path = Path("outputs/premarket_digest/state.sqlite3")
    dry_runs_dir: Path = Path("outputs/premarket_digest/dry_runs")

    momentum_enabled: bool = True
    momentum_universe: str = "US_ACTIVE"
    momentum_include_etfs: bool = False
    momentum_max_rows: int = 10
    momentum_min_exact_asof_coverage: float = 0.80
    momentum_min_evaluable_coverage: float = 0.80

    sector_rotation_enabled: bool = True
    group_universe: str = "SP500"
    group_taxonomy: str = "FMP"
    group_min_coverage: float = 0.98
    sector_top_n: int = 3
    sector_bottom_n: int = 3
    sub_industry_top_n: int = 5
    sub_industry_bottom_n: int = 5

    dashboard_base_url: str = ""
    momentum_webhook_url: str = field(default="", repr=False)
    sector_rotation_webhook_url: str = field(default="", repr=False)
    momentum_role_id: str = field(default="", repr=False)
    sector_rotation_role_id: str = field(default="", repr=False)

    def webhook_for(self, channel: DigestChannel) -> str:
        return (
            self.momentum_webhook_url
            if channel is DigestChannel.MOMENTUM
            else self.sector_rotation_webhook_url
        )

    def role_for(self, channel: DigestChannel) -> str:
        return (
            self.momentum_role_id
            if channel is DigestChannel.MOMENTUM
            else self.sector_rotation_role_id
        )


def load_premarket_digest_settings(
    raw_config: Mapping[str, Any] | None = None,
    *,
    output_root: Path | None = None,
    load_env: bool = True,
) -> PremarketDigestSettings:
    if load_env:
        load_local_env()
    root = dict(raw_config) if raw_config is not None else CONFIG.to_dict()
    cfg = _mapping(root.get("premarket_digest"))
    momentum = _mapping(cfg.get("momentum"))
    rotation = _mapping(cfg.get("sector_rotation"))
    if output_root is None:
        storage = _mapping(root.get("storage"))
        webapp = _mapping(root.get("webapp"))
        configured = storage.get("output_dir") or webapp.get("output_dir") or "outputs"
        output_root = CONFIG.abs_path(str(configured))
    output_root = Path(output_root)

    momentum_url = os.environ.get("DISCORD_MOMENTUM_WEBHOOK_URL", "").strip()
    if not momentum_url:
        momentum_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    settings = PremarketDigestSettings(
        enabled=_strict_bool(
            os.environ.get("PREMARKET_DIGEST_ENABLED", cfg.get("enabled")),
            default=False,
            field_name="premarket_digest.enabled",
        ),
        timezone=str(cfg.get("timezone") or "America/New_York"),
        scheduled_window_start=str(cfg.get("scheduled_window_start") or "09:20"),
        scheduled_window_end=str(cfg.get("scheduled_window_end") or "09:29"),
        state_path=output_root / "premarket_digest" / "state.sqlite3",
        dry_runs_dir=output_root / "premarket_digest" / "dry_runs",
        momentum_enabled=_strict_bool(
            momentum.get("enabled"), default=True, field_name="premarket_digest.momentum.enabled"
        ),
        momentum_universe=str(momentum.get("universe") or "US_ACTIVE").upper(),
        momentum_include_etfs=_strict_bool(
            momentum.get("include_etfs"),
            default=False,
            field_name="premarket_digest.momentum.include_etfs",
        ),
        momentum_max_rows=min(20, max(1, int(momentum.get("max_rows", 10)))),
        momentum_min_exact_asof_coverage=float(
            momentum.get("min_exact_asof_coverage", 0.80)
        ),
        momentum_min_evaluable_coverage=float(
            momentum.get("min_evaluable_coverage", 0.80)
        ),
        sector_rotation_enabled=_strict_bool(
            os.environ.get(
                "PREMARKET_SECTOR_ROTATION_ENABLED",
                rotation.get("enabled"),
            ),
            default=True,
            field_name="PREMARKET_SECTOR_ROTATION_ENABLED",
        ),
        group_universe=str(rotation.get("universe") or "SP500").upper(),
        group_taxonomy=str(rotation.get("taxonomy") or "FMP").upper(),
        group_min_coverage=float(rotation.get("min_coverage", 0.98)),
        sector_top_n=max(1, int(rotation.get("sector_top_n", 3))),
        sector_bottom_n=max(1, int(rotation.get("sector_bottom_n", 3))),
        sub_industry_top_n=max(1, int(rotation.get("sub_industry_top_n", 5))),
        sub_industry_bottom_n=max(1, int(rotation.get("sub_industry_bottom_n", 5))),
        dashboard_base_url=os.environ.get(
            "MOMENTUM_DASHBOARD_BASE_URL", ""
        ).strip().rstrip("/"),
        momentum_webhook_url=momentum_url,
        sector_rotation_webhook_url=os.environ.get(
            "DISCORD_SECTOR_ROTATION_WEBHOOK_URL", ""
        ).strip(),
        momentum_role_id=os.environ.get(
            "DISCORD_MOMENTUM_ROLE_ID",
            os.environ.get("DISCORD_ALERT_ROLE_ID", ""),
        ).strip(),
        sector_rotation_role_id=os.environ.get(
            "DISCORD_SECTOR_ROTATION_ROLE_ID", ""
        ).strip(),
    )
    _validate(settings)
    return settings


def _validate(settings: PremarketDigestSettings) -> None:
    if settings.timezone != "America/New_York":
        raise ValueError("premarket_digest.timezone must be America/New_York")
    if settings.momentum_universe != "US_ACTIVE":
        raise ValueError("premarket_digest.momentum.universe must be US_ACTIVE")
    if (
        settings.momentum_webhook_url
        and settings.sector_rotation_webhook_url
        and discord_webhook_identity(settings.momentum_webhook_url)
        == discord_webhook_identity(settings.sector_rotation_webhook_url)
    ):
        raise ValueError("the two premarket channels require independent Discord webhooks")
    for field_name, value in (
        ("momentum_min_exact_asof_coverage", settings.momentum_min_exact_asof_coverage),
        ("momentum_min_evaluable_coverage", settings.momentum_min_evaluable_coverage),
        ("group_min_coverage", settings.group_min_coverage),
    ):
        if not 0 <= value <= 1:
            raise ValueError(f"{field_name} must be in [0, 1]")
    for field_name, value in (
        ("scheduled_window_start", settings.scheduled_window_start),
        ("scheduled_window_end", settings.scheduled_window_end),
    ):
        if not re.fullmatch(r"\d{2}:\d{2}", value):
            raise ValueError(f"{field_name} must use zero-padded HH:MM")
        try:
            hour, minute = (int(item) for item in value.split(":"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must use zero-padded HH:MM") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"{field_name} must use zero-padded HH:MM")
    if settings.scheduled_window_start != "09:20":
        raise ValueError(
            "scheduled_window_start must stay aligned with the fixed 09:20 systemd timer"
        )
    if not "09:20" <= settings.scheduled_window_end <= "09:29":
        raise ValueError("scheduled_window_end must be between 09:20 and 09:29")


__all__ = ["PremarketDigestSettings", "load_premarket_digest_settings"]
