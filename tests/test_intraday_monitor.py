from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from src.breakouts.live.detector import (
    ALGORITHM_VERSION,
    PARAMETER_VERSION,
    BreakoutDetector,
)
from src.breakouts.live.candidates import build_daily_candidate_snapshot
from src.breakouts.live.models import (
    BreakoutSignal,
    DailyCandidate,
    QuoteSnapshot,
)
from src.breakouts.live.rolling import RollingIntradayBars
from src.breakouts.live.selector import select_active_pool
from src.breakouts.live.session import expected_source_session
from src.breakouts.live.service import IntradayMomentumMonitor
from src.breakouts.live.settings import IntradayMonitorSettings
from src.breakouts.live.state import IntradayMonitorState
from src.config import PROJECT_ROOT


NEW_YORK = ZoneInfo("America/New_York")


def _candidate(ticker: str = "AEVA", **overrides) -> DailyCandidate:
    values = {
        "ticker": ticker,
        "name": f"{ticker} Inc.",
        "sector": "Technology",
        "setup_score": 80,
        "daily_pivot": 11.8,
        "previous_high": 11.6,
        "adr20": 7.0,
        "avg_dollar_volume20": 20_000_000.0,
        "source_data_date": "2026-07-27",
        "setup_qualified": True,
        "daily_status": "READY",
        "return_reference_close": 9.0,
        "adr_sum_19": 120.0,
        "forced_watch": False,
    }
    values.update(overrides)
    return DailyCandidate(**values)


def _quote(ticker: str = "AEVA", **overrides) -> QuoteSnapshot:
    values = {
        "ticker": ticker,
        "timestamp": datetime(2026, 7, 28, 10, 31, 5, tzinfo=NEW_YORK),
        "price": 12.0,
        "cumulative_volume": 2_000_000.0,
        "day_high": 12.1,
        "day_low": 10.0,
        "open": 10.1,
        "previous_close": 10.0,
        "change_percentage": 20.0,
    }
    values.update(overrides)
    return QuoteSnapshot(**values)


def _exact_frame() -> pd.DataFrame:
    previous = pd.date_range("2026-07-27 09:30", periods=390, freq="min")
    current = pd.date_range("2026-07-28 09:30", periods=61, freq="min")
    index = previous.append(current)
    previous_close = pd.Series(
        [8.0 + index_value * (2.0 / 389.0) for index_value in range(390)],
        index=previous,
    )
    current_close = pd.Series(
        [10.0 + index_value * (2.0 / 60.0) for index_value in range(61)],
        index=current,
    )
    close = pd.concat([previous_close, current_close])
    return pd.DataFrame({
        "open": close - 0.01,
        "high": close + 0.02,
        "low": close - 0.02,
        "close": close,
        "volume": 100_000.0,
    }, index=index)


