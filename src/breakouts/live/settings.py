"""Configuration owned by the intraday breakout monitor."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any

from src.config import CONFIG, PROJECT_ROOT


def _nested(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    value: Any = mapping
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def _strict_bool(value: Any, *, default: bool, field_name: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{field_name} must be a boolean")


@dataclass(frozen=True)
class IntradayMonitorSettings:
    enabled: bool = False
    universe: str = "US_ACTIVE"
    include_etfs: bool = False
    max_symbols: int = 600
    broad_refresh_minutes: int = 5
    active_max_symbols: int = 40
    active_hard_limit: int = 60
    quote_chunk_size: int = 100
    min_return_20d: float = 20.0
    min_adr_20d: float = 6.0
    min_dollar_volume: float = 10_000_000.0
    min_avg_dollar_volume: float = 10_000_000.0
    broad_min_return_20d: float = 10.0
    broad_min_adr_20d: float = 4.5
    broad_min_current_dollar_volume: float = 5_000_000.0
    min_exact_daily_coverage: float = 0.80
    bars_interval_minutes: int = 1
    stale_after_seconds: int = 90
    preload_bars: int = 60
    opening_range_windows: tuple[int, ...] = (5, 15)
    detector_interval_minutes: int = 5
    legacy_opening_ranges: tuple[int, ...] = (60, 30)
    max_concurrent_requests: int = 4
    heartbeat_seconds: int = 30
    poll_offset_seconds: int = 8
    exact_confirm_cooldown_seconds: int = 50
    timezone: str = "America/New_York"
    cooldown_minutes: int = 20
    delivery_enabled: bool = False
    discord_webhook_url: str = field(default="", repr=False)
    discord_role_id: str = ""
    dashboard_base_url: str = ""
    max_delivery_attempts: int = 3
    max_messages_per_cycle: int = 5
    required_shadow_sessions: int = 5
    observation_min_cycle_coverage: float = 0.85
    observation_max_error_cycle_ratio: float = 0.05
    observation_max_cycle_p95_seconds: float = 30.0
    always_tickers: tuple[str, ...] = ()
    state_path: Path = (
        PROJECT_ROOT / "outputs" / "intraday_momentum_monitor" / "state.sqlite3"
    )
    snapshots_dir: Path = (
        PROJECT_ROOT / "outputs" / "intraday_momentum_monitor" / "snapshots"
    )

    def validate(self) -> "IntradayMonitorSettings":
        if self.universe != "US_ACTIVE":
            raise ValueError("intraday monitor currently requires the US_ACTIVE universe")
        if self.bars_interval_minutes != 1:
            raise ValueError("formal evaluation requires completed 1-minute source bars")
        if self.active_max_symbols > self.active_hard_limit:
            raise ValueError("active_max_symbols cannot exceed active_hard_limit")
        if not 1 <= self.active_hard_limit <= 60:
            raise ValueError("active_hard_limit must be between 1 and 60")
        if not 1 <= self.max_symbols <= 1000:
            raise ValueError("max_symbols must be between 1 and 1000")
        if self.broad_refresh_minutes < 1:
            raise ValueError("broad_refresh_minutes must be positive")
        if self.stale_after_seconds < 60:
            raise ValueError("stale_after_seconds must be at least 60")
        if not 0.5 <= self.min_exact_daily_coverage <= 1.0:
            raise ValueError("min_exact_daily_coverage must be between 0.5 and 1.0")
        if not 1 <= self.max_concurrent_requests <= 8:
            raise ValueError("max_concurrent_requests must be between 1 and 8")
        if self.cooldown_minutes < 0:
            raise ValueError("cooldown_minutes cannot be negative")
        if not 1 <= self.max_delivery_attempts <= 10:
            raise ValueError("max_delivery_attempts must be between 1 and 10")
        if not 1 <= self.max_messages_per_cycle <= 20:
            raise ValueError("max_messages_per_cycle must be between 1 and 20")
        if not 1 <= self.required_shadow_sessions <= 20:
            raise ValueError("required_shadow_sessions must be between 1 and 20")
        if not 0.5 <= self.observation_min_cycle_coverage <= 1.0:
            raise ValueError("observation_min_cycle_coverage must be between 0.5 and 1.0")
        if not 0.0 <= self.observation_max_error_cycle_ratio <= 1.0:
            raise ValueError("observation_max_error_cycle_ratio must be between 0 and 1")
        if self.observation_max_cycle_p95_seconds <= 0:
            raise ValueError("observation_max_cycle_p95_seconds must be positive")
        return self

    @classmethod
    def load(cls) -> "IntradayMonitorSettings":
        root = CONFIG.to_dict().get("intraday_momentum_monitor") or {}
        legacy = CONFIG.to_dict().get("momentum_alerts") or {}
        configured_always = legacy.get("always_tickers") or []
        if isinstance(configured_always, str):
            configured_always = configured_always.split(",")
        always = tuple(dict.fromkeys(
            str(value).strip().upper()
            for value in configured_always
            if str(value).strip()
        ))
        opening_windows = _nested(
            root, "opening_range", "windows", default=[5, 15]
        ) or [5, 15]
        settings = cls(
            enabled=_strict_bool(
                os.environ.get(
                    "INTRADAY_MOMENTUM_MONITOR_ENABLED",
                    root.get("enabled", False),
                ),
                default=False,
                field_name="INTRADAY_MOMENTUM_MONITOR_ENABLED",
            ),
            universe=str(_nested(root, "universe", "name", default="US_ACTIVE")).upper(),
            include_etfs=bool(
                _nested(root, "asset_types", "include_etfs", default=False)
            ),
            max_symbols=int(_nested(root, "universe", "max_symbols", default=600)),
            broad_refresh_minutes=int(
                _nested(root, "universe", "refresh_minutes", default=5)
            ),
            active_max_symbols=int(
                _nested(root, "universe", "active_max_symbols", default=40)
            ),
            active_hard_limit=int(
                _nested(root, "universe", "active_hard_limit", default=60)
            ),
            quote_chunk_size=int(
                _nested(root, "universe", "quote_chunk_size", default=100)
            ),
            min_return_20d=float(
                _nested(root, "strict_scan", "min_return_20d", default=20.0)
            ),
            min_adr_20d=float(
                _nested(root, "strict_scan", "min_adr_20d", default=6.0)
            ),
            min_dollar_volume=float(
                _nested(root, "strict_scan", "min_dollar_volume_m", default=10.0)
            ) * 1_000_000,
            min_avg_dollar_volume=float(
                _nested(root, "strict_scan", "min_avg_dollar_volume_m", default=10.0)
            ) * 1_000_000,
            broad_min_return_20d=float(
                _nested(root, "broad_scan", "min_return_20d", default=10.0)
            ),
            broad_min_adr_20d=float(
                _nested(root, "broad_scan", "min_adr_20d", default=4.5)
            ),
            broad_min_current_dollar_volume=float(
                _nested(
                    root,
                    "broad_scan",
                    "min_current_dollar_volume_m",
                    default=5.0,
                )
            ) * 1_000_000,
            min_exact_daily_coverage=float(
                _nested(root, "daily_data", "min_exact_coverage", default=0.80)
            ),
            bars_interval_minutes=int(
                _nested(root, "bars", "interval_minutes", default=1)
            ),
            stale_after_seconds=int(
                _nested(root, "bars", "stale_after_seconds", default=90)
            ),
            preload_bars=int(_nested(root, "bars", "preload_bars", default=60)),
            opening_range_windows=tuple(int(value) for value in opening_windows),
            max_concurrent_requests=int(
                _nested(root, "runtime", "max_concurrent_requests", default=4)
            ),
            heartbeat_seconds=int(
                _nested(root, "runtime", "heartbeat_seconds", default=30)
            ),
            poll_offset_seconds=int(
                _nested(root, "runtime", "poll_offset_seconds", default=8)
            ),
            exact_confirm_cooldown_seconds=int(
                _nested(
                    root,
                    "runtime",
                    "exact_confirm_cooldown_seconds",
                    default=50,
                )
            ),
            timezone=str(
                _nested(root, "runtime", "timezone", default="America/New_York")
            ),
            cooldown_minutes=int(
                _nested(root, "notifications", "cooldown_minutes", default=20)
            ),
            delivery_enabled=_strict_bool(
                os.environ.get(
                    "INTRADAY_MOMENTUM_DISCORD_ENABLED",
                    _nested(root, "notifications", "delivery_enabled", default=False),
                ),
                default=False,
                field_name="INTRADAY_MOMENTUM_DISCORD_ENABLED",
            ),
            discord_webhook_url=os.environ.get(
                "INTRADAY_MOMENTUM_DISCORD_WEBHOOK_URL", ""
            ).strip(),
            discord_role_id=os.environ.get(
                "INTRADAY_MOMENTUM_DISCORD_ROLE_ID", ""
            ).strip(),
            dashboard_base_url=os.environ.get(
                "MOMENTUM_DASHBOARD_BASE_URL", ""
            ).strip().rstrip("/"),
            max_delivery_attempts=int(
                _nested(root, "notifications", "max_delivery_attempts", default=3)
            ),
            max_messages_per_cycle=int(
                _nested(root, "notifications", "max_messages_per_cycle", default=5)
            ),
            required_shadow_sessions=int(
                _nested(root, "observation", "required_sessions", default=5)
            ),
            observation_min_cycle_coverage=float(
                _nested(root, "observation", "min_cycle_coverage", default=0.85)
            ),
            observation_max_error_cycle_ratio=float(
                _nested(root, "observation", "max_error_cycle_ratio", default=0.05)
            ),
            observation_max_cycle_p95_seconds=float(
                _nested(root, "observation", "max_cycle_p95_seconds", default=30.0)
            ),
            always_tickers=always,
        )
        return settings.validate()
