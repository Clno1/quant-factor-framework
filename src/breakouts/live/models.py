"""Typed values shared by the isolated intraday monitor."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
import math
from typing import Any
from zoneinfo import ZoneInfo


_NEW_YORK = ZoneInfo("America/New_York")


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _aware_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        timestamp = _finite(value, float("nan"))
        if math.isfinite(timestamp):
            parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        else:
            parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_NEW_YORK)
    return parsed.astimezone(_NEW_YORK)


class MonitorSymbolState(StrEnum):
    WATCHING = "WATCHING"
    ARMED = "ARMED"
    TRIGGERED = "TRIGGERED"
    COOLDOWN = "COOLDOWN"


@dataclass(frozen=True)
class DailyCandidate:
    ticker: str
    name: str
    sector: str
    setup_score: int
    daily_pivot: float
    previous_high: float
    adr20: float
    avg_dollar_volume20: float
    source_data_date: str
    setup_qualified: bool
    daily_status: str
    return_reference_close: float
    adr_sum_19: float
    forced_watch: bool = False

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "DailyCandidate":
        return cls(
            ticker=str(value.get("ticker") or "").strip().upper(),
            name=str(value.get("name") or "").strip(),
            sector=str(value.get("sector") or "").strip(),
            setup_score=int(_finite(value.get("setup_score", value.get("score")))),
            daily_pivot=_finite(value.get("daily_pivot", value.get("pivot"))),
            previous_high=_finite(value.get("previous_high")),
            adr20=_finite(value.get("adr20", value.get("adr_20d"))),
            avg_dollar_volume20=_finite(
                value.get("avg_dollar_volume20", value.get("avg_dollar_volume_20d"))
            ),
            source_data_date=str(
                value.get("source_data_date", value.get("data_date")) or ""
            ),
            setup_qualified=bool(value.get("setup_qualified")),
            daily_status=str(
                value.get("daily_status", value.get("status")) or "FORMING"
            ).upper(),
            return_reference_close=_finite(value.get("return_reference_close")),
            adr_sum_19=_finite(value.get("adr_sum_19")),
            forced_watch=bool(value.get("forced_watch")),
        )

    @property
    def breakout_level(self) -> float:
        levels = [value for value in (self.daily_pivot, self.previous_high) if value > 0]
        return max(levels, default=0.0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QuoteSnapshot:
    ticker: str
    timestamp: datetime
    price: float
    cumulative_volume: float
    day_high: float
    day_low: float
    open: float
    previous_close: float
    change_percentage: float

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "QuoteSnapshot":
        return cls(
            ticker=str(value.get("ticker", value.get("symbol")) or "").strip().upper(),
            timestamp=_aware_datetime(value.get("timestamp")),
            price=_finite(value.get("price")),
            cumulative_volume=max(0.0, _finite(value.get("volume"))),
            day_high=_finite(value.get("dayHigh", value.get("day_high"))),
            day_low=_finite(value.get("dayLow", value.get("day_low"))),
            open=_finite(value.get("open")),
            previous_close=_finite(value.get("previousClose", value.get("previous_close"))),
            change_percentage=_finite(
                value.get("changePercentage", value.get("change_percentage"))
            ),
        )

    @property
    def dollar_volume(self) -> float:
        return self.price * self.cumulative_volume

    def age_seconds(self, now: datetime) -> float:
        aware_now = _aware_datetime(now)
        return (aware_now - self.timestamp).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["timestamp"] = self.timestamp.isoformat(timespec="seconds")
        return payload


@dataclass(frozen=True)
class BreakoutSignal:
    session_date: str
    ticker: str
    signal_type: str
    trigger_family: str
    algorithm_version: str
    parameter_version: str
    triggered_at: datetime
    bar_timestamp: datetime
    price: float
    breakout_level: float
    opening_range_minutes: int | None
    opening_range_high: float | None
    vwap: float | None
    relative_volume: float | None
    ma10: float | None
    ma20: float | None
    ma50: float | None
    setup_score: int
    adr20_live: float
    return20_live: float
    dollar_volume: float
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["triggered_at"] = self.triggered_at.isoformat(timespec="seconds")
        payload["bar_timestamp"] = self.bar_timestamp.isoformat(timespec="seconds")
        payload["reasons"] = list(self.reasons)
        return payload
