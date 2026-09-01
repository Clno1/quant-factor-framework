from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from scripts.prepare_intraday_momentum_env import prepare_environment
from src.breakouts.live.detector import (
    ALGORITHM_VERSION,
    PARAMETER_VERSION,
    BreakoutDetector,
)
from src.breakouts.live.cup_handle import (
    CUP_HANDLE_ALGORITHM_VERSION,
    CUP_HANDLE_PARAMETER_VERSION,
)
from src.breakouts.live.candidates import build_daily_candidate_snapshot
from src.breakouts.live.delivery import build_signal_discord_payload
from src.breakouts.live.models import (
    BreakoutSignal,
    DailyCandidate,
    QuoteSnapshot,
)
from src.breakouts.live.rolling import RollingIntradayBars
from src.breakouts.live.selector import select_active_pool
from src.breakouts.live.session import (
    expected_source_session,
    previous_xnys_sessions,
    xnys_session_schedule,
)
from src.breakouts.live.service import IntradayMomentumMonitor
from src.breakouts.live.settings import IntradayMonitorSettings
from src.breakouts.live.state import IntradayMonitorState
from src.config import PROJECT_ROOT


NEW_YORK = ZoneInfo("America/New_York")


class _FakeContract:
    def to_dict(self):
        return {
            "schema_version": 1,
            "requested_universe": "US_ACTIVE",
            "data_universe": "US_LIQUID_5M",
            "dataset_version_id": "version-test",
            "dataset_run_id": "run-test",
            "target_session": "2026-07-27",
            "bars_sha256": "sha256:test",
            "membership_sha256": None,
            "factor_publication_id": None,
            "factor_generations": {},
            "runtime_factor_id": None,
            "coverage": {"passed": True},
        }


class _FakeDailyDataset:
    data_universe = "US_LIQUID_5M"
    dataset_version_id = "version-test"
    contract = _FakeContract()

    def __init__(self, universe: pd.DataFrame, frames: dict[str, pd.DataFrame]):
        self.universe = universe
        self.frames = frames

    def frame(self, ticker: str) -> pd.DataFrame:
        return self.frames.get(ticker, pd.DataFrame())


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


