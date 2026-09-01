from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

import pandas as pd

from src.breakouts.live.cup_handle import (
    CUP_HANDLE_ALGORITHM_VERSION,
    CUP_HANDLE_PARAMETER_VERSION,
    CUP_HANDLE_TRIGGER_FAMILY,
    CupHandleDetector,
    detect_daily_cup,
)
from src.breakouts.live.cup_handle_replay import replay_cup_handle
from src.breakouts.live.delivery import build_signal_discord_payload
from src.breakouts.live.models import DailyCandidate, QuoteSnapshot
from src.breakouts.live.rolling import RollingIntradayBars
from src.breakouts.live.settings import IntradayMonitorSettings
from src.breakouts.live.state import IntradayMonitorState


NEW_YORK = ZoneInfo("America/New_York")


def _daily_cup() -> pd.DataFrame:
    index = pd.bdate_range("2026-01-01", periods=70)
    prices: list[float] = []
    for offset in range(70):
        if offset < 10:
            prices.append(90.0 + offset)
        elif offset <= 35:
            prices.append(100.0 - (offset - 10) * 0.8)
        elif offset <= 60:
            prices.append(80.0 + (offset - 35) * 0.8)
        else:
            prices.append(99.0 - (offset - 60) * 0.1)
    return pd.DataFrame({
        "open": prices,
        "high": [value * 1.005 for value in prices],
        "low": [value * 0.995 for value in prices],
        "close": prices,
        "volume": [2_000_000.0 - offset * 15_000.0 for offset in range(70)],
    }, index=index)


def _candidate(settings: IntradayMonitorSettings) -> DailyCandidate:
    daily = _daily_cup()
    setup = detect_daily_cup(
        daily,
        settings=settings,
        asof=pd.Timestamp(daily.index[-1]).strftime("%Y-%m-%d"),
    )
    return DailyCandidate(
        ticker="TEST",
        name="Test Inc.",
        sector="Technology",
        setup_score=80,
        daily_pivot=101.0,
        previous_high=99.0,
        adr20=6.0,
        avg_dollar_volume20=20_000_000.0,
        source_data_date=pd.Timestamp(daily.index[-1]).strftime("%Y-%m-%d"),
        setup_qualified=True,
        daily_status="READY",
        return_reference_close=90.0,
        adr_sum_19=100.0,
        cup_qualified=setup.qualified,
        cup_rejection_reason=setup.rejection_reason,
        cup_left_rim_date=setup.left_rim_date,
        cup_right_rim_date=setup.right_rim_date,
        cup_bottom_date=setup.bottom_date,
        cup_left_rim=setup.left_rim,
        cup_right_rim=setup.right_rim,
        cup_bottom=setup.bottom,
        cup_depth_pct=setup.depth_pct,
        cup_width_sessions=setup.width_sessions,
        cup_rim_tolerance_pct=setup.rim_tolerance_pct,
        cup_volume_contraction_ratio=setup.volume_contraction_ratio,
        cup_score=setup.score,
    )


def _handle_bars(candidate: DailyCandidate, *, high_handle_volume: bool = False):
    rim = max(candidate.cup_left_rim, candidate.cup_right_rim)
    closes = [rim * 0.97] * 6 + [
        rim * 0.995,
        rim * 0.98,
        rim * 0.96,
        rim * 0.95,
        rim * 0.97,
        rim * 0.985,
        rim * 1.003,
    ]
    handle_volume = 140.0 if high_handle_volume else 40.0
    volumes = [100.0] * 6 + [
        handle_volume,
        handle_volume,
        handle_volume,
        handle_volume,
        handle_volume,
        handle_volume,
        200.0,
    ]
    rows = []
    start = datetime(2026, 4, 9, 9, 30, tzinfo=NEW_YORK)
    for offset, (close, volume) in enumerate(zip(closes, volumes, strict=True)):
        timestamp = start + timedelta(minutes=5 * offset)
        rows.append({
            "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "open": close,
            "high": close * 1.001,
            "low": close * 0.999,
            "close": close,
            "volume": volume,
        })
    return rows