class SettingsAndSelectorTests(unittest.TestCase):
    def test_defaults_match_confirmed_capacity_and_etf_policy(self):
        settings = IntradayMonitorSettings.load()

        self.assertFalse(settings.include_etfs)
        self.assertEqual(settings.max_symbols, 600)
        self.assertEqual(settings.active_max_symbols, 40)
        self.assertEqual(settings.broad_refresh_minutes, 5)
        self.assertEqual(settings.bars_interval_minutes, 1)

    def test_active_pool_is_bounded_and_prioritizes_forced_ready_names(self):
        candidates = [
            _candidate("LOW", setup_score=20, setup_qualified=False, daily_status="FORMING"),
            _candidate("READY", setup_score=90),
            _candidate("FORCED", setup_score=10, forced_watch=True),
        ]
        quotes = {
            "LOW": _quote("LOW", price=10.0, day_high=10.0),
            "READY": _quote("READY", price=11.7, day_high=11.9),
            "FORCED": _quote("FORCED", price=8.0, day_high=8.0),
        }

        selected = select_active_pool(candidates, quotes, max_symbols=2)

        self.assertEqual(
            [selection.candidate.ticker for selection in selected],
            ["FORCED", "READY"],
        )

    def test_active_pool_uses_retention_only_after_signal_priority(self):
        candidates = [
            _candidate("OLD", setup_score=80),
            _candidate("NEW", setup_score=80),
            _candidate("TOUCHED", setup_score=80),
        ]
        quotes = {
            "OLD": _quote("OLD", price=11.7, day_high=11.7),
            "NEW": _quote("NEW", price=11.7, day_high=11.7),
            "TOUCHED": _quote("TOUCHED", price=11.9, day_high=12.0),
        }

        selected = select_active_pool(
            candidates,
            quotes,
            max_symbols=2,
            previous_tickers=["OLD"],
        )

        self.assertEqual(
            [selection.candidate.ticker for selection in selected],
            ["TOUCHED", "OLD"],
        )

    def test_xnys_source_session_is_explicit(self):
        class FakeCalendar:
            @staticmethod
            def is_session(_label):
                return True

            @staticmethod
            def previous_session(_label):
                return pd.Timestamp("2026-07-27")

        self.assertEqual(
            expected_source_session("2026-07-28", calendar=FakeCalendar()),
            "2026-07-27",
        )

    @patch("src.breakouts.live.candidates.load_daily_frame")
    @patch("src.breakouts.live.candidates.get_universe")
    def test_stale_daily_coverage_fails_before_scanning(
        self,
        universe_mock,
        load_mock,
    ):
        universe_mock.return_value = pd.DataFrame({
            "ticker": ["AEVA", "OKTA"],
            "asset_type": ["STOCK", "STOCK"],
            "current_dollar_volume": [20_000_000.0, 20_000_000.0],
        })
        load_mock.return_value = pd.DataFrame({
            "open": [10.0] * 70,
            "high": [11.0] * 70,
            "low": [9.0] * 70,
            "close": [10.0] * 70,
            "volume": [1_000_000.0] * 70,
        }, index=pd.bdate_range(end="2026-07-10", periods=70))

        with self.assertRaisesRegex(RuntimeError, "coverage is stale"):
            build_daily_candidate_snapshot(
                replace(
                    IntradayMonitorSettings(),
                    min_exact_daily_coverage=0.80,
                ),
                session_date="2026-07-28",
                source_session="2026-07-27",
            )


