"""Incremental completed-bar storage and intraday metrics."""
from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


_REQUIRED = ["open", "high", "low", "close", "volume"]
_OPENING_WINDOWS = (1, 5, 30, 60)


class RollingIntradayBars:
    """Merge exact FMP bars and update indicators only for newly completed bars."""

    def __init__(self, ticker: str, *, timezone: str = "America/New_York") -> None:
        self.ticker = str(ticker).upper()
        self.timezone = ZoneInfo(timezone)
        self._frame = pd.DataFrame(columns=_REQUIRED)
        self._derived_interval: int | None = None
        self._derived_end = 0
        self._aggregated: list[dict[str, Any]] = []
        self._sessions: dict[str, dict[str, Any]] = {}
        self._cached_key: tuple[str, int, int, int] | None = None
        self._cached_metrics: dict[str, Any] | None = None
        self._quote_observations: list[dict[str, Any]] = []

    def _reset_derived(self, interval: int) -> None:
        self._derived_interval = interval
        self._derived_end = 0
        self._aggregated = []
        self._sessions = {}
        self._cached_key = None
        self._cached_metrics = None

    def merge(self, frame: pd.DataFrame | None) -> int:
        if frame is None or frame.empty:
            return 0
        if any(column not in frame.columns for column in _REQUIRED):
            raise ValueError("intraday frame is missing required OHLCV columns")
        incoming = frame[_REQUIRED].copy()
        incoming.index = pd.to_datetime(incoming.index, errors="coerce")
        incoming = incoming.loc[~incoming.index.isna()]
        if incoming.index.tz is not None:
            incoming.index = incoming.index.tz_convert(self.timezone).tz_localize(None)
        incoming = incoming[~incoming.index.duplicated(keep="last")].sort_index()
        for column in _REQUIRED:
            incoming[column] = pd.to_numeric(incoming[column], errors="coerce")
        incoming = incoming.dropna(subset=["open", "high", "low", "close"])
        if incoming.empty:
            return 0

        if self._frame.empty:
            self._frame = incoming
            return len(incoming)

        new_rows = incoming.loc[~incoming.index.isin(self._frame.index)]
        if new_rows.empty:
            return 0
        append_only = bool(new_rows.index.min() > self._frame.index.max())
        self._frame = pd.concat([self._frame, new_rows])
        self._frame = self._frame[~self._frame.index.duplicated(keep="last")].sort_index()
        if not append_only:
            self._reset_derived(self._derived_interval or 5)
        self._cached_key = None
        self._cached_metrics = None
        return len(new_rows)

    def observe_quote(
        self,
        *,
        observed_at: datetime,
        provider_timestamp: datetime,
        cumulative_volume: float,
    ) -> None:
        """Retain bounded quote evidence used to classify empty five-minute buckets."""
        observed_value = (
            observed_at
            if observed_at.tzinfo is not None
            else observed_at.replace(tzinfo=self.timezone)
        )
        provider_value = (
            provider_timestamp
            if provider_timestamp.tzinfo is not None
            else provider_timestamp.replace(tzinfo=self.timezone)
        )
        observed = observed_value.astimezone(self.timezone).replace(tzinfo=None)
        provider = provider_value.astimezone(self.timezone).replace(tzinfo=None)
        row = {
            "observed_at": pd.Timestamp(observed),
            "provider_timestamp": pd.Timestamp(provider),
            "cumulative_volume": max(0.0, float(cumulative_volume)),
        }
        minute = row["observed_at"].floor("min")
        self._quote_observations = [
            value
            for value in self._quote_observations
            if value["observed_at"].floor("min") != minute
        ]
        self._quote_observations.append(row)
        self._quote_observations.sort(key=lambda value: value["observed_at"])
        self._quote_observations = self._quote_observations[-600:]
        self._cached_key = None
        self._cached_metrics = None

    def _completed_end(self, now: datetime) -> int:
        aware = now if now.tzinfo is not None else now.replace(tzinfo=self.timezone)
        current_minute = pd.Timestamp(
            aware.astimezone(self.timezone).replace(tzinfo=None)
        ).floor("min")
        return int(self._frame.index.searchsorted(current_minute, side="left"))

    def completed_frame(self, now: datetime) -> pd.DataFrame:
        return self._frame.iloc[:self._completed_end(now)].copy()

    @staticmethod
    def _is_regular(timestamp: pd.Timestamp) -> bool:
        return time(9, 30) <= timestamp.time() < time(16, 0)

    def _process_row(self, timestamp: pd.Timestamp, row: Any, interval: int) -> None:
        if not self._is_regular(timestamp):
            return
        open_price = float(row.open)
        high = float(row.high)
        low = float(row.low)
        close = float(row.close)
        volume = max(0.0, float(row.volume))
        session_date = timestamp.strftime("%Y-%m-%d")
        session = self._sessions.setdefault(session_date, {
            "last_timestamp": timestamp,
            "last_price": close,
            "day_high": high,
            "day_low": low,
            "volume": 0.0,
            "vwap_numerator": 0.0,
            "source_bars": 0,
            "cumulative_volume_by_minute": {},
            "opening": {
                minutes: {
                    "high": float("-inf"),
                    "low": float("inf"),
                    "post_high": float("-inf"),
                }
                for minutes in _OPENING_WINDOWS
            },
        })
        session["last_timestamp"] = timestamp
        session["last_price"] = close
        session["day_high"] = max(float(session["day_high"]), high)
        session["day_low"] = min(float(session["day_low"]), low)
        session["volume"] = float(session["volume"]) + volume
        session["vwap_numerator"] = (
            float(session["vwap_numerator"])
            + ((high + low + close) / 3.0) * volume
        )
        session["source_bars"] = int(session["source_bars"]) + 1
        minute_offset = int(
            (timestamp - (
                timestamp.normalize() + pd.Timedelta(hours=9, minutes=30)
            )).total_seconds() // 60
        )
        session["cumulative_volume_by_minute"][minute_offset] = float(
            session["volume"]
        )

        start = timestamp.normalize() + pd.Timedelta(hours=9, minutes=30)
        for minutes, opening in session["opening"].items():
            range_end = start + pd.Timedelta(minutes=minutes)
            if timestamp < range_end:
                opening["high"] = max(float(opening["high"]), high)
                opening["low"] = min(float(opening["low"]), low)
            else:
                opening["post_high"] = max(float(opening["post_high"]), high)

        bucket = timestamp.floor(f"{interval}min")
        if self._aggregated and self._aggregated[-1]["timestamp"] == bucket:
            aggregate = self._aggregated[-1]
            aggregate["high"] = max(float(aggregate["high"]), high)
            aggregate["low"] = min(float(aggregate["low"]), low)
            aggregate["close"] = close
            aggregate["volume"] = float(aggregate["volume"]) + volume
            aggregate["source_count"] = int(aggregate["source_count"]) + 1
        else:
            self._aggregated.append({
                "timestamp": bucket,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "source_count": 1,
            })

    def _advance(self, completed_end: int, interval: int) -> None:
        if self._derived_interval != interval or completed_end < self._derived_end:
            self._reset_derived(interval)
        if completed_end <= self._derived_end:
            return
        chunk = self._frame.iloc[self._derived_end:completed_end]
        for row in chunk.itertuples():
            self._process_row(pd.Timestamp(row.Index), row, interval)
        self._derived_end = completed_end

    def metrics(
        self,
        *,
        now: datetime,
        session_date: str,
        interval: int = 5,
        max_bars: int = 96,
    ) -> dict[str, Any]:
        interval = max(1, int(interval))
        completed_end = self._completed_end(now)
        key = (session_date, interval, completed_end, max(1, int(max_bars)))
        if self._cached_key == key and self._cached_metrics is not None:
            return dict(self._cached_metrics)
        self._advance(completed_end, interval)
        session = self._sessions.get(session_date)
        if session is None:
            return {
                "session_date": None,
                "bars": [],
                "opening_ranges": {},
                "error": "没有常规交易时段分钟数据",
            }

        last_timestamp = pd.Timestamp(session["last_timestamp"])
        start = last_timestamp.normalize() + pd.Timedelta(hours=9, minutes=30)
        opening_ranges: dict[str, dict[str, Any]] = {}
        for minutes, opening in session["opening"].items():
            formed_at = start + pd.Timedelta(minutes=minutes - 1)
            high = float(opening["high"])
            low = float(opening["low"])
            if (
                last_timestamp < formed_at
                or high == float("-inf")
                or low == float("inf")
            ):
                continue
            post_high = float(opening["post_high"])
            opening_ranges[str(minutes)] = {
                "high": high,
                "low": low,
                "triggered": (
                    post_high != float("-inf") and post_high >= high
                ),
                "current_above": float(session["last_price"]) >= high,
            }

        closes = [float(row["close"]) for row in self._aggregated]

        def moving_average(window: int) -> float | None:
            if len(closes) < window:
                return None
            return sum(closes[-window:]) / window

        total_volume = float(session["volume"])
        current_offset = int(
            (last_timestamp - start).total_seconds() // 60
        )
        historical_cumulative: list[float] = []
        for prior_date in sorted(
            value for value in self._sessions if value < session_date
        )[-5:]:
            profile = self._sessions[prior_date]["cumulative_volume_by_minute"]
            eligible_offsets = [
                offset for offset in profile if offset <= current_offset
            ]
            if eligible_offsets:
                historical_cumulative.append(
                    float(profile[max(eligible_offsets)])
                )
        historical_mean = (
            sum(historical_cumulative) / len(historical_cumulative)
            if historical_cumulative
            else 0.0
        )
        bounded_bars = self.bounded_ohlcv(
            now=now,
            session_date=session_date,
            interval=interval,
            max_bars=max_bars,
        )
        data_quality = self._sequence_data_quality(
            bounded_bars,
            session_date=session_date,
            interval=interval,
        )
        metrics = {
            "session_date": session_date,
            "interval": interval,
            "last_timestamp": last_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "last_price": float(session["last_price"]),
            "day_low": float(session["day_low"]),
            "day_high": float(session["day_high"]),
            "opening_ranges": opening_ranges,
            "bars": bounded_bars,
            "data_quality": data_quality,
            "vwap": (
                float(session["vwap_numerator"]) / total_volume
                if total_volume > 0
                else None
            ),
            "relative_volume": (
                total_volume / historical_mean
                if historical_mean > 0
                else None
            ),
            "completed_source_bars": int(session["source_bars"]),
            "ma10": moving_average(10),
            "ma20": moving_average(20),
            "ma50": moving_average(50),
            "error": None,
        }
        self._cached_key = key
        self._cached_metrics = dict(metrics)
        return metrics

    def _classify_empty_bucket(
        self,
        *,
        bucket_start: pd.Timestamp,
        interval: int,
    ) -> tuple[str, dict[str, Any]]:
        bucket_end = bucket_start + pd.Timedelta(minutes=interval)
        session_date = bucket_start.strftime("%Y-%m-%d")
        observations = [
            value
            for value in self._quote_observations
            if value["observed_at"].strftime("%Y-%m-%d") == session_date
        ]
        before = [
            value for value in observations
            if value["observed_at"] <= bucket_start
        ]
        after = [
            value for value in observations
            if value["observed_at"] >= bucket_end
        ]
        evidence: dict[str, Any] = {
            "bucket_start": bucket_start.strftime("%Y-%m-%d %H:%M:%S"),
            "bucket_end": bucket_end.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if not before or not after:
            return "UNRESOLVED_SOURCE_GAP", evidence
        left = before[-1]
        right = after[0]
        volume_delta = (
            float(right["cumulative_volume"])
            - float(left["cumulative_volume"])
        )
        provider_timestamp = pd.Timestamp(right["provider_timestamp"])
        evidence.update({
            "before_observed_at": left["observed_at"].strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "after_observed_at": right["observed_at"].strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "after_provider_timestamp": provider_timestamp.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "cumulative_volume_delta": volume_delta,
        })
        if volume_delta == 0:
            return "NO_TRADE_CONFIRMED", evidence
        if (
            volume_delta > 0
            and bucket_start <= provider_timestamp < bucket_end
        ):
            return "PROVIDER_GAP_CONFIRMED", evidence
        return "UNRESOLVED_SOURCE_GAP", evidence

    def _sequence_data_quality(
        self,
        bars: list[dict[str, Any]],
        *,
        session_date: str,
        interval: int,
    ) -> dict[str, Any]:
        gaps: list[dict[str, Any]] = []
        timestamps = [
            pd.Timestamp(value["timestamp"])
            for value in bars
            if value.get("timestamp")
        ]
        for previous, current in zip(timestamps, timestamps[1:]):
            missing = previous + pd.Timedelta(minutes=interval)
            while missing < current:
                classification, evidence = self._classify_empty_bucket(
                    bucket_start=missing,
                    interval=interval,
                )
                gaps.append({
                    "gap_start": missing.strftime("%Y-%m-%d %H:%M:%S"),
                    "gap_end": (
                        missing + pd.Timedelta(minutes=interval)
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                    "classification": classification,
                    "evidence": evidence,
                })
                missing += pd.Timedelta(minutes=interval)
        classification_counts: dict[str, int] = {}
        for gap in gaps:
            classification = str(gap["classification"])
            classification_counts[classification] = (
                classification_counts.get(classification, 0) + 1
            )
        source_counts = [int(value.get("source_minute_count") or 0) for value in bars]
        source_slots = len(source_counts) * interval
        return {
            "status": "UNEVALUABLE" if gaps else "PASS",
            "session_date": session_date,
            "interval_minutes": interval,
            "gap_count": len(gaps),
            "classification_counts": classification_counts,
            "partial_bucket_count": sum(
                0 < count < interval for count in source_counts
            ),
            "source_minute_coverage": (
                round(sum(source_counts) / source_slots, 6)
                if source_slots else 0.0
            ),
            "gaps": gaps,
        }

    def bounded_ohlcv(
        self,
        *,
        now: datetime,
        session_date: str,
        interval: int = 5,
        max_bars: int = 96,
    ) -> list[dict[str, Any]]:
        """Return only complete regular-session aggregates with a hard row cap."""
        interval = max(1, int(interval))
        limit = max(1, int(max_bars))
        completed_end = self._completed_end(now)
        self._advance(completed_end, interval)
        aware = now if now.tzinfo is not None else now.replace(tzinfo=self.timezone)
        current_minute = pd.Timestamp(
            aware.astimezone(self.timezone).replace(tzinfo=None)
        ).floor("min")
        eligible = [
            row
            for row in self._aggregated
            if pd.Timestamp(row["timestamp"]).strftime("%Y-%m-%d") == session_date
            and int(row.get("source_count") or 0) > 0
            and pd.Timestamp(row["timestamp"]) + pd.Timedelta(minutes=interval)
            <= current_minute
        ][-limit:]
        return [
            {
                "timestamp": pd.Timestamp(row["timestamp"]).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "source_minute_count": int(row.get("source_count") or 0),
                "source_minute_coverage": round(
                    int(row.get("source_count") or 0) / interval,
                    6,
                ),
            }
            for row in eligible
        ]

    def latest_completed_timestamp(self, now: datetime) -> datetime | None:
        end = self._completed_end(now)
        if end <= 0:
            return None
        value = pd.Timestamp(self._frame.index[end - 1]).to_pydatetime()
        return value.replace(tzinfo=self.timezone)

    @property
    def stored_bars(self) -> int:
        return len(self._frame)
