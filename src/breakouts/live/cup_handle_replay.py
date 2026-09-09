"""Deterministic historical replay for the cup-handle detector."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from src.breakouts.live.cup_handle import (
    CUP_HANDLE_ALGORITHM_VERSION,
    CUP_HANDLE_PARAMETER_VERSION,
    CupHandleDetector,
    detect_daily_cup,
)
from src.breakouts.live.models import DailyCandidate, QuoteSnapshot
from src.breakouts.live.settings import IntradayMonitorSettings


_REQUIRED = ["open", "high", "low", "close", "volume"]


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty or any(column not in frame for column in _REQUIRED):
        return pd.DataFrame(columns=_REQUIRED)
    data = frame[_REQUIRED].copy()
    data.index = pd.to_datetime(data.index, errors="coerce")
    data = data.loc[~data.index.isna()]
    if data.index.tz is not None:
        data.index = data.index.tz_convert("America/New_York").tz_localize(None)
    for column in _REQUIRED:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["open", "high", "low", "close"])
    data = data.loc[
        (data["high"] > 0)
        & (data["low"] > 0)
        & (data["close"] > 0)
        & (data["high"] >= data["low"])
    ]
    return data.loc[~data.index.duplicated(keep="last")].sort_index()


def _complete_five_minute_bars(frame: pd.DataFrame) -> pd.DataFrame:
    data = _normalize(frame)
    if data.empty:
        return data
    regular = data.between_time("09:30", "15:59")
    pieces: list[pd.DataFrame] = []
    for _, session in regular.groupby(regular.index.date, sort=True):
        if session.empty:
            continue
        bars = session.resample(
            "5min",
            origin=pd.Timestamp(session.index[0]).normalize()
            + pd.Timedelta(hours=9, minutes=30),
            label="left",
            closed="left",
        ).agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        })
        counts = session["close"].resample(
            "5min",
            origin=pd.Timestamp(session.index[0]).normalize()
            + pd.Timedelta(hours=9, minutes=30),
            label="left",
            closed="left",
        ).count()
        bars = bars.loc[counts >= 5].dropna(subset=["open", "high", "low", "close"])
        pieces.append(bars)
    return pd.concat(pieces).sort_index() if pieces else pd.DataFrame(columns=_REQUIRED)


def _records(frame: pd.DataFrame, limit: int) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": pd.Timestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S"),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume),
        }
        for timestamp, row in frame.tail(limit).iterrows()
    ]


def _candidate(
    ticker: str,
    daily: pd.DataFrame,
    setup: Any,
    *,
    source_session: str,
) -> DailyCandidate:
    data = _normalize(daily)
    high = data["high"].astype(float)
    low = data["low"].astype(float)
    close = data["close"].astype(float)
    volume = data["volume"].fillna(0.0).clip(lower=0.0).astype(float)
    adr = (high / low - 1.0) * 100.0
    adv = (close * volume).tail(20).mean()
    reference = close.iloc[-20] if len(close) >= 20 else close.iloc[0]
    return DailyCandidate(
        ticker=ticker,
        name=ticker,
        sector="",
        setup_score=int(round(setup.score)),
        daily_pivot=float(high.tail(20).max()),
        previous_high=float(high.iloc[-1]),
        adr20=float(adr.tail(20).mean()),
        avg_dollar_volume20=float(adv),
        source_data_date=source_session,
        setup_qualified=True,
        daily_status="READY",
        return_reference_close=float(reference),
        adr_sum_19=float(adr.tail(19).sum()),
        cup_qualified=bool(setup.qualified),
        cup_rejection_reason=str(setup.rejection_reason),
        cup_left_rim_date=setup.left_rim_date,
        cup_right_rim_date=setup.right_rim_date,
        cup_bottom_date=setup.bottom_date,
        cup_left_rim=float(setup.left_rim),
        cup_right_rim=float(setup.right_rim),
        cup_bottom=float(setup.bottom),
        cup_depth_pct=float(setup.depth_pct),
        cup_width_sessions=int(setup.width_sessions),
        cup_rim_tolerance_pct=float(setup.rim_tolerance_pct),
        cup_volume_contraction_ratio=float(setup.volume_contraction_ratio),
        cup_score=float(setup.score),
    )


def replay_cup_handle(
    daily_frames: Mapping[str, pd.DataFrame],
    minute_frames: Mapping[str, pd.DataFrame],
    *,
    settings: IntradayMonitorSettings,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Replay completed bars and report a documented short-horizon false-positive proxy."""
    detector = CupHandleDetector(settings)
    timezone = ZoneInfo(settings.timezone)
    signals: list[dict[str, Any]] = []
    rejection_counts: Counter[str] = Counter()
    daily_rejections: Counter[str] = Counter()
    evaluated_sessions = 0
    evaluated_bars = 0
    for raw_ticker in sorted(set(daily_frames) & set(minute_frames)):
        ticker = str(raw_ticker).upper()
        daily = _normalize(daily_frames[raw_ticker])
        five = _complete_five_minute_bars(minute_frames[raw_ticker])
        if daily.empty or five.empty:
            continue
        session_dates = sorted(set(five.index.strftime("%Y-%m-%d")))
        for session_date in session_dates:
            if start and session_date < start:
                continue
            if end and session_date > end:
                continue
            prior = daily.loc[daily.index.normalize() < pd.Timestamp(session_date)]
            source_session = (
                pd.Timestamp(prior.index[-1]).strftime("%Y-%m-%d")
                if not prior.empty else ""
            )
            setup = detect_daily_cup(
                prior,
                settings=settings,
                asof=source_session or None,
            )
            evaluated_sessions += 1
            if not setup.qualified:
                daily_rejections[setup.rejection_reason] += 1
                continue
            candidate = _candidate(
                ticker,
                prior,
                setup,
                source_session=source_session,
            )
            session = five.loc[five.index.strftime("%Y-%m-%d") == session_date]
            cumulative_volume = 0.0
            for index in range(len(session)):
                visible = session.iloc[:index + 1]
                current = visible.iloc[-1]
                cumulative_volume += float(current["volume"])
                timestamp = pd.Timestamp(visible.index[-1]).to_pydatetime().replace(
                    tzinfo=timezone
                )
                quote = QuoteSnapshot(
                    ticker=ticker,
                    timestamp=timestamp + timedelta(minutes=5),
                    price=float(current["close"]),
                    cumulative_volume=cumulative_volume,
                    day_high=float(visible["high"].max()),
                    day_low=float(visible["low"].min()),
                    open=float(visible["open"].iloc[0]),
                    previous_close=float(prior["close"].iloc[-1]),
                    change_percentage=(
                        float(current["close"] / prior["close"].iloc[-1] - 1.0) * 100.0
                    ),
                )
                now = timestamp + timedelta(minutes=5, seconds=1)
                evaluation = detector.evaluate(
                    candidate,
                    quote,
                    {
                        "bars": _records(visible, settings.cup_max_output_bars),
                        "last_timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                        "last_price": float(current["close"]),
                        "error": None,
                    },
                    now=now,
                    session_date=session_date,
                    market_open=True,
                )
                evaluated_bars += 1
                if evaluation.outcome != "MATCH":
                    rejection_counts[evaluation.rejection_reason] += 1
                    continue
                signal = evaluation.signal
                assert signal is not None
                future = session.iloc[
                    index + 1:index + 1 + settings.cup_replay_confirmation_horizon_bars
                ]
                target = signal.price * (
                    1.0 + settings.cup_replay_confirmation_return_pct / 100.0
                )
                stop = float((signal.pattern or {}).get("handle_low") or 0.0)
                outcome = "UNRESOLVED"
                for _, bar in future.iterrows():
                    if stop > 0 and float(bar["low"]) <= stop:
                        outcome = "FALSE_POSITIVE_PROXY"
                        break
                    if float(bar["high"]) >= target:
                        outcome = "CONFIRMED_PROXY"
                        break
                if (
                    outcome == "UNRESOLVED"
                    and len(future) >= settings.cup_replay_confirmation_horizon_bars
                ):
                    outcome = "FALSE_POSITIVE_PROXY"
                signals.append({
                    **signal.to_dict(),
                    "replay_outcome": outcome,
                    "confirmation_target": target,
                    "confirmation_horizon_bars": (
                        settings.cup_replay_confirmation_horizon_bars
                    ),
                })
                break

    outcome_counts = Counter(signal["replay_outcome"] for signal in signals)
    resolved = (
        outcome_counts["CONFIRMED_PROXY"]
        + outcome_counts["FALSE_POSITIVE_PROXY"]
    )
    return {
        "algorithm_version": CUP_HANDLE_ALGORITHM_VERSION,
        "parameter_version": CUP_HANDLE_PARAMETER_VERSION,
        "generated_at": datetime.now(timezone).isoformat(timespec="seconds"),
        "start": start,
        "end": end,
        "ticker_count": len(set(daily_frames) & set(minute_frames)),
        "evaluated_sessions": evaluated_sessions,
        "evaluated_completed_5m_bars": evaluated_bars,
        "signal_count": len(signals),
        "outcome_counts": dict(sorted(outcome_counts.items())),
        "false_positive_rate_proxy": (
            outcome_counts["FALSE_POSITIVE_PROXY"] / resolved
            if resolved else None
        ),
        "daily_rejection_counts": dict(daily_rejections.most_common()),
        "intraday_rejection_counts": dict(rejection_counts.most_common()),
        "proxy_definition": (
            "A signal is confirmed when price reaches the configured return target "
            "before the handle low within the configured completed-5m horizon; "
            "otherwise a fully observed horizon is a false-positive proxy."
        ),
        "signals": signals,
    }


__all__ = ["replay_cup_handle"]
