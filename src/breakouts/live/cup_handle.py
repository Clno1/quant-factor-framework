"""Versioned daily-cup and completed-five-minute handle detection."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import math
import time
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from src.breakouts.live.models import BreakoutSignal, DailyCandidate, QuoteSnapshot
from src.breakouts.live.settings import IntradayMonitorSettings


CUP_HANDLE_ALGORITHM_VERSION = "daily-cup-5m-handle-shadow-v2"
CUP_HANDLE_PARAMETER_VERSION = "2026-09-01.1"
CUP_HANDLE_TRIGGER_FAMILY = "CUP_HANDLE_BREAKOUT"


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class DailyCupSetup:
    qualified: bool
    rejection_reason: str
    left_rim_date: str = ""
    right_rim_date: str = ""
    bottom_date: str = ""
    left_rim: float = 0.0
    right_rim: float = 0.0
    bottom: float = 0.0
    depth_pct: float = 0.0
    width_sessions: int = 0
    rim_tolerance_pct: float = 0.0
    bottom_position: float = 0.0
    recovery_ratio: float = 0.0
    volume_contraction_ratio: float = 0.0
    score: float = 0.0

    @property
    def breakout_level(self) -> float:
        return max(self.left_rim, self.right_rim)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CupHandleEvaluation:
    ticker: str
    session_date: str
    outcome: str
    rejection_reason: str
    evaluated_at: datetime
    latency_ms: float
    bar_count: int
    details: dict[str, Any]
    signal: BreakoutSignal | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evaluated_at"] = self.evaluated_at.isoformat(timespec="seconds")
        payload["signal"] = self.signal.to_dict() if self.signal is not None else None
        return payload


def _normalized_daily_frame(
    frame: pd.DataFrame,
    *,
    asof: str | None,
) -> pd.DataFrame:
    required = ["high", "low", "close", "volume"]
    if frame is None or frame.empty or any(column not in frame for column in required):
        return pd.DataFrame(columns=required)
    data = frame[required].copy()
    data.index = pd.to_datetime(data.index, errors="coerce")
    data = data.loc[~data.index.isna()]
    if asof:
        data = data.loc[data.index.normalize() <= pd.Timestamp(asof).normalize()]
    for column in required:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["high", "low", "close"])
    data = data.loc[
        (data["high"] > 0)
        & (data["low"] > 0)
        & (data["close"] > 0)
        & (data["high"] >= data["low"])
    ]
    return data.loc[~data.index.duplicated(keep="last")].sort_index()


def _local_high_indices(values: Sequence[float], radius: int = 2) -> list[int]:
    result: list[int] = []
    for index, value in enumerate(values):
        start = max(0, index - radius)
        end = min(len(values), index + radius + 1)
        if value >= max(values[start:end]):
            result.append(index)
    return result


def detect_daily_cup(
    frame: pd.DataFrame,
    *,
    settings: IntradayMonitorSettings,
    asof: str | None = None,
) -> DailyCupSetup:
    """Find the best recent U-shaped cup using only bars visible at ``asof``."""
    data = _normalized_daily_frame(frame, asof=asof)
    minimum = settings.cup_min_width_sessions + settings.cup_right_rim_search_sessions
    if len(data) < minimum:
        return DailyCupSetup(False, "INSUFFICIENT_DAILY_BARS")
    data = data.tail(settings.cup_lookback_sessions)
    highs = data["high"].astype(float).tolist()
    lows = data["low"].astype(float).tolist()
    closes = data["close"].astype(float).tolist()
    volumes = data["volume"].fillna(0.0).clip(lower=0.0).astype(float).tolist()
    local_highs = _local_high_indices(highs)
    right_start = max(
        settings.cup_min_width_sessions,
        len(data) - settings.cup_right_rim_search_sessions,
    )
    right_candidates = [index for index in local_highs if index >= right_start]
    if not right_candidates:
        right_candidates = list(range(right_start, len(data)))

    reason_priority = {
        "RIM_MISMATCH": 1,
        "CUP_DEPTH_OUT_OF_RANGE": 2,
        "BOTTOM_POSITION_INVALID": 3,
        "RIGHT_RIM_RECOVERY_INCOMPLETE": 4,
        "CUP_VOLUME_NOT_CONTRACTING": 5,
    }
    best_failure: tuple[int, float, DailyCupSetup] | None = None
    qualified: list[DailyCupSetup] = []
    for right in right_candidates:
        left_min = max(0, right - settings.cup_max_width_sessions + 1)
        left_max = right - settings.cup_min_width_sessions + 1
        for left in (index for index in local_highs if left_min <= index <= left_max):
            width = right - left + 1
            if width < settings.cup_min_width_sessions:
                continue
            side_margin = max(2, int(round(width * settings.cup_min_side_fraction)))
            bottom_start = left + side_margin
            bottom_end = right - side_margin
            if bottom_start > bottom_end:
                continue
            bottom = min(range(bottom_start, bottom_end + 1), key=lows.__getitem__)
            left_rim = highs[left]
            right_rim = highs[right]
            rim_mean = (left_rim + right_rim) / 2.0
            cup_bottom = lows[bottom]
            if rim_mean <= 0 or cup_bottom <= 0 or cup_bottom >= rim_mean:
                continue
            rim_tolerance = abs(left_rim - right_rim) / max(left_rim, right_rim) * 100.0
            depth = (rim_mean - cup_bottom) / rim_mean * 100.0
            bottom_position = (bottom - left) / max(1, right - left)
            recovery = (
                (closes[right] - cup_bottom) / max(1e-12, left_rim - cup_bottom)
            )
            third = max(2, width // 3)
            left_volume = sum(volumes[left:left + third]) / third
            right_values = volumes[max(left, right - third + 1):right + 1]
            right_volume = sum(right_values) / max(1, len(right_values))
            contraction = right_volume / left_volume if left_volume > 0 else float("inf")

            failure = ""
            if rim_tolerance > settings.cup_max_rim_tolerance_pct:
                failure = "RIM_MISMATCH"
            elif not settings.cup_min_depth_pct <= depth <= settings.cup_max_depth_pct:
                failure = "CUP_DEPTH_OUT_OF_RANGE"
            elif not settings.cup_min_bottom_position <= bottom_position <= settings.cup_max_bottom_position:
                failure = "BOTTOM_POSITION_INVALID"
            elif recovery < settings.cup_min_right_rim_recovery:
                failure = "RIGHT_RIM_RECOVERY_INCOMPLETE"
            elif contraction > settings.cup_max_volume_contraction_ratio:
                failure = "CUP_VOLUME_NOT_CONTRACTING"

            depth_fit = max(
                0.0,
                1.0 - abs(depth - settings.cup_ideal_depth_pct)
                / max(settings.cup_ideal_depth_pct, 1.0),
            )
            center_fit = max(0.0, 1.0 - abs(bottom_position - 0.5) * 2.0)
            rim_fit = max(
                0.0,
                1.0 - rim_tolerance / max(settings.cup_max_rim_tolerance_pct, 0.1),
            )
            volume_fit = max(0.0, min(1.0, 1.0 - contraction + 0.5))
            score = 100.0 * (
                0.30 * rim_fit
                + 0.25 * depth_fit
                + 0.25 * center_fit
                + 0.20 * volume_fit
            )
            setup = DailyCupSetup(
                qualified=not failure,
                rejection_reason=failure or "MATCH",
                left_rim_date=pd.Timestamp(data.index[left]).strftime("%Y-%m-%d"),
                right_rim_date=pd.Timestamp(data.index[right]).strftime("%Y-%m-%d"),
                bottom_date=pd.Timestamp(data.index[bottom]).strftime("%Y-%m-%d"),
                left_rim=left_rim,
                right_rim=right_rim,
                bottom=cup_bottom,
                depth_pct=depth,
                width_sessions=width,
                rim_tolerance_pct=rim_tolerance,
                bottom_position=bottom_position,
                recovery_ratio=recovery,
                volume_contraction_ratio=contraction,
                score=score,
            )
            if not failure:
                qualified.append(setup)
            else:
                candidate_failure = (reason_priority[failure], score, setup)
                if best_failure is None or candidate_failure[:2] > best_failure[:2]:
                    best_failure = candidate_failure

    if qualified:
        return max(
            qualified,
            key=lambda value: (
                value.score,
                value.right_rim_date,
                value.width_sessions,
            ),
        )
    if best_failure is not None:
        return best_failure[2]
    return DailyCupSetup(False, "NO_VALID_RIM_PAIR")


def _bar_timestamp(value: dict[str, Any], timezone: ZoneInfo) -> datetime | None:
    raw = value.get("timestamp", value.get("date"))
    if raw in {None, ""}:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


class CupHandleDetector:
    """Confirm a daily cup with a bounded sequence of completed five-minute bars."""

    def __init__(self, settings: IntradayMonitorSettings) -> None:
        self.settings = settings.validate()
        self.timezone = ZoneInfo(settings.timezone)

    def _result(
        self,
        *,
        started: float,
        candidate: DailyCandidate,
        session_date: str,
        now: datetime,
        bars: Sequence[dict[str, Any]],
        outcome: str,
        reason: str,
        details: dict[str, Any] | None = None,
        signal: BreakoutSignal | None = None,
    ) -> CupHandleEvaluation:
        return CupHandleEvaluation(
            ticker=candidate.ticker,
            session_date=session_date,
            outcome=outcome,
            rejection_reason=reason,
            evaluated_at=now.astimezone(self.timezone),
            latency_ms=(time.perf_counter() - started) * 1000.0,
            bar_count=len(bars),
            details=details or {},
            signal=signal,
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
    ) -> CupHandleEvaluation:
        started = time.perf_counter()
        bars = list(metrics.get("bars") or [])
        common = {
            "algorithm_version": CUP_HANDLE_ALGORITHM_VERSION,
            "parameter_version": CUP_HANDLE_PARAMETER_VERSION,
            "cup_depth_pct": candidate.cup_depth_pct,
            "cup_width_sessions": candidate.cup_width_sessions,
        }
        if not market_open:
            return self._result(
                started=started, candidate=candidate, session_date=session_date,
                now=now, bars=bars, outcome="NOT_READY", reason="MARKET_CLOSED",
                details=common,
            )
        quote_age = quote.age_seconds(now)
        if (
            quote.timestamp.strftime("%Y-%m-%d") != session_date
            or not -5.0 <= quote_age <= self.settings.stale_after_seconds
        ):
            return self._result(
                started=started, candidate=candidate, session_date=session_date,
                now=now, bars=bars, outcome="NOT_READY", reason="STALE_QUOTE",
                details={**common, "quote_age_seconds": quote_age},
            )
        if not candidate.cup_qualified:
            return self._result(
                started=started, candidate=candidate, session_date=session_date,
                now=now, bars=bars, outcome="REJECTED",
                reason=f"DAILY_{candidate.cup_rejection_reason or 'CUP_NOT_QUALIFIED'}",
                details=common,
            )
        if metrics.get("error") and not bars:
            return self._result(
                started=started, candidate=candidate, session_date=session_date,
                now=now, bars=bars, outcome="NOT_READY",
                reason="NO_COMPLETED_5M_BARS",
                details={**common, "source_status": str(metrics.get("error"))},
            )
        if metrics.get("error"):
            return self._result(
                started=started, candidate=candidate, session_date=session_date,
                now=now, bars=bars, outcome="ERROR", reason="MINUTE_DATA_ERROR",
                details={**common, "error": str(metrics.get("error"))},
            )
        data_quality = metrics.get("data_quality") or {}
        gaps = list(data_quality.get("gaps") or [])
        if gaps:
            classifications = {
                str(value.get("classification") or "UNRESOLVED_SOURCE_GAP")
                for value in gaps
            }
            if "PROVIDER_GAP_CONFIRMED" in classifications:
                reason = "PROVIDER_MINUTE_DATA_GAP"
            elif "UNRESOLVED_SOURCE_GAP" in classifications:
                reason = "UNRESOLVED_5M_SOURCE_GAP"
            else:
                reason = "NO_TRADE_5M_INTERVAL"
            return self._result(
                started=started,
                candidate=candidate,
                session_date=session_date,
                now=now,
                bars=bars,
                outcome="UNEVALUABLE",
                reason=reason,
                details={**common, "data_quality": data_quality},
            )
        if len(bars) > self.settings.cup_max_output_bars:
            return self._result(
                started=started, candidate=candidate, session_date=session_date,
                now=now, bars=bars, outcome="ERROR", reason="UNBOUNDED_BAR_SEQUENCE",
                details=common,
            )
        if len(bars) < (
            self.settings.cup_min_handle_bars
            + self.settings.cup_volume_baseline_bars
            + 1
        ):
            return self._result(
                started=started, candidate=candidate, session_date=session_date,
                now=now, bars=bars, outcome="NOT_READY",
                reason="INSUFFICIENT_COMPLETED_5M_BARS", details=common,
            )

        parsed: list[dict[str, Any]] = []
        for bar in bars:
            timestamp = _bar_timestamp(bar, self.timezone)
            values = {key: _finite(bar.get(key)) for key in ("open", "high", "low", "close", "volume")}
            if timestamp is None or any(values[key] is None for key in ("open", "high", "low", "close")):
                return self._result(
                    started=started, candidate=candidate, session_date=session_date,
                    now=now, bars=bars, outcome="ERROR", reason="INVALID_5M_BAR",
                    details=common,
                )
            parsed.append({"timestamp": timestamp, **values})
        current = parsed[-1]
        if any(
            bar["timestamp"].strftime("%Y-%m-%d") != session_date
            for bar in parsed
        ):
            return self._result(
                started=started, candidate=candidate, session_date=session_date,
                now=now, bars=bars, outcome="ERROR", reason="BAR_SESSION_MISMATCH",
                details=common,
            )
        if any(
            parsed[index]["timestamp"] - parsed[index - 1]["timestamp"]
            != timedelta(minutes=self.settings.cup_intraday_interval_minutes)
            for index in range(1, len(parsed))
        ):
            return self._result(
                started=started, candidate=candidate, session_date=session_date,
                now=now, bars=bars, outcome="ERROR",
                reason="NON_CONTIGUOUS_5M_SEQUENCE", details=common,
            )
        bar_close_at = current["timestamp"] + timedelta(
            minutes=self.settings.cup_intraday_interval_minutes
        )
        max_age = max(
            self.settings.stale_after_seconds,
            self.settings.broad_refresh_minutes * 60 + 30,
        )
        bar_age = (now.astimezone(self.timezone) - bar_close_at).total_seconds()
        if not 0 <= bar_age <= max_age:
            return self._result(
                started=started, candidate=candidate, session_date=session_date,
                now=now, bars=bars, outcome="NOT_READY", reason="STALE_COMPLETED_5M_BAR",
                details={**common, "bar_age_seconds": bar_age},
            )

        rim = max(candidate.cup_left_rim, candidate.cup_right_rim)
        if rim <= 0:
            return self._result(
                started=started, candidate=candidate, session_date=session_date,
                now=now, bars=bars, outcome="ERROR", reason="INVALID_DAILY_CUP_GEOMETRY",
                details=common,
            )
        trigger_level = rim * (1.0 + self.settings.cup_breakout_buffer_bps / 10_000.0)
        pre_breakout = parsed[:-1]
        touch_floor = rim * (1.0 - self.settings.cup_handle_start_tolerance_pct / 100.0)
        eligible_starts = [
            index
            for index, bar in enumerate(pre_breakout)
            if float(bar["high"]) >= touch_floor
            and self.settings.cup_min_handle_bars
            <= len(pre_breakout) - index
            <= self.settings.cup_max_handle_bars
            and index >= self.settings.cup_volume_baseline_bars
        ]
        if not eligible_starts:
            return self._result(
                started=started, candidate=candidate, session_date=session_date,
                now=now, bars=bars, outcome="NOT_READY", reason="HANDLE_NOT_FORMED",
                details={**common, "breakout_level": trigger_level},
            )
        handle_start = eligible_starts[-1]
        handle = pre_breakout[handle_start:]
        baseline = pre_breakout[
            handle_start - self.settings.cup_volume_baseline_bars:handle_start
        ]
        handle_low = min(float(bar["low"]) for bar in handle)
        handle_depth = (rim - handle_low) / rim * 100.0
        handle_to_cup = handle_depth / max(candidate.cup_depth_pct, 1e-12)
        cup_midpoint = candidate.cup_bottom + (rim - candidate.cup_bottom) * 0.5
        baseline_volume = sum(float(bar["volume"] or 0.0) for bar in baseline) / len(baseline)
        handle_volume = sum(float(bar["volume"] or 0.0) for bar in handle) / len(handle)
        handle_volume_ratio = (
            handle_volume / baseline_volume if baseline_volume > 0 else float("inf")
        )
        breakout_volume_ratio = (
            float(current["volume"] or 0.0) / handle_volume
            if handle_volume > 0 else float("inf")
        )
        details = {
            **common,
            "left_rim": candidate.cup_left_rim,
            "right_rim": candidate.cup_right_rim,
            "cup_bottom": candidate.cup_bottom,
            "breakout_level": trigger_level,
            "handle_start": handle[0]["timestamp"].isoformat(timespec="minutes"),
            "handle_low": handle_low,
            "handle_depth_pct": handle_depth,
            "handle_length_bars": len(handle),
            "handle_to_cup_depth_ratio": handle_to_cup,
            "handle_volume_ratio": handle_volume_ratio,
            "breakout_volume_ratio": breakout_volume_ratio,
            "bar_timestamp": current["timestamp"].isoformat(timespec="minutes"),
        }
        reason = ""
        if handle_depth < self.settings.cup_min_handle_depth_pct:
            reason = "HANDLE_TOO_SHALLOW"
        elif handle_depth > self.settings.cup_max_handle_depth_pct:
            reason = "HANDLE_TOO_DEEP"
        elif handle_to_cup > self.settings.cup_max_handle_to_cup_depth_ratio:
            reason = "HANDLE_EXCEEDS_CUP_DEPTH_RATIO"
        elif handle_low < cup_midpoint:
            reason = "HANDLE_BELOW_CUP_MIDPOINT"
        elif handle_volume_ratio > self.settings.cup_max_handle_volume_ratio:
            reason = "HANDLE_VOLUME_NOT_CONTRACTING"
        elif float(pre_breakout[-1]["close"]) >= trigger_level:
            reason = "BREAKOUT_ALREADY_OCCURRED"
        elif float(current["close"]) < trigger_level:
            reason = "RIM_NOT_BROKEN"
        elif breakout_volume_ratio < self.settings.cup_min_breakout_volume_ratio:
            reason = "BREAKOUT_VOLUME_TOO_LOW"
        if reason:
            return self._result(
                started=started, candidate=candidate, session_date=session_date,
                now=now, bars=bars, outcome="REJECTED", reason=reason,
                details=details,
            )

        return20 = (
            (quote.price / candidate.return_reference_close - 1.0) * 100.0
            if quote.price > 0 and candidate.return_reference_close > 0 else 0.0
        )
        current_adr = (
            (quote.day_high / quote.day_low - 1.0) * 100.0
            if quote.day_high > 0 and quote.day_low > 0 else 0.0
        )
        signal = BreakoutSignal(
            session_date=session_date,
            ticker=candidate.ticker,
            signal_type="CUP_HANDLE_BREAKOUT",
            trigger_family=CUP_HANDLE_TRIGGER_FAMILY,
            algorithm_version=CUP_HANDLE_ALGORITHM_VERSION,
            parameter_version=CUP_HANDLE_PARAMETER_VERSION,
            triggered_at=now.astimezone(self.timezone),
            bar_timestamp=current["timestamp"],
            price=float(current["close"]),
            breakout_level=trigger_level,
            opening_range_minutes=None,
            opening_range_high=None,
            vwap=_finite(metrics.get("vwap")),
            relative_volume=_finite(metrics.get("relative_volume")),
            ma10=_finite(metrics.get("ma10")),
            ma20=_finite(metrics.get("ma20")),
            ma50=_finite(metrics.get("ma50")),
            setup_score=max(candidate.setup_score, int(round(candidate.cup_score))),
            adr20_live=(candidate.adr_sum_19 + current_adr) / 20.0,
            return20_live=return20,
            dollar_volume=quote.dollar_volume,
            reasons=(
                "DAILY_CUP_CONFIRMED",
                "FIVE_MINUTE_HANDLE",
                "HANDLE_VOLUME_CONTRACTION",
                "RIM_BREAKOUT",
            ),
            pattern=details,
        )
        return self._result(
            started=started, candidate=candidate, session_date=session_date,
            now=now, bars=bars, outcome="MATCH", reason="MATCH",
            details=details, signal=signal,
        )


__all__ = [
    "CUP_HANDLE_ALGORITHM_VERSION",
    "CUP_HANDLE_PARAMETER_VERSION",
    "CUP_HANDLE_TRIGGER_FAMILY",
    "CupHandleDetector",
    "CupHandleEvaluation",
    "DailyCupSetup",
    "detect_daily_cup",
]