class RollingAndDetectorTests(unittest.TestCase):
    def test_forming_minute_is_excluded(self):
        frame = pd.DataFrame({
            "open": [10.0, 11.0],
            "high": [10.2, 11.2],
            "low": [9.9, 10.9],
            "close": [10.1, 11.1],
            "volume": [100.0, 200.0],
        }, index=pd.to_datetime(["2026-07-28 10:30", "2026-07-28 10:31"]))
        rolling = RollingIntradayBars("AEVA")
        rolling.merge(frame)

        completed = rolling.completed_frame(
            datetime(2026, 7, 28, 10, 31, 8, tzinfo=NEW_YORK)
        )

        self.assertEqual(completed.index.tolist(), [pd.Timestamp("2026-07-28 10:30")])

    def test_duplicate_and_out_of_order_exact_bars_are_reconciled(self):
        rolling = RollingIntradayBars("AEVA")
        initial = pd.DataFrame({
            "open": [10.0, 10.2],
            "high": [10.1, 10.3],
            "low": [9.9, 10.1],
            "close": [10.0, 10.2],
            "volume": [100.0, 100.0],
        }, index=pd.to_datetime(["2026-07-28 10:30", "2026-07-28 10:32"]))
        missing = pd.DataFrame({
            "open": [10.1],
            "high": [10.2],
            "low": [10.0],
            "close": [10.1],
            "volume": [100.0],
        }, index=pd.to_datetime(["2026-07-28 10:31"]))
        now = datetime(2026, 7, 28, 10, 34, tzinfo=NEW_YORK)

        self.assertEqual(rolling.merge(initial), 2)
        self.assertEqual(rolling.merge(initial), 0)
        rolling.metrics(now=now, session_date="2026-07-28", interval=5)
        self.assertEqual(rolling.merge(missing), 1)
        rebuilt = rolling.metrics(
            now=now,
            session_date="2026-07-28",
            interval=5,
        )

        self.assertEqual(rolling.stored_bars, 3)
        self.assertEqual(rebuilt["completed_source_bars"], 3)
        self.assertEqual(rebuilt["last_timestamp"], "2026-07-28 10:32:00")

    def test_exact_metrics_preserve_legacy_opening_range_and_add_vwap(self):
        rolling = RollingIntradayBars("AEVA")
        rolling.merge(_exact_frame())

        metrics = rolling.metrics(
            now=datetime(2026, 7, 28, 10, 31, 8, tzinfo=NEW_YORK),
            session_date="2026-07-28",
            interval=5,
        )

        self.assertIsNone(metrics["error"])
        self.assertTrue(metrics["opening_ranges"]["60"]["triggered"])
        self.assertTrue(metrics["opening_ranges"]["60"]["current_above"])
        self.assertIsNotNone(metrics["vwap"])
        self.assertAlmostEqual(metrics["relative_volume"], 1.0)
        self.assertGreater(metrics["ma10"], metrics["ma20"])
        self.assertGreater(metrics["ma20"], metrics["ma50"])

    def test_detector_requires_fresh_exact_bar_and_market_open(self):
        detector = BreakoutDetector(IntradayMonitorSettings())
        now = datetime(2026, 7, 28, 10, 31, 8, tzinfo=NEW_YORK)
        metrics = {
            "error": None,
            "last_timestamp": "2026-07-28 10:30:00",
            "last_price": 12.0,
            "vwap": 11.0,
            "ma10": 11.8,
            "ma20": 11.4,
            "ma50": 10.9,
            "opening_ranges": {
                "60": {
                    "high": 11.7,
                    "low": 10.0,
                    "triggered": True,
                    "current_above": True,
                }
            },
        }

        signal = detector.evaluate(
            _candidate(),
            _quote(),
            metrics,
            now=now,
            session_date="2026-07-28",
            market_open=True,
        )
        closed = detector.evaluate(
            _candidate(),
            _quote(),
            metrics,
            now=now,
            session_date="2026-07-28",
            market_open=False,
        )
        stale = detector.evaluate(
            _candidate(),
            _quote(timestamp=datetime(2026, 7, 28, 10, 20, tzinfo=NEW_YORK)),
            metrics,
            now=now,
            session_date="2026-07-28",
            market_open=True,
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, "OPENING_RANGE_BREAK")
        self.assertEqual(
            signal.reasons,
            ("DAILY_PIVOT_BREAK", "OPENING_RANGE_BREAK"),
        )
        self.assertIsNone(closed)
        self.assertIsNone(stale)