def _quote(bars: list[dict]) -> QuoteSnapshot:
    now = datetime(2026, 4, 9, 10, 35, 1, tzinfo=NEW_YORK)
    return QuoteSnapshot(
        ticker="TEST",
        timestamp=now,
        price=float(bars[-1]["close"]),
        cumulative_volume=sum(float(row["volume"]) for row in bars),
        day_high=max(float(row["high"]) for row in bars),
        day_low=min(float(row["low"]) for row in bars),
        open=float(bars[0]["open"]),
        previous_close=99.0,
        change_percentage=1.0,
    )


class CupHandleAlgorithmTests(unittest.TestCase):
    def test_daily_cup_geometry_is_deterministic(self):
        settings = IntradayMonitorSettings()
        daily = _daily_cup()

        first = detect_daily_cup(daily, settings=settings)
        second = detect_daily_cup(daily.copy(), settings=settings)

        self.assertEqual(first, second)
        self.assertTrue(first.qualified)
        self.assertEqual(first.width_sessions, 51)
        self.assertAlmostEqual(first.depth_pct, 20.796, places=3)
        self.assertEqual(first.bottom_position, 0.5)
        self.assertLess(first.volume_contraction_ratio, 1.0)

    def test_rolling_output_excludes_partial_bucket_and_honors_cap(self):
        index = pd.date_range("2026-04-09 09:30", periods=13, freq="min")
        frame = pd.DataFrame({
            "open": 10.0,
            "high": 10.2,
            "low": 9.8,
            "close": 10.1,
            "volume": 100.0,
        }, index=index)
        rolling = RollingIntradayBars("TEST")
        rolling.merge(frame)
        now = datetime(2026, 4, 9, 9, 43, tzinfo=NEW_YORK)

        bars = rolling.bounded_ohlcv(
            now=now,
            session_date="2026-04-09",
            interval=5,
            max_bars=96,
        )
        capped = rolling.bounded_ohlcv(
            now=now,
            session_date="2026-04-09",
            interval=5,
            max_bars=1,
        )

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[-1]["timestamp"], "2026-04-09 09:35:00")
        self.assertEqual(capped, bars[-1:])

    def test_partial_source_bucket_uses_only_observed_trades(self):
        index = pd.to_datetime([
            "2026-04-09 09:30",
            "2026-04-09 09:31",
            "2026-04-09 09:33",
            "2026-04-09 09:34",
            "2026-04-09 09:35",
            "2026-04-09 09:38",
        ])
        frame = pd.DataFrame({
            "open": [10.0] * len(index),
            "high": [10.2] * len(index),
            "low": [9.8] * len(index),
            "close": [10.1] * len(index),
            "volume": [100.0] * len(index),
        }, index=index)
        rolling = RollingIntradayBars("TEST")
        rolling.merge(frame)

        metrics = rolling.metrics(
            now=datetime(2026, 4, 9, 9, 40, 1, tzinfo=NEW_YORK),
            session_date="2026-04-09",
            interval=5,
        )

        self.assertEqual(len(metrics["bars"]), 2)
        self.assertEqual(
            [row["source_minute_count"] for row in metrics["bars"]],
            [4, 2],
        )
        self.assertEqual(metrics["data_quality"]["status"], "PASS")
        self.assertEqual(metrics["data_quality"]["partial_bucket_count"], 2)
        self.assertEqual(metrics["data_quality"]["gap_count"], 0)

    def test_empty_bucket_is_classified_from_quote_evidence(self):
        index = pd.to_datetime([
            *pd.date_range("2026-04-09 09:30", periods=5, freq="min"),
            *pd.date_range("2026-04-09 09:40", periods=5, freq="min"),
        ])
        frame = pd.DataFrame({
            "open": [10.0] * len(index),
            "high": [10.2] * len(index),
            "low": [9.8] * len(index),
            "close": [10.1] * len(index),
            "volume": [100.0] * len(index),
        }, index=index)
        rolling = RollingIntradayBars("TEST")
        rolling.merge(frame)
        rolling.observe_quote(
            observed_at=datetime(2026, 4, 9, 9, 35, tzinfo=NEW_YORK),
            provider_timestamp=datetime(2026, 4, 9, 9, 34, tzinfo=NEW_YORK),
            cumulative_volume=500.0,
        )
        rolling.observe_quote(
            observed_at=datetime(2026, 4, 9, 9, 40, tzinfo=NEW_YORK),
            provider_timestamp=datetime(2026, 4, 9, 9, 34, tzinfo=NEW_YORK),
            cumulative_volume=500.0,
        )

        metrics = rolling.metrics(
            now=datetime(2026, 4, 9, 9, 45, 1, tzinfo=NEW_YORK),
            session_date="2026-04-09",
            interval=5,
        )
        quality = metrics["data_quality"]

        self.assertEqual(quality["status"], "UNEVALUABLE")
        self.assertEqual(quality["gap_count"], 1)
        self.assertEqual(
            quality["gaps"][0]["classification"],
            "NO_TRADE_CONFIRMED",
        )

        evaluation = CupHandleDetector(IntradayMonitorSettings()).evaluate(
            _candidate(IntradayMonitorSettings()),
            QuoteSnapshot(
                ticker="TEST",
                timestamp=datetime(2026, 4, 9, 9, 44, tzinfo=NEW_YORK),
                price=10.1,
                cumulative_volume=1000.0,
                day_high=10.2,
                day_low=9.8,
                open=10.0,
                previous_close=9.9,
                change_percentage=2.0,
            ),
            metrics,
            now=datetime(2026, 4, 9, 9, 45, 1, tzinfo=NEW_YORK),
            session_date="2026-04-09",
            market_open=True,
        )
        self.assertEqual(evaluation.outcome, "UNEVALUABLE")
        self.assertEqual(evaluation.rejection_reason, "NO_TRADE_5M_INTERVAL")

        provider_gap = RollingIntradayBars("TEST")
        provider_gap.merge(frame)
        provider_gap.observe_quote(
            observed_at=datetime(2026, 4, 9, 9, 35, tzinfo=NEW_YORK),
            provider_timestamp=datetime(2026, 4, 9, 9, 34, tzinfo=NEW_YORK),
            cumulative_volume=500.0,
        )
        provider_gap.observe_quote(
            observed_at=datetime(2026, 4, 9, 9, 40, tzinfo=NEW_YORK),
            provider_timestamp=datetime(2026, 4, 9, 9, 37, tzinfo=NEW_YORK),
            cumulative_volume=600.0,
        )
        provider_metrics = provider_gap.metrics(
            now=datetime(2026, 4, 9, 9, 45, 1, tzinfo=NEW_YORK),
            session_date="2026-04-09",
            interval=5,
        )
        self.assertEqual(
            provider_metrics["data_quality"]["gaps"][0]["classification"],
            "PROVIDER_GAP_CONFIRMED",
        )

    def test_completed_handle_and_volume_breakout_match(self):
        settings = IntradayMonitorSettings()
        candidate = _candidate(settings)
        bars = _handle_bars(candidate)
        now = datetime(2026, 4, 9, 10, 35, 1, tzinfo=NEW_YORK)

        evaluation = CupHandleDetector(settings).evaluate(
            candidate,
            _quote(bars),
            {"bars": bars, "error": None},
            now=now,
            session_date="2026-04-09",
            market_open=True,
        )

        self.assertEqual(evaluation.outcome, "MATCH")
        self.assertEqual(evaluation.rejection_reason, "MATCH")
        self.assertEqual(evaluation.signal.trigger_family, CUP_HANDLE_TRIGGER_FAMILY)
        self.assertEqual(
            evaluation.signal.algorithm_version,
            CUP_HANDLE_ALGORITHM_VERSION,
        )
        self.assertLess(evaluation.details["handle_volume_ratio"], 0.85)

    def test_non_contracting_handle_is_rejected_with_reason(self):
        settings = IntradayMonitorSettings()
        candidate = _candidate(settings)
        bars = _handle_bars(candidate, high_handle_volume=True)

        evaluation = CupHandleDetector(settings).evaluate(
            candidate,
            _quote(bars),
            {"bars": bars, "error": None},
            now=datetime(2026, 4, 9, 10, 35, 1, tzinfo=NEW_YORK),
            session_date="2026-04-09",
            market_open=True,
        )

        self.assertEqual(evaluation.outcome, "REJECTED")
        self.assertEqual(evaluation.rejection_reason, "HANDLE_VOLUME_NOT_CONTRACTING")

    def test_opening_wait_without_completed_bar_is_not_an_error(self):
        settings = IntradayMonitorSettings()
        candidate = _candidate(settings)
        quote = QuoteSnapshot(
            ticker="TEST",
            timestamp=datetime(2026, 4, 9, 9, 30, 1, tzinfo=NEW_YORK),
            price=99.0,
            cumulative_volume=100.0,
            day_high=99.0,
            day_low=99.0,
            open=99.0,
            previous_close=98.0,
            change_percentage=1.0,
        )

        evaluation = CupHandleDetector(settings).evaluate(
            candidate,
            quote,
            {"bars": [], "error": "no regular-session rows"},
            now=datetime(2026, 4, 9, 9, 30, 1, tzinfo=NEW_YORK),
            session_date="2026-04-09",
            market_open=True,
        )

        self.assertEqual(evaluation.outcome, "NOT_READY")
        self.assertEqual(evaluation.rejection_reason, "NO_COMPLETED_5M_BARS")

    def test_sqlite_signal_outbox_and_independent_shadow_gate(self):
        settings = IntradayMonitorSettings()
        candidate = _candidate(settings)
        bars = _handle_bars(candidate)
        evaluation = CupHandleDetector(settings).evaluate(
            candidate,
            _quote(bars),
            {"bars": bars, "error": None},
            now=datetime(2026, 4, 9, 10, 35, 1, tzinfo=NEW_YORK),
            session_date="2026-04-09",
            market_open=True,
        )
        signal = evaluation.signal
        self.assertIsNotNone(signal)
        sessions = [f"2026-04-{day:02d}" for day in range(6, 11)]
        with tempfile.TemporaryDirectory() as temporary:
            state = IntradayMonitorState(Path(temporary) / "state.sqlite3")
            self.assertTrue(state.record_signal(signal))
            self.assertTrue(state.stage_signal_delivery(
                signal,
                build_signal_discord_payload(signal),
                shadow=True,
            ))
            self.assertEqual(state.status()["outbox_counts"], {"SHADOW": 1})
            for session_date in sessions:
                for minute in range(3):
                    state.record_cup_handle_cycle({
                        "session_date": session_date,
                        "observed_at": f"{session_date}T14:{30 + minute}:08+00:00",
                        "algorithm_version": CUP_HANDLE_ALGORITHM_VERSION,
                        "parameter_version": CUP_HANDLE_PARAMETER_VERSION,
                        "daily_evaluated_count": 500,
                        "daily_candidate_count": 10,
                        "data_contract_complete": True,
                    }, [{
                        "ticker": "TEST",
                        "outcome": "REJECTED",
                        "rejection_reason": "RIM_NOT_BROKEN",
                        "evaluated_at": f"{session_date}T14:{30 + minute}:08+00:00",
                        "latency_ms": 1.2,
                        "bar_count": 10,
                        "details": {},
                        "signal": None,
                    }])
                summary = state.finalize_cup_handle_observation(
                    session_date=session_date,
                    algorithm_version=CUP_HANDLE_ALGORITHM_VERSION,
                    parameter_version=CUP_HANDLE_PARAMETER_VERSION,
                    expected_open_cycles=3,
                    min_cycle_coverage=0.85,
                    max_error_cycle_ratio=0.05,
                    max_detection_p95_ms=250.0,
                    min_evaluable_ticker_coverage=0.95,
                    max_gap_ticker_ratio=0.05,
                    max_bar_count=96,
                )
                self.assertEqual(summary["status"], "PASS")

            legacy = state.promotion_status(sessions)
            cup = state.cup_handle_promotion_status(
                sessions,
                algorithm_version=CUP_HANDLE_ALGORITHM_VERSION,
            )
            self.assertFalse(legacy["eligible"])
            self.assertTrue(cup["eligible"])
            self.assertEqual(cup["passed_sessions"], 5)

    def test_gap_event_is_deduplicated_and_fails_coverage_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = IntradayMonitorState(Path(temporary) / "state.sqlite3")
            for minute in range(2):
                rows = []
                for index in range(10):
                    ticker = f"T{index}"
                    gap = ({
                        "gap_start": "2026-04-09 10:00:00",
                        "gap_end": "2026-04-09 10:05:00",
                        "classification": "PROVIDER_GAP_CONFIRMED",
                        "evidence": {"cumulative_volume_delta": 100.0},
                    } if ticker == "T0" else None)
                    rows.append({
                        "ticker": ticker,
                        "outcome": "UNEVALUABLE" if gap else "REJECTED",
                        "rejection_reason": (
                            "PROVIDER_MINUTE_DATA_GAP" if gap else "RIM_NOT_BROKEN"
                        ),
                        "evaluated_at": (
                            f"2026-04-09T14:{30 + minute}:08+00:00"
                        ),
                        "latency_ms": 1.0,
                        "bar_count": 10,
                        "details": {
                            "data_quality": {"gaps": [gap]}
                        } if gap else {},
                        "signal": None,
                    })
                state.record_cup_handle_cycle({
                    "session_date": "2026-04-09",
                    "observed_at": f"2026-04-09T14:{30 + minute}:08+00:00",
                    "algorithm_version": CUP_HANDLE_ALGORITHM_VERSION,
                    "parameter_version": CUP_HANDLE_PARAMETER_VERSION,
                    "daily_evaluated_count": 500,
                    "daily_candidate_count": 10,
                    "data_contract_complete": True,
                }, rows)

            summary = state.finalize_cup_handle_observation(
                session_date="2026-04-09",
                algorithm_version=CUP_HANDLE_ALGORITHM_VERSION,
                parameter_version=CUP_HANDLE_PARAMETER_VERSION,
                expected_open_cycles=2,
                min_cycle_coverage=0.85,
                max_error_cycle_ratio=0.05,
                max_detection_p95_ms=250.0,
                min_evaluable_ticker_coverage=0.95,
                max_gap_ticker_ratio=0.05,
                max_bar_count=96,
            )

            self.assertEqual(summary["status"], "FAIL")
            self.assertEqual(summary["error_count"], 0)
            self.assertEqual(summary["gap_event_count"], 1)
            self.assertEqual(summary["gap_ticker_count"], 1)
            self.assertEqual(summary["evaluable_ticker_coverage"], 0.9)
            self.assertIn(
                "INSUFFICIENT_EVALUABLE_TICKER_COVERAGE",
                summary["failure_reasons"],
            )
            self.assertIn(
                "EXCESSIVE_MINUTE_DATA_GAPS",
                summary["failure_reasons"],
            )
            state.finalize_session_observation(
                session_date="2026-04-09",
                expected_open_cycles=1,
                min_cycle_coverage=0.85,
                max_error_cycle_ratio=0.05,
                max_cycle_p95_seconds=30.0,
            )
            gap_counts = state.status()["cup_handle"]["latest_gap_counts"]
            self.assertEqual(gap_counts[0]["event_count"], 1)

    def test_replay_reports_false_positive_proxy_contract(self):
        settings = replace(
            IntradayMonitorSettings(),
            cup_replay_confirmation_horizon_bars=2,
            cup_replay_confirmation_return_pct=1.0,
        )
        candidate = _candidate(settings)
        five_rows = _handle_bars(candidate)
        trigger_close = float(five_rows[-1]["close"])
        five_rows.extend([
            {
                **five_rows[-1],
                "timestamp": "2026-04-09 10:35:00",
                "high": trigger_close * 1.02,
                "low": trigger_close * 0.999,
                "close": trigger_close * 1.01,
            },
            {
                **five_rows[-1],
                "timestamp": "2026-04-09 10:40:00",
            },
        ])
        minute_rows = []
        minute_index = []
        for row in five_rows:
            start = pd.Timestamp(row["timestamp"])
            for offset in range(5):
                minute_index.append(start + pd.Timedelta(minutes=offset))
                minute_rows.append({
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "volume": float(row["volume"]) / 5.0,
                })
        minute = pd.DataFrame(minute_rows, index=minute_index)

        result = replay_cup_handle(
            {"TEST": _daily_cup()},
            {"TEST": minute},
            settings=settings,
            start="2026-04-09",
            end="2026-04-09",
        )

        self.assertEqual(result["algorithm_version"], CUP_HANDLE_ALGORITHM_VERSION)
        self.assertEqual(result["signal_count"], 1)
        self.assertEqual(result["outcome_counts"], {"CONFIRMED_PROXY": 1})
        self.assertEqual(result["false_positive_rate_proxy"], 0.0)


if __name__ == "__main__":
    unittest.main()
