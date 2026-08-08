"""Versioned, fail-closed detector preserving the existing live semantics."""
from __future__ import annotations

from datetime import datetime
import math
from typing import Any
from zoneinfo import ZoneInfo

from src.breakouts.live.models import BreakoutSignal, DailyCandidate, QuoteSnapshot
from src.breakouts.live.settings import IntradayMonitorSettings


ALGORITHM_VERSION = "legacy-breakout-shadow-v1"
PARAMETER_VERSION = "2026-07-28.2"
TRIGGER_FAMILY = "MOMENTUM_BREAKOUT"


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class BreakoutDetector:
    def __init__(self, settings: IntradayMonitorSettings) -> None:
        self.settings = settings.validate()
        self.timezone = ZoneInfo(settings.timezone)

    def strict_metrics(
        self,
        candidate: DailyCandidate,
        quote: QuoteSnapshot,
    ) -> dict[str, Any]:
        return20 = (
            (quote.price / candidate.return_reference_close - 1.0) * 100.0
            if candidate.return_reference_close > 0 and quote.price > 0
            else float("-inf")
        )
        current_adr = (
            (quote.day_high / quote.day_low - 1.0) * 100.0
            if quote.day_high > 0 and quote.day_low > 0
            else float("-inf")
        )
        adr20_live = (
            (candidate.adr_sum_19 + current_adr) / 20.0
            if math.isfinite(current_adr)
            else float("-inf")
        )
        checks = {
            "return_20d": return20 >= self.settings.min_return_20d,
            "adr_20d": adr20_live >= self.settings.min_adr_20d,
            "dollar_volume": quote.dollar_volume >= self.settings.min_dollar_volume,
            "avg_dollar_volume": (
                candidate.avg_dollar_volume20
                >= self.settings.min_avg_dollar_volume
            ),
            "daily_setup": candidate.setup_qualified,
        }
        return {
            "return20_live": return20,
            "adr20_live": adr20_live,
            "dollar_volume": quote.dollar_volume,
            "checks": checks,
            "passed": all(checks.values()),
        }

    def quote_is_fresh(
        self,
        quote: QuoteSnapshot,
        *,
        now: datetime,
        session_date: str,
    ) -> bool:
        age = quote.age_seconds(now)
        return (
            -5.0 <= age <= self.settings.stale_after_seconds
            and quote.timestamp.strftime("%Y-%m-%d") == session_date
        )

    def should_confirm(
        self,
        candidate: DailyCandidate,
        quote: QuoteSnapshot,
        metrics: dict[str, Any],
        *,
        now: datetime,
        session_date: str,
    ) -> bool:
        if not self.quote_is_fresh(quote, now=now, session_date=session_date):
            return False
        if not self.strict_metrics(candidate, quote)["passed"]:
            return False
        levels = [candidate.breakout_level]
        opening_ranges = metrics.get("opening_ranges") or {}
        for minutes in self.settings.legacy_opening_ranges:
            opening_range = opening_ranges.get(str(minutes)) or {}
            high = _finite(opening_range.get("high"))
            if high is not None and high > 0:
                levels.append(high)
                break
        return any(
            level > 0
            and quote.day_high >= level
            and quote.price >= level * 0.995
            for level in levels
        )

    def evaluate(
        self,
        candidate: DailyCandidate,
        quote: QuoteSnapshot,
        metrics: dict[str, Any],
        *,
        now: datetime,
        session_date: str,
        market_open: bool,
    ) -> BreakoutSignal | None:
        if not market_open:
            return None
        if not self.quote_is_fresh(quote, now=now, session_date=session_date):
            return None
        strict = self.strict_metrics(candidate, quote)
        if not strict["passed"] or metrics.get("error"):
            return None

        raw_timestamp = metrics.get("last_timestamp")
        if not raw_timestamp:
            return None
        bar_timestamp = datetime.fromisoformat(str(raw_timestamp)).replace(
            tzinfo=self.timezone
        )
        bar_age = (now.astimezone(self.timezone) - bar_timestamp).total_seconds()
        if not 0 <= bar_age <= self.settings.stale_after_seconds:
            return None

        latest_price = _finite(metrics.get("last_price"))
        if latest_price is None or latest_price <= 0:
            return None
        reasons: list[str] = []
        if candidate.breakout_level > 0 and latest_price >= candidate.breakout_level:
            reasons.append("DAILY_PIVOT_BREAK")

        opening_minutes: int | None = None
        opening_high: float | None = None
        opening_ranges = metrics.get("opening_ranges") or {}
        for minutes in self.settings.legacy_opening_ranges:
            opening_range = opening_ranges.get(str(minutes)) or {}
            if opening_range.get("triggered") and opening_range.get("current_above"):
                opening_minutes = minutes
                opening_high = _finite(opening_range.get("high"))
                break

        ma10 = _finite(metrics.get("ma10"))
        ma20 = _finite(metrics.get("ma20"))
        ma50 = _finite(metrics.get("ma50"))
        ma_aligned = (
            ma10 is not None
            and ma20 is not None
            and ma50 is not None
            and ma10 > ma20 > ma50
        )
        if opening_minutes is not None and ma_aligned:
            reasons.append("OPENING_RANGE_BREAK")
        if not reasons:
            return None

        signal_type = (
            "OPENING_RANGE_BREAK"
            if "OPENING_RANGE_BREAK" in reasons
            else "BREAKOUT"
        )
        return BreakoutSignal(
            session_date=session_date,
            ticker=candidate.ticker,
            signal_type=signal_type,
            trigger_family=TRIGGER_FAMILY,
            algorithm_version=ALGORITHM_VERSION,
            parameter_version=PARAMETER_VERSION,
            triggered_at=now.astimezone(self.timezone),
            bar_timestamp=bar_timestamp,
            price=latest_price,
            breakout_level=candidate.breakout_level,
            opening_range_minutes=opening_minutes,
            opening_range_high=opening_high,
            vwap=_finite(metrics.get("vwap")),
            relative_volume=_finite(metrics.get("relative_volume")),
            ma10=ma10,
            ma20=ma20,
            ma50=ma50,
            setup_score=candidate.setup_score,
            adr20_live=float(strict["adr20_live"]),
            return20_live=float(strict["return20_live"]),
            dollar_volume=float(strict["dollar_volume"]),
            reasons=tuple(reasons),
        )