def _signal(ticker: str = "AEVA", **overrides) -> BreakoutSignal:
    values = {
        "session_date": "2026-07-28",
        "ticker": ticker,
        "signal_type": "BREAKOUT",
        "trigger_family": "MOMENTUM_BREAKOUT",
        "algorithm_version": ALGORITHM_VERSION,
        "parameter_version": PARAMETER_VERSION,
        "triggered_at": datetime(2026, 7, 28, 10, 31, tzinfo=NEW_YORK),
        "bar_timestamp": datetime(2026, 7, 28, 10, 30, tzinfo=NEW_YORK),
        "price": 12.0,
        "breakout_level": 11.8,
        "opening_range_minutes": None,
        "opening_range_high": None,
        "vwap": 11.0,
        "relative_volume": 1.5,
        "ma10": 11.8,
        "ma20": 11.5,
        "ma50": 10.8,
        "setup_score": 80,
        "adr20_live": 7.0,
        "return20_live": 25.0,
        "dollar_volume": 20_000_000.0,
        "reasons": ("DAILY_PIVOT_BREAK",),
    }
    values.update(overrides)
    return BreakoutSignal(**values)


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
        self.assertFalse(settings.delivery_enabled)
        self.assertEqual(settings.required_shadow_sessions, 5)
        self.assertEqual(settings.cup_required_shadow_sessions, 5)

    def test_process_environment_can_control_monitor_and_delivery(self):
        with patch.dict(
            "os.environ",
            {
                "INTRADAY_MOMENTUM_MONITOR_ENABLED": "false",
                "INTRADAY_MOMENTUM_DISCORD_ENABLED": "true",
                "INTRADAY_MOMENTUM_DISCORD_WEBHOOK_URL": (
                    "https://discord.com/api/webhooks/test/token"
                ),
            },
            clear=False,
        ):
            settings = IntradayMonitorSettings.load()

        self.assertFalse(settings.enabled)
        self.assertTrue(settings.delivery_enabled)
        self.assertEqual(
            settings.discord_webhook_url,
            "https://discord.com/api/webhooks/test/token",
        )

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

    def test_session_schedule_and_promotion_window_follow_xnys(self):
        schedule = xnys_session_schedule("2026-11-27")

        self.assertEqual(schedule.expected_minutes, 210)
        self.assertEqual(
            previous_xnys_sessions("2026-07-28", 3),
            ["2026-07-23", "2026-07-24", "2026-07-27"],
        )

    def test_stale_daily_coverage_fails_before_scanning(self):
        universe = pd.DataFrame({
            "ticker": ["AEVA", "OKTA"],
            "asset_type": ["STOCK", "STOCK"],
            "current_dollar_volume": [20_000_000.0, 20_000_000.0],
        })
        stale = pd.DataFrame({
            "open": [10.0] * 70,
            "high": [11.0] * 70,
            "low": [9.0] * 70,
            "close": [10.0] * 70,
            "volume": [1_000_000.0] * 70,
        }, index=pd.bdate_range(end="2026-07-10", periods=70))
        daily = _FakeDailyDataset(
            universe,
            {"AEVA": stale, "OKTA": stale},
        )
        load_calls = 0

        def dataset_loader(**_kwargs):
            nonlocal load_calls
            load_calls += 1
            return daily

        with self.assertRaisesRegex(RuntimeError, "coverage is stale"):
            build_daily_candidate_snapshot(
                replace(
                    IntradayMonitorSettings(),
                    min_exact_daily_coverage=0.80,
                ),
                session_date="2026-07-28",
                source_session="2026-07-27",
                dataset_loader=dataset_loader,
            )
        self.assertEqual(load_calls, 1)

    def test_candidate_snapshot_freezes_selected_version_contract(self):
        universe = pd.DataFrame({
            "ticker": ["AEVA", "LOW", "SOXL"],
            "name": ["Aeva", "Low", "ETF"],
            "sector": ["Technology", "Technology", "Fund"],
            "asset_type": ["STOCK", "STOCK", "ETF"],
            "current_dollar_volume": [20_000_000.0, 1_000_000.0, 1_000_000_000.0],
        })
        index = pd.bdate_range(end="2026-07-27", periods=70)
        close = pd.Series(range(70), index=index, dtype="float64") / 10.0 + 10.0
        frame = pd.DataFrame({
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1_000_000.0,
        }, index=index)
        daily = _FakeDailyDataset(universe, {"AEVA": frame})
        selected: list[str] = []

        def dataset_loader(**kwargs):
            selected.extend(kwargs["ticker_selector"](universe))
            return daily

        scan = {
            "rows": [{
                "ticker": "AEVA",
                "name": "Aeva",
                "sector": "Technology",
                "score": 80,
                "adr_20d": 7.0,
                "avg_dollar_volume_20d": 20_000_000.0,
                "setup_qualified": True,
                "status": "READY",
            }],
        }
        with patch(
            "src.breakouts.live.candidates.scan_breakouts",
            return_value=scan,
        ):
            snapshot = build_daily_candidate_snapshot(
                IntradayMonitorSettings(),
                session_date="2026-07-28",
                source_session="2026-07-27",
                dataset_loader=dataset_loader,
            )

        self.assertEqual(selected, ["AEVA"])
        self.assertEqual(snapshot["candidate_count"], 1)
        self.assertEqual(snapshot["dataset_version_id"], "version-test")
        self.assertEqual(
            snapshot["data_contract"]["bars_sha256"],
            "sha256:test",
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
    def test_prepare_candidates_persists_snapshot_without_live_feed_io(self):
        class ForbiddenFeed:
            source_name = "forbidden"

            async def market_status(self, _exchange):
                raise AssertionError("candidate preparation must not query market status")

            async def quotes(self, _symbols):
                raise AssertionError("candidate preparation must not query quotes")

            async def intraday_many(self, *_args, **_kwargs):
                raise AssertionError("candidate preparation must not query minute bars")

            def counters(self):
                return {}

        snapshot = {
            "session_date": "2026-07-28",
            "generated_at": "2026-07-28T10:30:00+00:00",
            "algorithm_version": ALGORITHM_VERSION,
            "parameter_version": PARAMETER_VERSION,
            "source_data_date": "2026-07-27",
            "data_contract": _FakeContract().to_dict(),
            "candidate_count": 1,
            "cup_handle_daily": {
                "algorithm_version": CUP_HANDLE_ALGORITHM_VERSION,
                "parameter_version": CUP_HANDLE_PARAMETER_VERSION,
                "evaluated_count": 1,
                "qualified_count": 0,
            },
            "rows": [_candidate().to_dict()],
        }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = replace(
                IntradayMonitorSettings(),
                state_path=root / "state.sqlite3",
                snapshots_dir=root / "snapshots",
            )
            state = IntradayMonitorState(settings.state_path)
            monitor = IntradayMomentumMonitor(
                settings,
                feed=ForbiddenFeed(),
                state=state,
                candidate_builder=lambda *_args, **_kwargs: snapshot,
                source_session_resolver=lambda _session: "2026-07-27",
                contract_validator=lambda _contract: True,
            )

            result = asyncio.run(monitor.prepare_candidates(
                now=datetime(2026, 7, 28, 6, 30, tzinfo=NEW_YORK),
            ))

            self.assertEqual(result["phase"], "candidates_prepared")
            self.assertEqual(result["candidate_count"], 1)
            self.assertIsNotNone(state.load_candidate_snapshot(
                "2026-07-28",
                ALGORITHM_VERSION,
                PARAMETER_VERSION,
            ))

    def test_candidate_snapshot_schema_migrates_and_indexes_data_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute("""
                    CREATE TABLE candidate_snapshots (
                        session_date TEXT NOT NULL,
                        algorithm_version TEXT NOT NULL,
                        parameter_version TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY (
                            session_date, algorithm_version, parameter_version
                        )
                    )
                """)
            state = IntradayMonitorState(path)
            snapshot = {
                "session_date": "2026-07-28",
                "algorithm_version": ALGORITHM_VERSION,
                "parameter_version": PARAMETER_VERSION,
                "source_data_date": "2026-07-27",
                "data_contract": _FakeContract().to_dict(),
                "rows": [],
            }
            state.save_candidate_snapshot(snapshot)

            with sqlite3.connect(path) as connection:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(candidate_snapshots)"
                    )
                }
                row = connection.execute("""
                    SELECT data_universe, dataset_version_id, bars_sha256
                    FROM candidate_snapshots
                """).fetchone()
            self.assertTrue(
                {"data_universe", "dataset_version_id", "bars_sha256"}
                .issubset(columns)
            )
            self.assertEqual(
                row,
                ("US_LIQUID_5M", "version-test", "sha256:test"),
            )

    def test_signal_idempotency_survives_store_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            signal = _signal()

            self.assertTrue(IntradayMonitorState(path).record_signal(signal))
            self.assertFalse(IntradayMonitorState(path).record_signal(signal))
            self.assertEqual(
                IntradayMonitorState(path).status()["signal_counts"],
                {"SHADOW": 1},
            )

    def test_signal_outbox_is_idempotent_and_enforces_ticker_cooldown(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = IntradayMonitorState(Path(temporary) / "state.sqlite3")
            first = _signal()
            payload = build_signal_discord_payload(first)
            self.assertTrue(state.record_signal(first, delivery_state="PENDING"))
            self.assertTrue(state.stage_signal_delivery(first, payload, shadow=False))
            self.assertFalse(state.stage_signal_delivery(first, payload, shadow=False))

            claim = state.claim_next_delivery(
                session_date=first.session_date,
                now=first.triggered_at,
                cooldown_minutes=20,
                max_attempts=3,
            )
            self.assertIsNotNone(claim)
            state.mark_delivery_sent(claim, message_id="discord-1")

            second = _signal(
                trigger_family="SECOND_CONFIRMATION",
                signal_type="OPENING_RANGE_BREAK",
                triggered_at=datetime(2026, 7, 28, 10, 35, tzinfo=NEW_YORK),
            )
            self.assertTrue(state.record_signal(second, delivery_state="PENDING"))
            self.assertTrue(state.stage_signal_delivery(
                second,
                build_signal_discord_payload(second),
                shadow=False,
            ))
            self.assertIsNone(state.claim_next_delivery(
                session_date=second.session_date,
                now=second.triggered_at,
                cooldown_minutes=20,
                max_attempts=3,
            ))
            self.assertEqual(
                state.status()["outbox_counts"],
                {"SENT": 1, "SUPPRESSED_COOLDOWN": 1},
            )

    def test_interrupted_outbox_send_fails_closed_as_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            signal = _signal()
            state = IntradayMonitorState(path)
            state.record_signal(signal, delivery_state="PENDING")
            state.stage_signal_delivery(
                signal,
                build_signal_discord_payload(signal),
                shadow=False,
            )
            self.assertIsNotNone(state.claim_next_delivery(
                session_date=signal.session_date,
                now=signal.triggered_at,
                cooldown_minutes=20,
                max_attempts=3,
            ))

            restarted = IntradayMonitorState(path)
            self.assertIsNone(restarted.claim_next_delivery(
                session_date=signal.session_date,
                now=signal.triggered_at,
                cooldown_minutes=20,
                max_attempts=3,
            ))
            self.assertEqual(restarted.status()["outbox_counts"], {"UNKNOWN": 1})

    def test_five_complete_observations_unlock_promotion(self):
        sessions = [
            "2026-07-21",
            "2026-07-22",
            "2026-07-23",
            "2026-07-24",
            "2026-07-27",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            state = IntradayMonitorState(Path(temporary) / "state.sqlite3")
            for session_date in sessions:
                for minute in range(3):
                    state.record_monitor_cycle({
                        "session_date": session_date,
                        "mode": "shadow",
                        "phase": "completed",
                        "observed_at": f"{session_date}T14:{30 + minute}:08+00:00",
                        "market_open": True,
                        "cycle_seconds": 0.2,
                        "errors": [],
                        "candidate_count": 20,
                        "active_count": 10,
                        "data_universe": "US_LIQUID_5M",
                        "dataset_version_id": "version-test",
                        "bars_sha256": "sha256:test",
                    })
                summary = state.finalize_session_observation(
                    session_date=session_date,
                    expected_open_cycles=3,
                    min_cycle_coverage=0.85,
                    max_error_cycle_ratio=0.05,
                    max_cycle_p95_seconds=5.0,
                )
                self.assertEqual(summary["status"], "PASS")

            promotion = state.promotion_status(sessions)
            self.assertTrue(promotion["eligible"])
            self.assertEqual(promotion["passed_sessions"], 5)

    def test_live_monitor_drains_independent_outbox_once(self):
        class FakeNotifier:
            def __init__(self):
                self.calls = 0

            def send(self, _payload):
                self.calls += 1
                return {"status": 200, "message_id": "discord-live-1"}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = replace(
                IntradayMonitorSettings(),
                state_path=root / "state.sqlite3",
                snapshots_dir=root / "snapshots",
            )
            state = IntradayMonitorState(settings.state_path)
            signal = _signal()
            state.record_signal(signal, delivery_state="PENDING")
            state.stage_signal_delivery(
                signal,
                build_signal_discord_payload(signal),
                shadow=False,
            )
            notifier = FakeNotifier()
            monitor = IntradayMomentumMonitor(
                settings,
                state=state,
                delivery_mode="live",
                notifier=notifier,
            )

            first = asyncio.run(monitor._drain_outbox(
                session_date=signal.session_date,
                now=signal.triggered_at,
            ))
            second = asyncio.run(monitor._drain_outbox(
                session_date=signal.session_date,
                now=signal.triggered_at,
            ))

            self.assertEqual(notifier.calls, 1)
            self.assertEqual(first[0]["status"], "SENT")
            self.assertEqual(second, [])
            self.assertEqual(state.status()["outbox_counts"], {"SENT": 1})

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
            "data_contract": _FakeContract().to_dict(),
            "candidate_count": 1,
            "cup_handle_daily": {
                "algorithm_version": CUP_HANDLE_ALGORITHM_VERSION,
                "parameter_version": CUP_HANDLE_PARAMETER_VERSION,
                "evaluated_count": 1,
                "qualified_count": 0,
            },
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
                contract_validator=lambda _contract: True,
            )
            first_result = asyncio.run(first.cycle(now=now, force_broad=True))

            restarted = IntradayMomentumMonitor(
                settings,
                feed=FakeFeed(),
                state=IntradayMonitorState(settings.state_path),
                candidate_builder=builder,
                source_session_resolver=lambda _session: "2026-07-27",
                contract_validator=lambda _contract: True,
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
            self.assertEqual(
                IntradayMonitorState(settings.state_path)
                .status()["outbox_counts"],
                {"SHADOW": 1},
            )


class DeploymentContractTests(unittest.TestCase):
    def test_intraday_env_is_prepared_atomically_without_secret_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "momentum.env"
            destination = root / "intraday.env"
            source.write_text(
                "FMP_API_KEY=fmp-secret\n"
                "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/test/token\n"
                "DISCORD_ALERT_ROLE_ID=123456789012345678\n"
                "MOMENTUM_DASHBOARD_BASE_URL=https://quant.example/\n",
                encoding="utf-8",
            )

            keys = prepare_environment(
                [source],
                destination,
                delivery_enabled=True,
            )
            content = destination.read_text(encoding="utf-8")

            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertIn("INTRADAY_MOMENTUM_MONITOR_ENABLED=true", content)
            self.assertIn("INTRADAY_MOMENTUM_DISCORD_ENABLED=true", content)
            self.assertIn("MOMENTUM_DASHBOARD_BASE_URL=https://quant.example", content)
            self.assertEqual(len(keys), 6)

    def test_intraday_env_discards_an_invalid_optional_role(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "momentum.env"
            destination = root / "intraday.env"
            source.write_text(
                "FMP_API_KEY=fmp-secret\n"
                "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/test/token\n"
                "DISCORD_ALERT_ROLE_ID=disabled\n",
                encoding="utf-8",
            )

            prepare_environment([source], destination, delivery_enabled=True)

            self.assertIn(
                "INTRADAY_MOMENTUM_DISCORD_ROLE_ID=\n",
                destination.read_text(encoding="utf-8"),
            )

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
        self.assertIn("--auto", service)
        self.assertIn("MemoryMax=768M", service)
        self.assertIn("CPUQuota=100%", service)
        self.assertNotIn("DISCORD_WEBHOOK_URL", service)
        self.assertIn("INTRADAY_MOMENTUM_MONITOR_ENABLED=true", env_example)
        self.assertIn("INTRADAY_MOMENTUM_DISCORD_ENABLED=false", env_example)
        self.assertIn("INTRADAY_MOMENTUM_DISCORD_WEBHOOK_URL=", env_example)
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
