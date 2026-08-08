"""Configuration owned by the intraday breakout monitor."""
from __future__ import annotations

from dataclasses import dataclass
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
            enabled=bool(root.get("enabled", False)),
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
            always_tickers=always,
        )
        return settings.validate()