class StateAndServiceTests(unittest.TestCase):
    def test_signal_idempotency_survives_store_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            signal = BreakoutSignal(
                session_date="2026-07-28",
                ticker="AEVA",
                signal_type="BREAKOUT",
                trigger_family="MOMENTUM_BREAKOUT",
                algorithm_version=ALGORITHM_VERSION,
                parameter_version=PARAMETER_VERSION,
                triggered_at=datetime(2026, 7, 28, 10, 31, tzinfo=NEW_YORK),
                bar_timestamp=datetime(2026, 7, 28, 10, 30, tzinfo=NEW_YORK),
                price=12.0,
                breakout_level=11.8,
                opening_range_minutes=None,
                opening_range_high=None,
                vwap=11.0,
                relative_volume=1.5,
                ma10=None,
                ma20=None,
                ma50=None,
                setup_score=80,
                adr20_live=7.0,
                return20_live=25.0,
                dollar_volume=20_000_000.0,
                reasons=("DAILY_PIVOT_BREAK",),
            )

            self.assertTrue(IntradayMonitorState(path).record_signal(signal))
            self.assertFalse(IntradayMonitorState(path).record_signal(signal))
            self.assertEqual(
                IntradayMonitorState(path).status()["signal_counts"],
                {"SHADOW": 1},
            )

    def test_service_records_once_and_uses_exact_confirmation(self):
        class FakeFeed:
            source_name = "fake"

            def __init__(self):
                self.exact_requests = 0

            async def market_status(self, _exchange):
                return {"isMarketOpen": True}

            async def quotes(self, symbols):
                return {
                    ticker: _quote(ticker)
                    for ticker in list(symbols)
                }

            async def intraday_many(self, tickers, *, session_date, preload=False):
                del session_date
                del preload
                normalized = list(tickers)
                self.exact_requests += len(normalized)
                return {ticker: _exact_frame() for ticker in normalized}

            def counters(self):
                return {"exact_requests": self.exact_requests}

        snapshot = {
            "session_date": "2026-07-28",
            "generated_at": "2026-07-28T13:20:00+00:00",
            "algorithm_version": ALGORITHM_VERSION,
            "parameter_version": PARAMETER_VERSION,
            "source_data_date": "2026-07-27",
            "candidate_count": 1,
            "rows": [_candidate().to_dict()],
        }

        def builder(_settings, *, session_date, source_session):
            self.assertEqual(session_date, "2026-07-28")
            self.assertEqual(source_session, "2026-07-27")
            return snapshot

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = replace(
                IntradayMonitorSettings(),
                active_max_symbols=1,
                active_hard_limit=1,
                state_path=root / "state.sqlite3",
                snapshots_dir=root / "snapshots",
            )
            state = IntradayMonitorState(settings.state_path)
            now = datetime(2026, 7, 28, 10, 31, 8, tzinfo=NEW_YORK)
            first = IntradayMomentumMonitor(
                settings,
                feed=FakeFeed(),
                state=state,
                candidate_builder=builder,
                source_session_resolver=lambda _session: "2026-07-27",
            )
            first_result = asyncio.run(first.cycle(now=now, force_broad=True))

            restarted = IntradayMomentumMonitor(
                settings,
                feed=FakeFeed(),
                state=IntradayMonitorState(settings.state_path),
                candidate_builder=builder,
                source_session_resolver=lambda _session: "2026-07-27",
            )
            second_result = asyncio.run(
                restarted.cycle(now=now, force_broad=True)
            )

            self.assertEqual(first_result["new_signal_count"], 1)
            self.assertEqual(second_result["new_signal_count"], 0)
            self.assertEqual(
                IntradayMonitorState(settings.state_path)
                .status()["signal_counts"],
                {"SHADOW": 1},
            )


class DeploymentContractTests(unittest.TestCase):
    def test_systemd_unit_is_shadow_only_and_resource_bounded(self):
        service = (
            PROJECT_ROOT
            / "deploy"
            / "systemd"
            / "quant-intraday-momentum-monitor.service"
        ).read_text(encoding="utf-8")
        timer = (
            PROJECT_ROOT
            / "deploy"
            / "systemd"
            / "quant-intraday-momentum-monitor.timer"
        ).read_text(encoding="utf-8")
        env_example = (
            PROJECT_ROOT
            / "deploy"
            / "systemd"
            / "intraday-momentum-monitor.env.example"
        ).read_text(encoding="utf-8")

        self.assertIn("run_intraday_momentum_monitor.py", service)
        self.assertIn("--shadow", service)
        self.assertIn("MemoryMax=768M", service)
        self.assertIn("CPUQuota=100%", service)
        self.assertNotIn("DISCORD_WEBHOOK_URL", service)
        self.assertNotIn("DISCORD_WEBHOOK_URL", env_example)
        self.assertIn("America/New_York", timer)
        self.assertIn("09:20:00", timer)

    def test_market_closed_cycle_does_not_build_candidates(self):
        class ClosedFeed:
            source_name = "closed"

            async def market_status(self, _exchange):
                return {"isMarketOpen": False}

            def counters(self):
                return {}

        def forbidden_builder(*_args, **_kwargs):
            raise AssertionError("candidate builder must not run while closed")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = replace(
                IntradayMonitorSettings(),
                state_path=root / "state.sqlite3",
                snapshots_dir=root / "snapshots",
            )
            monitor = IntradayMomentumMonitor(
                settings,
                feed=ClosedFeed(),
                candidate_builder=forbidden_builder,
                source_session_resolver=lambda _session: (_ for _ in ()).throw(
                    AssertionError("calendar must not run while closed")
                ),
            )

            result = asyncio.run(monitor.cycle(
                now=datetime(2026, 7, 28, 10, 0, tzinfo=NEW_YORK),
            ))

            self.assertEqual(result["phase"], "market_closed")


if __name__ == "__main__":
    unittest.main()
