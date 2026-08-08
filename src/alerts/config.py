"""Configuration and local secret loading for momentum alerts."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Iterable

from src.config import CONFIG, PROJECT_ROOT
from src.utils.env import load_local_env


def _nested(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    value: Any = mapping
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def _tickers(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        str(value).strip().upper()
        for value in values
        if str(value).strip()
    ))


@dataclass(frozen=True)
class AlertSettings:
    universe: str = "US_ACTIVE"
    exchange: str = "NASDAQ"
    include_etfs: bool = False
    broad_min_return_20d: float = 10.0
    broad_min_adr_20d: float = 4.5
    broad_min_current_dollar_volume: float = 5_000_000.0
    min_avg_dollar_volume: float = 10_000_000.0
    broad_max_symbols: int = 600
    strict_min_return_20d: float = 20.0
    strict_min_adr_20d: float = 6.0
    strict_min_dollar_volume: float = 10_000_000.0
    strict_min_avg_dollar_volume: float = 10_000_000.0
    quote_chunk_size: int = 100
    intraday_enabled: bool = False
    intraday_interval: int = 5
    intraday_max_symbols: int = 25
    intraday_lookback_days: int = 3
    notification_max_rows: int = 10
    mention_levels: tuple[str, ...] = ("READY", "BREAKOUT", "OPENING_RANGE_BREAK")
    send_empty_digest: bool = False
    always_tickers: tuple[str, ...] = ()
    discord_webhook_url: str = ""
    discord_role_id: str = ""
    dashboard_base_url: str = ""
    state_path: Path = PROJECT_ROOT / "outputs" / "momentum_alerts" / "state.sqlite3"
    runs_dir: Path = PROJECT_ROOT / "outputs" / "momentum_alerts" / "runs"

    @classmethod
    def load(
        cls,
        *,
        extra_tickers: Iterable[str] = (),
        load_env: bool = True,
        include_environment_tickers: bool = True,
    ) -> "AlertSettings":
        if load_env:
            load_local_env()
        root = CONFIG.to_dict().get("momentum_alerts") or {}
        configured_always = root.get("always_tickers") or []
        if isinstance(configured_always, str):
            configured_always = configured_always.split(",")
        elif not isinstance(configured_always, (list, tuple, set)):
            raise ValueError("momentum_alerts.always_tickers must be a list or CSV string")
        configured_extra = (
            os.environ.get("MOMENTUM_ALERT_EXTRA_TICKERS", "")
            if include_environment_tickers
            else ""
        )
        always = _tickers([
            *configured_always,
            *configured_extra.split(","),
            *extra_tickers,
        ])
        mention_levels = _nested(
            root, "notifications", "mention_levels", default=list(cls.mention_levels)
        )
        return cls(
            universe=str(root.get("universe") or "US_ACTIVE").upper(),
            exchange=str(root.get("exchange") or "NASDAQ").upper(),
            include_etfs=bool(_nested(root, "asset_types", "include_etfs", default=False)),
            broad_min_return_20d=float(_nested(root, "broad_scan", "min_return_20d", default=10.0)),
            broad_min_adr_20d=float(_nested(root, "broad_scan", "min_adr_20d", default=4.5)),
            broad_min_current_dollar_volume=float(
                _nested(root, "broad_scan", "min_current_dollar_volume_m", default=5.0)
            ) * 1_000_000,
            min_avg_dollar_volume=float(
                _nested(root, "broad_scan", "min_avg_dollar_volume_m", default=10.0)
            ) * 1_000_000,
            broad_max_symbols=min(1000, max(1, int(
                _nested(root, "broad_scan", "max_symbols", default=600)
            ))),
            strict_min_return_20d=float(_nested(root, "strict_scan", "min_return_20d", default=20.0)),
            strict_min_adr_20d=float(_nested(root, "strict_scan", "min_adr_20d", default=6.0)),
            strict_min_dollar_volume=float(
                _nested(root, "strict_scan", "min_dollar_volume_m", default=10.0)
            ) * 1_000_000,
            strict_min_avg_dollar_volume=float(
                _nested(root, "strict_scan", "min_avg_dollar_volume_m", default=10.0)
            ) * 1_000_000,
            quote_chunk_size=min(500, max(1, int(
                _nested(root, "quotes", "chunk_size", default=100)
            ))),
            intraday_enabled=bool(_nested(root, "intraday", "enabled", default=False)),
            intraday_interval=int(_nested(root, "intraday", "interval", default=5)),
            intraday_max_symbols=max(1, int(
                _nested(root, "intraday", "max_symbols", default=25)
            )),
            intraday_lookback_days=max(2, int(
                _nested(root, "intraday", "lookback_days", default=3)
            )),
            notification_max_rows=min(20, max(1, int(
                _nested(root, "notifications", "max_rows", default=10)
            ))),
            mention_levels=tuple(str(value).upper() for value in (mention_levels or [])),
            send_empty_digest=bool(_nested(
                root, "notifications", "send_empty_digest", default=False
            )),
            always_tickers=always,
            discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL", "").strip(),
            discord_role_id=os.environ.get("DISCORD_ALERT_ROLE_ID", "").strip(),
            dashboard_base_url=os.environ.get("MOMENTUM_DASHBOARD_BASE_URL", "").strip().rstrip("/"),
        )

    @property
    def discord_configured(self) -> bool:
        return bool(self.discord_webhook_url)
