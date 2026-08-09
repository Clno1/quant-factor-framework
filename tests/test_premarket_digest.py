from __future__ import annotations

import copy
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

import src.alerts.config as alert_config
from src.alerts.config import AlertSettings
from src.alerts.discord import DiscordDeliveryError, validate_discord_payload
from src.premarket_digest.groups import GroupArtifactDigestSource
from src.premarket_digest.models import DigestChannel, SourceGateError
from src.premarket_digest.momentum import CompletedSessionMomentumSource
from src.premarket_digest.render import (
    build_momentum_payload,
    build_sector_rotation_payload,
)
from src.premarket_digest.schedule import resolve_premarket_context
from src.premarket_digest.service import PremarketDigestService
from src.premarket_digest.settings import (
    PremarketDigestSettings,
    load_premarket_digest_settings,
)
from src.premarket_digest.state import DigestStateStore
from scripts.configure_momentum_discord import (
    _update_env_file as update_momentum_env_file,
)
from scripts.configure_premarket_discord import (
    _update_env_file as update_premarket_env_file,
)
from scripts.run_premarket_digest import main as run_premarket_digest_main
from scripts.refresh_us_active import (
    _latest_completed_xnys_session,
    _publish_universe_manifest,
    _select_refresh_tickers,
)


TARGET = "2026-07-16"
SOURCE = "2026-07-15"
NOW = datetime(2026, 7, 16, 13, 20, 15, tzinfo=timezone.utc)  # 09:20:15 ET


class _Calendar:
    sessions = {
        pd.Timestamp("2026-07-02"): pd.Timestamp("2026-07-01"),
        pd.Timestamp("2026-07-06"): pd.Timestamp("2026-07-02"),
        pd.Timestamp("2026-07-16"): pd.Timestamp("2026-07-15"),
    }

    def is_session(self, label):
        return pd.Timestamp(label).normalize() in self.sessions

    def previous_session(self, label):
        return self.sessions[pd.Timestamp(label).normalize()]


def _settings(root: Path, **updates) -> PremarketDigestSettings:
    base = PremarketDigestSettings(
        enabled=True,
        state_path=root / "state.sqlite3",
        dry_runs_dir=root / "dry-runs",
        momentum_webhook_url="https://discord.com/api/webhooks/1/momentum-secret",
        sector_rotation_webhook_url="https://discord.com/api/webhooks/2/sector-secret",
    )
    return replace(base, **updates)


def _context(settings: PremarketDigestSettings | None = None):
    return resolve_premarket_context(
        settings or PremarketDigestSettings(),
        now=NOW,
        requested_session=TARGET,
        calendar=_Calendar(),
    )


def _daily_frame(end: str = SOURCE) -> pd.DataFrame:
    dates = pd.bdate_range(end=pd.Timestamp(end), periods=80)
    close = pd.Series(
        [20.0 * (1.02**index) for index in range(len(dates))],
        index=dates,
    )
    return pd.DataFrame(
        {
            "open": close * 0.99,
            "high": close * 1.04,
            "low": close * 0.96,
            "close": close,
            "volume": 2_000_000.0,
        },
        index=dates,
    )


def _write_universe_manifest(cache: Path, row_count: int, *, source: str = SOURCE) -> None:
    digest = "sha256:" + hashlib.sha256(cache.read_bytes()).hexdigest()
    cache.with_suffix(".premarket.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "universe": "US_ACTIVE",
                "source_session": source,
                "refreshed_at": datetime.now(timezone.utc).isoformat(),
                "parquet_sha256": digest,
                "row_count": row_count,
            }
        ),
        encoding="utf-8",
    )


def _momentum_report() -> dict:
    row = {
        "ticker": "NVDA",
        "name": "NVIDIA @everyone",
        "status": "READY",
        "score": 85,
        "close": 173.1,
        "pivot": 175.2,
        "pivot_distance": -1.2,
        "return_20d": 24.6,
        "adr_20d": 6.8,
        "dollar_volume": 1_200_000_000,
        "avg_dollar_volume_20d": 980_000_000,
        "data_date": SOURCE,
    }
    return {
        "source_session": SOURCE,
        "universe": "US_ACTIVE",
        "asset_scope": "stocks",
        "universe_count": 100,
        "exact_asof_count": 99,
        "exact_asof_coverage": 0.99,
        "evaluable_history_count": 98,
        "evaluable_history_coverage": 0.98,
        "universe_manifest_source_session": SOURCE,
        "universe_manifest_refreshed_at": "2026-07-15T23:15:00+00:00",
        "candidate_count": 1,
        "breakout_count": 0,
        "ready_count": 1,
        "setup_count": 0,
        "forming_count": 0,
        "market_regime": {"status": "PASS", "asof": SOURCE},
        "input_fingerprint": "sha256:test",
        "rows": [row],
    }


def _group_level(level: str, run_id: str) -> dict:
    def row(name, value):
        return {
            "group_id": name.lower(),
            "group_name": name,
            "robust_ew_return_1d": value,
            "up_pct": 0.72 if value > 0 else 0.2,
            "n_valid": 64,
            "n_expected": 67,
            "count_coverage": 64 / 67,
            "headline_relative_return_1d": value - 0.003,
            "top_driver_ticker": "NVDA",
            "bottom_driver_ticker": "TSLA",
        }

    return {
        "level": level,
        "run_id": run_id,
        "source_session": SOURCE,
        "algorithm_version": "1.0.0",
        "taxonomy_version": "fmp-v1",
        "quality_status": "OK",
        "quality_summary": {
            "n_expected": 500,
            "n_valid": 495,
            "count_coverage": 0.99,
            "n_groups_ranked": 11,
        },
        "benchmark": "SPY",
        "benchmark_return_1d": 0.003,
        "warning": None,
        "top": [row("Technology", 0.0123), row("Energy", 0.009)],
        "bottom": [row("Utilities", -0.011), row("Real Estate", -0.008)],
    }


def _group_report() -> dict:
    return {
        "source_session": SOURCE,
        "universe": "SP500",
        "taxonomy": "FMP",
        "methodology": "ROBUST_EW / MAD winsor",
        "partial": False,
        "errors": {},
        "levels": {
            "sector": _group_level("sector", "ga-sector"),
            "sub_industry": _group_level("sub_industry", "ga-sub"),
        },
    }


class SettingsTests(unittest.TestCase):
    def test_shared_always_tickers_parse_csv_without_loading_local_env(self):
        with patch.object(
            alert_config.CONFIG,
            "to_dict",
            return_value={"momentum_alerts": {"always_tickers": "AEVA,OKTA"}},
        ), patch("src.alerts.config.load_local_env") as env_loader, patch.dict(
            os.environ,
            {"MOMENTUM_ALERT_EXTRA_TICKERS": "IGNORED"},
            clear=True,
        ):
            settings = AlertSettings.load(
                load_env=False,
                include_environment_tickers=False,
            )
        env_loader.assert_not_called()
        self.assertEqual(settings.always_tickers, ("AEVA", "OKTA"))

    def test_dual_webhooks_and_legacy_momentum_only_fallback(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/legacy"},
            clear=True,
        ):
            settings = load_premarket_digest_settings(
                {"premarket_digest": {}},
                output_root=Path(temporary),
                load_env=False,
            )
            self.assertEqual(settings.momentum_webhook_url.endswith("/legacy"), True)
            self.assertEqual(settings.sector_rotation_webhook_url, "")
            self.assertNotIn("legacy", repr(settings))

    def test_strict_boolean_rejects_typo(self):
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            load_premarket_digest_settings(
                {"premarket_digest": {"enabled": "truthy"}}, load_env=False
            )

    def test_environment_can_disable_sector_rotation(self):
        with patch.dict(
            os.environ,
            {"PREMARKET_SECTOR_ROTATION_ENABLED": "false"},
            clear=True,
        ):
            settings = load_premarket_digest_settings(
                {"premarket_digest": {"sector_rotation": {"enabled": True}}},
                load_env=False,
            )

        self.assertFalse(settings.sector_rotation_enabled)

    def test_schedule_times_must_be_zero_padded(self):
        with self.assertRaisesRegex(ValueError, "zero-padded"):
            load_premarket_digest_settings(
                {"premarket_digest": {"scheduled_window_start": "9:20"}},
                load_env=False,
            )

    def test_schedule_window_must_stay_aligned_with_fixed_timer(self):
        invalid = (
            {"scheduled_window_start": "09:21"},
            {"scheduled_window_end": "09:30"},
            {"scheduled_window_end": "09:19"},
        )
        for update in invalid:
            with self.subTest(update=update), self.assertRaises(ValueError):
                load_premarket_digest_settings(
                    {"premarket_digest": update}, load_env=False
                )

    def test_invalid_role_is_deferred_to_its_channel(self):
        with patch.dict(
            os.environ,
            {"DISCORD_SECTOR_ROTATION_ROLE_ID": "١٢٣"},
            clear=True,
        ):
            settings = load_premarket_digest_settings(
                {"premarket_digest": {}}, load_env=False
            )
        self.assertEqual(settings.sector_rotation_role_id, "١٢٣")

    def test_same_webhook_credential_is_rejected_even_with_different_query(self):
        base = "https://discord.com/api/webhooks/1/same-token"
        with patch.dict(
            os.environ,
            {
                "DISCORD_MOMENTUM_WEBHOOK_URL": base + "?wait=false",
                "DISCORD_SECTOR_ROTATION_WEBHOOK_URL": base.replace(
                    "discord.com", "discord.com:443"
                ) + "?thread_id=2",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "independent"):
                load_premarket_digest_settings(
                    {"premarket_digest": {}}, load_env=False
                )


class ScheduleTests(unittest.TestCase):
    def test_regular_session_and_previous_session(self):
        context = resolve_premarket_context(
            PremarketDigestSettings(),
            now=NOW,
            scheduled=True,
            calendar=_Calendar(),
        )
        self.assertEqual(context.target_session, TARGET)
        self.assertEqual(context.source_session, SOURCE)

    def test_monday_after_holiday_uses_previous_exchange_session(self):
        context = resolve_premarket_context(
            PremarketDigestSettings(),
            now=datetime(2026, 7, 6, 13, 20, tzinfo=timezone.utc),
            scheduled=True,
            calendar=_Calendar(),
        )
        self.assertEqual(context.source_session, "2026-07-02")

    def test_window_boundaries_and_late_persistent_wakeup(self):
        settings = PremarketDigestSettings()
        for minute, allowed in ((19, False), (20, True), (29, True), (30, False)):
            with self.subTest(minute=minute):
                now = datetime(2026, 7, 16, 13, minute, tzinfo=timezone.utc)
                if allowed:
                    resolve_premarket_context(
                        settings, now=now, scheduled=True, calendar=_Calendar()
                    )
                else:
                    with self.assertRaisesRegex(RuntimeError, "outside"):
                        resolve_premarket_context(
                            settings, now=now, scheduled=True, calendar=_Calendar()
                        )

    @unittest.skipUnless(
        importlib.util.find_spec("exchange_calendars"),
        "exchange-calendars is installed by requirements.txt in production",
    )
    def test_real_xnys_calendar_covers_dst_and_independence_day(self):
        cases = (
            (
                datetime(2026, 3, 9, 13, 20, tzinfo=timezone.utc),
                "2026-03-09",
                "2026-03-06",
            ),
            (
                datetime(2026, 11, 2, 14, 20, tzinfo=timezone.utc),
                "2026-11-02",
                "2026-10-30",
            ),
            (
                datetime(2026, 7, 6, 13, 20, tzinfo=timezone.utc),
                "2026-07-06",
                "2026-07-02",
            ),
        )
        for now, target, source in cases:
            with self.subTest(now=now):
                context = resolve_premarket_context(
                    PremarketDigestSettings(), now=now, scheduled=True
                )
                self.assertEqual(context.target_session, target)
                self.assertEqual(context.source_session, source)
        with self.assertRaisesRegex(RuntimeError, "not an XNYS"):
            resolve_premarket_context(
                PremarketDigestSettings(),
                now=datetime(2026, 7, 3, 13, 20, tzinfo=timezone.utc),
                scheduled=True,
            )
        self.assertEqual(
            _latest_completed_xnys_session(
                now=pd.Timestamp("2026-07-03T23:15:00Z")
            ).date().isoformat(),
            "2026-07-02",
        )
        self.assertEqual(
            _latest_completed_xnys_session(
                now=pd.Timestamp("2026-11-27T19:29:59Z")
            ).date().isoformat(),
            "2026-11-25",
        )
        self.assertEqual(
            _latest_completed_xnys_session(
                now=pd.Timestamp("2026-11-27T19:30:00Z")
            ).date().isoformat(),
            "2026-11-27",
        )

    def test_refresh_target_uses_completed_xnys_session_across_holiday(self):
        class Calendar:
            def sessions_in_range(self, start, end):
                return pd.DatetimeIndex(["2026-07-02", "2026-07-06"])

            def session_close(self, session):
                closes = {
                    "2026-07-02": "2026-07-02T20:00:00Z",
                    "2026-07-06": "2026-07-06T20:00:00Z",
                }
                key = pd.Timestamp(session).date().isoformat()
                return pd.Timestamp(closes[key])

        target = _latest_completed_xnys_session(
            now=pd.Timestamp("2026-07-06T19:00:00Z"),
            calendar=Calendar(),
        )
        self.assertEqual(target.date().isoformat(), "2026-07-02")

    def test_refresh_target_waits_ninety_minutes_after_official_close(self):
        class Calendar:
            def sessions_in_range(self, start, end):
                return pd.DatetimeIndex(["2026-07-02"])

            def session_close(self, session):
                return pd.Timestamp("2026-07-02T20:00:00Z")

        with self.assertRaisesRegex(RuntimeError, "no completed"):
            _latest_completed_xnys_session(
                now=pd.Timestamp("2026-07-02T21:29:59Z"),
                calendar=Calendar(),
            )
        target = _latest_completed_xnys_session(
            now=pd.Timestamp("2026-07-02T21:30:00Z"),
            calendar=Calendar(),
        )
        self.assertEqual(target.date().isoformat(), "2026-07-02")


class MomentumSourceTests(unittest.TestCase):
    def test_production_source_loads_one_version_bound_daily_dataset(self):
        with tempfile.TemporaryDirectory() as temporary:
            universe = pd.DataFrame(
                {
                    "ticker": ["GOOD"],
                    "name": ["Good"],
                    "sector": ["Tech"],
                    "asset_type": ["STOCK"],
                    "current_dollar_volume": [50e6],
                }
            )
            contract_payload = {
                "dataset_version_id": "version-test",
                "data_universe": "US_LIQUID_5M",
            }
            contract = Mock()
            contract.target_session = SOURCE
            contract.to_dict.return_value = contract_payload
            version = Mock()
            version.created_at = NOW
            daily = Mock()
            daily.universe = universe
            daily.data_universe = "US_LIQUID_5M"
            daily.dataset_version_id = "version-test"
            daily.contract = contract
            daily.version = version
            daily.frame.side_effect = lambda ticker: (
                _daily_frame() if ticker == "GOOD" else pd.DataFrame()
            )
            calls: list[dict] = []

            def dataset_loader(**kwargs):
                calls.append(kwargs)
                return daily

            source = CompletedSessionMomentumSource(
                _settings(
                    Path(temporary),
                    momentum_min_exact_asof_coverage=1.0,
                    momentum_min_evaluable_coverage=1.0,
                ),
                alert_settings=AlertSettings(),
                dataset_loader=dataset_loader,
                regime_loader=lambda **_: {"status": "PASS", "asof": SOURCE},
            )

            result = source.load(SOURCE)

            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["end"], SOURCE)
            self.assertEqual(result["dataset_version_id"], "version-test")
            self.assertEqual(result["data_contract"], contract_payload)

    def test_default_universe_loader_is_cache_only(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "src.premarket_digest.momentum.PROJECT_ROOT", Path(temporary)
        ):
            cache = Path(temporary) / "data/raw/universe/us_active.parquet"
            cache.parent.mkdir(parents=True)
            expected = pd.DataFrame(
                {
                    "ticker": ["GOOD"],
                    "asset_type": ["STOCK"],
                    "current_dollar_volume": [50e6],
                }
            )
            expected.to_parquet(cache)
            _write_universe_manifest(cache, len(expected))
            source = CompletedSessionMomentumSource(_settings(Path(temporary)))
            loaded = source.universe_loader("US_ACTIVE")
            self.assertEqual(loaded["ticker"].tolist(), ["GOOD"])
            self.assertEqual(loaded.attrs["manifest_source_session"], SOURCE)
            cache.unlink()
            with self.assertRaises(SourceGateError) as caught:
                source.universe_loader("US_ACTIVE")
            self.assertEqual(
                caught.exception.code, "MOMENTUM_UNIVERSE_CACHE_MISSING"
            )

    def test_universe_manifest_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "src.premarket_digest.momentum.PROJECT_ROOT", Path(temporary)
        ):
            cache = Path(temporary) / "data/raw/universe/us_active.parquet"
            cache.parent.mkdir(parents=True)
            frame = pd.DataFrame(
                {
                    "ticker": ["GOOD"],
                    "asset_type": ["STOCK"],
                    "current_dollar_volume": [50e6],
                }
            )
            frame.to_parquet(cache)
            _write_universe_manifest(cache, len(frame))
            manifest_path = cache.with_suffix(".premarket.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["parquet_sha256"] = "sha256:" + "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            source = CompletedSessionMomentumSource(_settings(Path(temporary)))
            with self.assertRaises(SourceGateError) as caught:
                source.universe_loader("US_ACTIVE")
            self.assertEqual(caught.exception.code, "MOMENTUM_UNIVERSE_CACHE_INVALID")

    def test_failed_force_refresh_cannot_resign_an_unchanged_cache(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "scripts.refresh_us_active.ROOT", Path(temporary)
        ):
            cache = Path(temporary) / "data/raw/universe/us_active.parquet"
            cache.parent.mkdir(parents=True)
            frame = pd.DataFrame({"ticker": ["GOOD"]})
            frame.to_parquet(cache)
            stat = cache.stat()
            signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            with self.assertRaisesRegex(RuntimeError, "reused the previous cache"):
                _publish_universe_manifest(
                    frame,
                    source_session=pd.Timestamp(SOURCE),
                    refresh_started_at=datetime.now(timezone.utc),
                    previous_signature=signature,
                )

    def test_refresh_limit_keeps_low_liquidity_always_stock_but_not_etf(self):
        universe = pd.DataFrame(
            {
                "ticker": ["HIGH1", "HIGH2", "KEEP", "ETF1"],
                "asset_type": ["STOCK", "STOCK", "STOCK", "ETF"],
                "current_dollar_volume": [50e6, 40e6, 1e6, 100e6],
            }
        )
        tickers = _select_refresh_tickers(
            universe,
            stocks_only=True,
            liquidity_floor=5e6,
            always_tickers={"KEEP", "ETF1"},
            limit=1,
        )
        self.assertEqual(tickers, ["HIGH1", "KEEP"])

    def test_refresh_liquidity_filter_requires_its_source_column(self):
        with self.assertRaisesRegex(RuntimeError, "current_dollar_volume"):
            _select_refresh_tickers(
                pd.DataFrame(
                    {"ticker": ["GOOD"], "asset_type": ["STOCK"]}
                ),
                stocks_only=True,
                liquidity_floor=5e6,
                always_tickers=set(),
                limit=None,
            )

    def test_stock_only_exact_asof_gate_and_completed_bar(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(
                Path(temporary),
                momentum_min_exact_asof_coverage=0.5,
                momentum_min_evaluable_coverage=0.5,
            )
            universe = pd.DataFrame(
                {
                    "ticker": ["GOOD", "STALE", "ETF1"],
                    "name": ["Good", "Stale", "ETF"],
                    "sector": ["Tech", "Tech", "Fund"],
                    "asset_type": ["STOCK", "STOCK", "ETF"],
                    "current_dollar_volume": [50e6, 50e6, 1e9],
                }
            )
            frames = {
                "GOOD": _daily_frame(),
                "STALE": _daily_frame("2026-07-14"),
                "ETF1": _daily_frame(),
            }
            source = CompletedSessionMomentumSource(
                settings,
                alert_settings=AlertSettings(),
                universe_loader=lambda _: universe,
                frame_loader=lambda ticker: frames[ticker],
                regime_loader=lambda **_: {"status": "PASS", "asof": SOURCE},
            )

            result = source.load(SOURCE)

            self.assertEqual(result["universe_count"], 2)
            self.assertEqual(result["exact_asof_count"], 1)
            self.assertEqual(result["exact_asof_coverage"], 0.5)
            self.assertEqual(result["evaluable_history_coverage"], 0.5)
            self.assertTrue(all(row["data_date"] == SOURCE for row in result["rows"]))
            self.assertNotIn("ETF1", [row["ticker"] for row in result["rows"]])

    def test_low_exact_coverage_blocks_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(
                Path(temporary), momentum_min_exact_asof_coverage=0.8
            )
            universe = pd.DataFrame(
                {
                    "ticker": ["GOOD", "STALE"],
                    "name": ["Good", "Stale"],
                    "sector": ["Tech", "Tech"],
                    "asset_type": ["STOCK", "STOCK"],
                    "current_dollar_volume": [50e6, 50e6],
                }
            )
            source = CompletedSessionMomentumSource(
                settings,
                alert_settings=AlertSettings(),
                universe_loader=lambda _: universe,
                frame_loader=lambda ticker: _daily_frame(
                    SOURCE if ticker == "GOOD" else "2026-07-14"
                ),
                regime_loader=lambda **_: {"status": "PASS", "asof": SOURCE},
            )
            with self.assertRaises(SourceGateError) as caught:
                source.load(SOURCE)
            self.assertEqual(caught.exception.code, "MOMENTUM_LOW_EXACT_ASOF_COVERAGE")

    def test_null_and_empty_tickers_never_reach_frame_loader(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(
                Path(temporary), momentum_min_exact_asof_coverage=1.0
            )
            universe = pd.DataFrame(
                {
                    "ticker": [pd.NA, "", "GOOD"],
                    "name": ["Missing", "Empty", "Good"],
                    "sector": ["Tech", "Tech", "Tech"],
                    "asset_type": ["STOCK", "STOCK", "STOCK"],
                    "current_dollar_volume": [50e6, 50e6, 50e6],
                }
            )
            loaded: list[str] = []

            def frame_loader(ticker):
                loaded.append(ticker)
                return _daily_frame()

            source = CompletedSessionMomentumSource(
                settings,
                alert_settings=AlertSettings(),
                universe_loader=lambda _: universe,
                frame_loader=frame_loader,
                regime_loader=lambda **_: {"status": "PASS", "asof": SOURCE},
            )
            result = source.load(SOURCE)
            self.assertEqual(loaded, ["GOOD"])
            self.assertEqual(result["universe_count"], 1)

    def test_invalid_t1_bar_blocks_a_false_empty_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            universe = pd.DataFrame(
                {
                    "ticker": ["SHORT"],
                    "name": ["Short"],
                    "sector": ["Tech"],
                    "asset_type": ["STOCK"],
                    "current_dollar_volume": [50e6],
                }
            )
            frame = _daily_frame().iloc[-65:].copy()
            frame.loc[pd.Timestamp(SOURCE), "close"] = float("nan")
            source = CompletedSessionMomentumSource(
                _settings(
                    Path(temporary),
                    momentum_min_exact_asof_coverage=1.0,
                    momentum_min_evaluable_coverage=1.0,
                ),
                alert_settings=AlertSettings(),
                universe_loader=lambda _: universe,
                frame_loader=lambda _: frame,
                regime_loader=lambda **_: {"status": "PASS", "asof": SOURCE},
            )
            with self.assertRaises(SourceGateError) as caught:
                source.load(SOURCE)
            self.assertEqual(caught.exception.code, "MOMENTUM_LOW_EVALUABLE_COVERAGE")

    def test_exactly_sixty_five_unique_valid_sessions_are_evaluable(self):
        with tempfile.TemporaryDirectory() as temporary:
            universe = pd.DataFrame(
                {
                    "ticker": ["GOOD"],
                    "asset_type": ["STOCK"],
                    "current_dollar_volume": [50e6],
                }
            )
            source = CompletedSessionMomentumSource(
                _settings(
                    Path(temporary),
                    momentum_min_exact_asof_coverage=1.0,
                    momentum_min_evaluable_coverage=1.0,
                ),
                alert_settings=AlertSettings(),
                universe_loader=lambda _: universe,
                frame_loader=lambda _: _daily_frame().iloc[-65:],
                regime_loader=lambda **_: {"status": "PASS", "asof": SOURCE},
            )
            result = source.load(SOURCE)
            self.assertEqual(result["evaluable_history_count"], 1)

    def test_duplicate_daily_sessions_are_not_evaluable(self):
        with tempfile.TemporaryDirectory() as temporary:
            universe = pd.DataFrame(
                {
                    "ticker": ["DUP"],
                    "asset_type": ["STOCK"],
                    "current_dollar_volume": [50e6],
                }
            )
            frame = pd.concat([_daily_frame(), _daily_frame().iloc[[-1]]])
            source = CompletedSessionMomentumSource(
                _settings(
                    Path(temporary),
                    momentum_min_exact_asof_coverage=1.0,
                    momentum_min_evaluable_coverage=1.0,
                ),
                alert_settings=AlertSettings(),
                universe_loader=lambda _: universe,
                frame_loader=lambda _: frame,
                regime_loader=lambda **_: {"status": "PASS", "asof": SOURCE},
            )
            with self.assertRaises(SourceGateError) as caught:
                source.load(SOURCE)
            self.assertEqual(caught.exception.code, "MOMENTUM_LOW_EVALUABLE_COVERAGE")

    def test_manifest_source_session_must_match_requested_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            universe = pd.DataFrame(
                {
                    "ticker": ["GOOD"],
                    "asset_type": ["STOCK"],
                    "current_dollar_volume": [50e6],
                }
            )
            universe.attrs["manifest_source_session"] = "2026-07-14"
            universe.attrs["manifest_refreshed_at"] = NOW.isoformat()
            source = CompletedSessionMomentumSource(
                _settings(Path(temporary)),
                alert_settings=AlertSettings(),
                universe_loader=lambda _: universe,
                frame_loader=lambda _: _daily_frame(),
            )
            with self.assertRaises(SourceGateError) as caught:
                source.load(SOURCE)
            self.assertEqual(caught.exception.code, "MOMENTUM_UNIVERSE_CACHE_STALE")

    def test_nonfinite_evaluator_return_is_dropped_without_ambiguous_na(self):
        with tempfile.TemporaryDirectory() as temporary:
            universe = pd.DataFrame(
                {
                    "ticker": ["GOOD"],
                    "name": ["Good"],
                    "sector": ["Tech"],
                    "asset_type": ["STOCK"],
                    "current_dollar_volume": [50e6],
                }
            )
            source = CompletedSessionMomentumSource(
                _settings(
                    Path(temporary),
                    momentum_min_exact_asof_coverage=1.0,
                    momentum_min_evaluable_coverage=0.0,
                ),
                alert_settings=AlertSettings(),
                universe_loader=lambda _: universe,
                frame_loader=lambda _: _daily_frame(),
                regime_loader=lambda **_: {"status": "PASS", "asof": SOURCE},
            )
            invalid = {
                "ticker": "GOOD",
                "base_pass": True,
                "data_date": SOURCE,
                "return_20d": pd.NA,
            }
            with patch(
                "src.premarket_digest.momentum.evaluate_daily_setup",
                return_value=invalid,
            ):
                result = source.load(SOURCE)
            self.assertEqual(result["candidate_count"], 0)


def _metrics(level: str) -> pd.DataFrame:
    rows = []
    for index, value in enumerate((0.03, 0.02, 0.01, -0.01, -0.02, -0.03)):
        rows.append(
            {
                "group_id": f"g{index}",
                "group_name": f"Group {index}",
                "level": level,
                "robust_ew_return_1d": value,
                "up_pct": 0.8 if value > 0 else 0.2,
                "n_valid": 10,
                "n_expected": 10,
                "count_coverage": 1.0,
                "headline_relative_return_1d": value - 0.001,
                "benchmark_return_1d": 0.001,
                "top_driver_ticker": "TOP",
                "bottom_driver_ticker": "BOTTOM",
                "eligible_for_ranking": True,
            }
        )
    return pd.DataFrame(rows)


class _Reader:
    def __init__(self, bad_levels=(), manifest_updates=None, last_attempt="SUCCESS"):
        self.bad_levels = set(bad_levels)
        self.manifest_updates = manifest_updates or {}
        self.last_attempt = last_attempt

    def load_latest(self, combination):
        level = combination.level
        asof = "2026-07-14" if level in self.bad_levels else SOURCE
        manifest = {
            "asof": asof,
            "mode": "eod",
            "snapshot_id": "EOD",
            "session_status": "FINAL",
            "universe": "SP500",
            "taxonomy": "FMP",
            "taxonomy_level": level,
            "research_only": False,
            "quality_status": "OK",
            "quality_summary": {
                "n_expected": 500,
                "n_valid": 495,
                "count_coverage": 0.99,
                "n_groups_ranked": 6,
            },
            "benchmark": "SPY",
            "algorithm_version": "1.0.0",
            "taxonomy_version": "fmp-v1",
            "generated_at": "2026-07-16T08:00:00Z",
        }
        manifest.update(self.manifest_updates.get(level, {}))
        return SimpleNamespace(
            run_id=f"run-{level}", manifest=manifest, metrics=_metrics(level)
        )

    def load_last_attempt(self, combination):
        return {"last_attempt_status": self.last_attempt}


class GroupSourceTests(unittest.TestCase):
    def test_reads_both_exact_artifacts_and_ranks_non_overlapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = GroupArtifactDigestSource(
                _settings(Path(temporary)), reader=_Reader(), now=NOW
            )
            result = source.load(SOURCE)
            self.assertEqual(set(result["levels"]), {"sector", "sub_industry"})
            for level in result["levels"].values():
                top_ids = {row["group_id"] for row in level["top"]}
                bottom_ids = {row["group_id"] for row in level["bottom"]}
                self.assertTrue(top_ids.isdisjoint(bottom_ids))

    def test_one_bad_level_degrades_without_using_stale_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = GroupArtifactDigestSource(
                _settings(Path(temporary)),
                reader=_Reader(bad_levels={"sub_industry"}),
                now=NOW,
            )
            result = source.load(SOURCE)
            self.assertTrue(result["partial"])
            self.assertEqual(set(result["levels"]), {"sector"})
            self.assertEqual(
                result["errors"]["sub_industry"]["code"], "GROUP_MANIFEST_MISMATCH"
            )

    def test_both_bad_levels_block_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = GroupArtifactDigestSource(
                _settings(Path(temporary)),
                reader=_Reader(bad_levels={"sector", "sub_industry"}),
                now=NOW,
            )
            with self.assertRaises(SourceGateError) as caught:
                source.load(SOURCE)
            self.assertEqual(caught.exception.code, "GROUP_ALL_LEVELS_UNAVAILABLE")

    def test_research_coverage_and_future_gates_are_level_local(self):
        with tempfile.TemporaryDirectory() as temporary:
            updates = {
                "sector": {"research_only": True},
                "sub_industry": {
                    "quality_summary": {
                        "n_expected": 500,
                        "n_valid": 400,
                        "count_coverage": 0.8,
                        "n_groups_ranked": 6,
                    }
                },
            }
            source = GroupArtifactDigestSource(
                _settings(Path(temporary)),
                reader=_Reader(manifest_updates=updates),
                now=NOW,
            )
            with self.assertRaises(SourceGateError) as caught:
                source.load(SOURCE)
            errors = caught.exception.details["levels"]
            self.assertEqual(errors["sector"]["code"], "GROUP_RESEARCH_ONLY")
            self.assertEqual(errors["sub_industry"]["code"], "GROUP_LOW_COVERAGE")

    def test_generated_at_allows_60_seconds_but_rejects_61(self):
        with tempfile.TemporaryDirectory() as temporary:
            exactly_60 = (NOW + pd.Timedelta(seconds=60)).isoformat()
            over_60 = (NOW + pd.Timedelta(seconds=61)).isoformat()
            source = GroupArtifactDigestSource(
                _settings(Path(temporary)),
                reader=_Reader(
                    manifest_updates={
                        "sector": {"generated_at": exactly_60},
                        "sub_industry": {"generated_at": over_60},
                    }
                ),
                now=NOW,
            )
            result = source.load(SOURCE)
            self.assertEqual(set(result["levels"]), {"sector"})
            self.assertEqual(
                result["errors"]["sub_industry"]["code"], "GROUP_FUTURE_ARTIFACT"
            )

    def test_failed_newer_attempt_adds_warning_to_valid_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = GroupArtifactDigestSource(
                _settings(Path(temporary)),
                reader=_Reader(last_attempt="FAILED"),
                now=NOW,
            )
            result = source.load(SOURCE)
            self.assertIn("较新的计算尝试失败", result["levels"]["sector"]["warning"])


class RendererTests(unittest.TestCase):
    def test_momentum_payload_is_one_safe_message_and_only_mentions_role_for_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(Path(temporary), momentum_role_id="123456")
            payload = build_momentum_payload(_momentum_report(), _context(), settings)
            self.assertEqual(len(payload["embeds"]), 1)
            self.assertEqual(payload["allowed_mentions"]["roles"], ["123456"])
            self.assertNotIn("@everyone", str(payload))
            self.assertIn(SOURCE, payload["embeds"][0]["description"])
            validate_discord_payload(payload)

    def test_sector_payload_is_one_message_two_embeds_and_labels_single_day_method(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(Path(temporary))
            payload = build_sector_rotation_payload(_group_report(), _context(), settings)
            self.assertEqual(len(payload["embeds"]), 2)
            self.assertIn("今日分类涨跌", str(payload))
            self.assertIn("单日强弱并非中期行业动量", str(payload))
            validate_discord_payload(payload)

    def test_momentum_max_rows_and_missing_scalars_stay_within_embed_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = copy.deepcopy(_momentum_report())
            base = report["rows"][0]
            report["rows"] = []
            for index in range(20):
                row = dict(base)
                row.update(
                    ticker=f"T{index:02d}",
                    name="N" * 300,
                    status="READY",
                )
                report["rows"].append(row)
            report["rows"][0]["name"] = pd.NA
            report["rows"][0]["status"] = pd.NA
            report["rows"][0]["score"] = pd.NA
            report["market_regime"]["status"] = pd.NA
            report["asset_scope"] = pd.NA
            settings = _settings(Path(temporary), momentum_max_rows=20)
            payload = build_momentum_payload(report, _context(), settings)
            validate_discord_payload(payload)
            embed = payload["embeds"][0]
            total = (
                len(embed.get("title", ""))
                + len(embed.get("description", ""))
                + len(embed.get("footer", {}).get("text", ""))
                + sum(
                    len(field["name"]) + len(field["value"])
                    for field in embed["fields"]
                )
            )
            self.assertLessEqual(total, 6_000)
            self.assertLessEqual(len(embed["fields"]), 20)
            self.assertNotIn("<NA>", str(payload))

    def test_sector_pathological_text_is_trimmed_across_both_embeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = copy.deepcopy(_group_report())
            for level in report["levels"].values():
                level["run_id"] = "r" * 500
                level["taxonomy_version"] = "t" * 500
                level["warning"] = "w" * 1_000
                for side in ("top", "bottom"):
                    level[side] = []
                    for index in range(10):
                        row = copy.deepcopy(_group_level("sector", "run")[side][0])
                        row["group_name"] = "G" * 72
                        row["top_driver_ticker"] = "D" * 20
                        row["bottom_driver_ticker"] = pd.NA if index == 0 else "B" * 20
                        row["n_valid"] = 10**100
                        row["n_expected"] = 10**100
                        level[side].append(row)
            payload = build_sector_rotation_payload(report, _context(), _settings(Path(temporary)))
            validate_discord_payload(payload)
            total = sum(
                len(embed.get("title", ""))
                + len(embed.get("description", ""))
                + len(embed.get("footer", {}).get("text", ""))
                + sum(len(field["name"]) + len(field["value"]) for field in embed["fields"])
                for embed in payload["embeds"]
            )
            self.assertLessEqual(total, 5_500)
            self.assertNotIn("<NA>", str(payload))


class StateTests(unittest.TestCase):
    def test_legacy_outbox_adds_retryability_column_conservatively(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    CREATE TABLE deliveries (
                        target_session TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        destination TEXT NOT NULL,
                        source_session TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        message_id TEXT,
                        last_error_code TEXT,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        sent_at TEXT,
                        PRIMARY KEY (target_session, channel)
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()

            store = DigestStateStore(path)
            connection = sqlite3.connect(path)
            try:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(deliveries)"
                    ).fetchall()
                }
            finally:
                connection.close()
            self.assertIn("retryable", columns)

    def test_sent_delivery_is_immutable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = DigestStateStore(Path(temporary) / "state.sqlite3")
            payload = {"content": "one", "allowed_mentions": {"parse": []}}
            store.stage(TARGET, DigestChannel.MOMENTUM, SOURCE, payload)
            claim = store.claim(TARGET, DigestChannel.MOMENTUM)
            self.assertEqual(claim.action, "send")
            store.mark_sent(TARGET, DigestChannel.MOMENTUM, "message-1")
            store.stage(
                TARGET,
                DigestChannel.MOMENTUM,
                SOURCE,
                {"content": "changed", "allowed_mentions": {"parse": []}},
            )
            second = store.claim(TARGET, DigestChannel.MOMENTUM)
            self.assertEqual(second.action, "already_sent")
            self.assertEqual(second.message_id, "message-1")
            self.assertNotIn("payload_json", store.get(TARGET, DigestChannel.MOMENTUM))

    def test_interrupted_sending_requires_explicit_unknown_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = DigestStateStore(Path(temporary) / "state.sqlite3")
            store.stage(
                TARGET,
                DigestChannel.MOMENTUM,
                SOURCE,
                {"content": "one", "allowed_mentions": {"parse": []}},
            )
            store.claim(TARGET, DigestChannel.MOMENTUM)
            blocked = store.claim(TARGET, DigestChannel.MOMENTUM)
            self.assertEqual(blocked.action, "unknown_blocked")
            retry = store.claim(
                TARGET, DigestChannel.MOMENTUM, retry_unknown=True
            )
            self.assertEqual(retry.action, "send")
            self.assertEqual(retry.payload["content"], "one")

    def test_only_failed_payload_can_be_explicitly_rebuilt(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = DigestStateStore(Path(temporary) / "state.sqlite3")
            original = {"content": "one", "allowed_mentions": {"parse": []}}
            changed = {"content": "two", "allowed_mentions": {"parse": []}}
            store.stage(TARGET, DigestChannel.MOMENTUM, SOURCE, original)
            store.claim(TARGET, DigestChannel.MOMENTUM)
            store.mark_failed(
                TARGET,
                DigestChannel.MOMENTUM,
                error_code="HTTP_ERROR",
                error_message="safe rejection",
                uncertain=False,
                retryable=False,
            )
            store.stage(
                TARGET,
                DigestChannel.MOMENTUM,
                SOURCE,
                changed,
                rebuild_failed=True,
            )
            rebuilt = store.claim(TARGET, DigestChannel.MOMENTUM)
            self.assertEqual(rebuilt.payload["content"], "two")

            store.mark_failed(
                TARGET,
                DigestChannel.MOMENTUM,
                error_code="TIMEOUT",
                error_message="unknown",
                uncertain=True,
                retryable=False,
            )
            store.stage(
                TARGET,
                DigestChannel.MOMENTUM,
                SOURCE,
                original,
                rebuild_failed=True,
            )
            unknown = store.claim(
                TARGET, DigestChannel.MOMENTUM, retry_unknown=True
            )
            self.assertEqual(unknown.payload["content"], "two")

    def test_confirmed_delivery_recreates_missing_sent_tombstone(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = DigestStateStore(Path(temporary) / "state.sqlite3")
            payload = {"content": "one", "allowed_mentions": {"parse": []}}
            store.stage(TARGET, DigestChannel.MOMENTUM, SOURCE, payload)
            claim = store.claim(TARGET, DigestChannel.MOMENTUM)
            connection = sqlite3.connect(store.path)
            try:
                connection.execute(
                    "DELETE FROM deliveries WHERE target_session=? AND channel=?",
                    (TARGET, DigestChannel.MOMENTUM.value),
                )
                connection.commit()
            finally:
                connection.close()
            store.mark_sent(
                TARGET,
                DigestChannel.MOMENTUM,
                "message-1",
                source_session=SOURCE,
                payload=claim.payload,
                expected_payload_hash=claim.payload_hash,
                attempts=claim.attempts,
            )
            second = store.claim(TARGET, DigestChannel.MOMENTUM)
            self.assertEqual(second.action, "already_sent")
            self.assertEqual(second.message_id, "message-1")


class _Source:
    def __init__(self, report):
        self.report = report
        self.calls = 0

    def load(self, source_session):
        self.calls += 1
        self.assert_session = source_session
        return self.report


class _Notifier:
    def __init__(self, outcome, calls):
        self.outcome = outcome
        self.calls = calls

    def send(self, payload):
        self.calls.append(payload)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class ServiceTests(unittest.TestCase):
    def _service(self, root, outcomes, momentum=None, groups=None):
        settings = _settings(Path(root))
        calls = {"momentum": [], "sector": []}

        def factory(url):
            key = "momentum" if "momentum-secret" in url else "sector"
            outcome = outcomes[key].pop(0)
            return _Notifier(outcome, calls[key])

        service = PremarketDigestService(
            settings,
            momentum_source=momentum or _Source(_momentum_report()),
            group_source=groups or _Source(_group_report()),
            notifier_factory=factory,
            now=lambda: NOW,
            calendar=_Calendar(),
        )
        return service, calls

    def test_default_dry_run_has_no_delivery_and_writes_two_previews(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, calls = self._service(
                temporary,
                {"momentum": [], "sector": []},
            )
            summary = service.run(requested_session=TARGET)
            self.assertEqual(summary["exit_code"], 0)
            self.assertEqual(
                {result["status"] for result in summary["results"]}, {"DRY_RUN"}
            )
            self.assertEqual(calls, {"momentum": [], "sector": []})
            self.assertEqual(len(summary["preview_files"]), 3)
            self.assertTrue(all(Path(path).exists() for path in summary["preview_files"]))
            self.assertFalse((Path(temporary) / "state.sqlite3").exists())
            momentum = next(
                result for result in summary["results"] if result["channel"] == "momentum"
            )
            self.assertEqual(momentum["metadata"]["evaluable_history_coverage"], 0.98)
            self.assertEqual(
                momentum["metadata"]["universe_manifest_source_session"], SOURCE
            )

    def test_partial_failure_retries_only_failed_channel(self):
        with tempfile.TemporaryDirectory() as temporary:
            failure = DiscordDeliveryError(
                "safe", retryable=True, status_code=503, reason="http_error"
            )
            momentum_source = _Source(_momentum_report())
            group_source = _Source(_group_report())
            service, calls = self._service(
                temporary,
                {
                    "momentum": [{"status": 200, "message_id": "m1"}],
                    "sector": [failure, {"status": 200, "message_id": "s1"}],
                },
                momentum=momentum_source,
                groups=group_source,
            )
            first = service.run(send=True, requested_session=TARGET)
            self.assertEqual(first["exit_code"], 1)
            self.assertEqual(momentum_source.calls, 1)
            self.assertEqual(group_source.calls, 1)

            second = service.run(send=True, requested_session=TARGET)
            self.assertEqual(second["exit_code"], 0)
            statuses = {result["channel"]: result["status"] for result in second["results"]}
            self.assertEqual(statuses["momentum"], "SKIPPED_ALREADY_SENT")
            self.assertEqual(statuses["sector-rotation"], "SENT")
            self.assertEqual(momentum_source.calls, 1)
            self.assertEqual(group_source.calls, 1)
            self.assertEqual(len(calls["momentum"]), 1)
            self.assertEqual(len(calls["sector"]), 2)

    def test_uncertain_response_is_not_automatically_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            unknown = DiscordDeliveryError(
                "safe", uncertain=True, reason="response_timeout"
            )
            service, calls = self._service(
                temporary,
                {
                    "momentum": [unknown, {"status": 200, "message_id": "m2"}],
                    "sector": [{"status": 200, "message_id": "s1"}],
                },
            )
            first = service.run(send=True, requested_session=TARGET)
            self.assertEqual(first["exit_code"], 3)
            second = service.run(
                send=True,
                requested_session=TARGET,
                channels=[DigestChannel.MOMENTUM],
            )
            self.assertEqual(second["exit_code"], 3)
            self.assertEqual(len(calls["momentum"]), 1)
            third = service.run(
                send=True,
                requested_session=TARGET,
                channels=[DigestChannel.MOMENTUM],
                retry_unknown=True,
            )
            self.assertEqual(third["exit_code"], 0)
            self.assertEqual(len(calls["momentum"]), 2)

    def test_missing_sector_webhook_does_not_block_momentum(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(
                Path(temporary), sector_rotation_webhook_url=""
            )
            calls = []
            service = PremarketDigestService(
                settings,
                momentum_source=_Source(_momentum_report()),
                group_source=_Source(_group_report()),
                notifier_factory=lambda _: _Notifier(
                    {"status": 200, "message_id": "m1"}, calls
                ),
                now=lambda: NOW,
                calendar=_Calendar(),
            )
            summary = service.run(send=True, requested_session=TARGET)
            statuses = {result["channel"]: result["status"] for result in summary["results"]}
            self.assertEqual(summary["exit_code"], 2)
            self.assertEqual(statuses["momentum"], "SENT")
            self.assertEqual(statuses["sector-rotation"], "FAILED_PERMANENT")
            self.assertEqual(len(calls), 1)

    def test_retryable_channel_takes_exit_priority_over_other_permanent_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(Path(temporary), sector_rotation_webhook_url="")
            retryable = DiscordDeliveryError(
                "safe", retryable=True, status_code=503, reason="http_error"
            )
            outcomes = [retryable, {"status": 200, "message_id": "m1"}]
            calls = []
            service = PremarketDigestService(
                settings,
                momentum_source=_Source(_momentum_report()),
                group_source=_Source(_group_report()),
                notifier_factory=lambda _: _Notifier(outcomes.pop(0), calls),
                now=lambda: NOW,
                calendar=_Calendar(),
            )

            first = service.run(send=True, requested_session=TARGET)
            self.assertEqual(first["exit_code"], 1)
            second = service.run(send=True, requested_session=TARGET)
            self.assertEqual(second["exit_code"], 2)
            statuses = {
                result["channel"]: result["status"] for result in second["results"]
            }
            self.assertEqual(statuses["momentum"], "SENT")
            self.assertEqual(statuses["sector-rotation"], "FAILED_PERMANENT")

    def test_unknown_and_retryable_channels_remain_independent_across_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            unknown = DiscordDeliveryError(
                "safe", uncertain=True, reason="response_timeout"
            )
            retryable = DiscordDeliveryError(
                "safe", retryable=True, reason="connect_timeout"
            )
            service, calls = self._service(
                temporary,
                {
                    "momentum": [unknown],
                    "sector": [retryable, {"status": 200, "message_id": "s1"}],
                },
            )

            first = service.run(send=True, requested_session=TARGET)
            self.assertEqual(first["exit_code"], 1)
            second = service.run(send=True, requested_session=TARGET)
            self.assertEqual(second["exit_code"], 3)
            statuses = {
                result["channel"]: result["status"] for result in second["results"]
            }
            self.assertEqual(statuses["momentum"], "UNKNOWN")
            self.assertEqual(statuses["sector-rotation"], "SENT")
            self.assertEqual(len(calls["momentum"]), 1)
            self.assertEqual(len(calls["sector"]), 2)

    def test_permanent_channel_is_not_reposted_while_other_channel_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            permanent = DiscordDeliveryError(
                "safe",
                retryable=False,
                uncertain=False,
                status_code=400,
                reason="http_error",
            )
            retryable = DiscordDeliveryError(
                "safe", retryable=True, reason="connect_timeout"
            )
            service, calls = self._service(
                temporary,
                {
                    "momentum": [permanent],
                    "sector": [retryable, {"status": 200, "message_id": "s1"}],
                },
            )

            first = service.run(send=True, requested_session=TARGET)
            second = service.run(send=True, requested_session=TARGET)

            self.assertEqual(first["exit_code"], 1)
            self.assertEqual(second["exit_code"], 2)
            statuses = {
                result["channel"]: result["status"] for result in second["results"]
            }
            self.assertEqual(statuses["momentum"], "FAILED_PERMANENT")
            self.assertEqual(statuses["sector-rotation"], "SENT")
            self.assertEqual(len(calls["momentum"]), 1)
            self.assertEqual(len(calls["sector"]), 2)

    def test_invalid_unicode_sector_role_does_not_block_momentum(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(
                Path(temporary), sector_rotation_role_id="١٢٣"
            )
            calls = []
            service = PremarketDigestService(
                settings,
                momentum_source=_Source(_momentum_report()),
                group_source=_Source(_group_report()),
                notifier_factory=lambda _: _Notifier(
                    {"status": 200, "message_id": "m1"}, calls
                ),
                now=lambda: NOW,
                calendar=_Calendar(),
            )
            summary = service.run(send=True, requested_session=TARGET)
            statuses = {
                result["channel"]: result["status"]
                for result in summary["results"]
            }
            self.assertEqual(summary["exit_code"], 2)
            self.assertEqual(statuses["momentum"], "SENT")
            self.assertEqual(statuses["sector-rotation"], "FAILED_PERMANENT")
            self.assertEqual(len(calls), 1)

    def test_sent_outbox_is_not_masked_by_a_later_invalid_role(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = DigestStateStore(Path(temporary) / "state.sqlite3")
            payload = {"content": "frozen", "allowed_mentions": {"parse": []}}
            store.stage(TARGET, DigestChannel.MOMENTUM, SOURCE, payload)
            store.claim(TARGET, DigestChannel.MOMENTUM)
            store.mark_sent(TARGET, DigestChannel.MOMENTUM, "message-1")
            source = _Source(_momentum_report())
            calls = []
            service = PremarketDigestService(
                _settings(Path(temporary), momentum_role_id="١٢٣"),
                state_store=store,
                momentum_source=source,
                notifier_factory=lambda _: _Notifier(
                    {"status": 200, "message_id": "unexpected"}, calls
                ),
                now=lambda: NOW,
                calendar=_Calendar(),
            )
            summary = service.run(
                send=True,
                requested_session=TARGET,
                channels=[DigestChannel.MOMENTUM],
            )
            self.assertEqual(summary["results"][0]["status"], "SKIPPED_ALREADY_SENT")
            self.assertEqual(source.calls, 0)
            self.assertEqual(calls, [])

    def test_interrupted_outbox_is_not_masked_by_a_later_invalid_role(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = DigestStateStore(Path(temporary) / "state.sqlite3")
            payload = {"content": "frozen", "allowed_mentions": {"parse": []}}
            store.stage(TARGET, DigestChannel.MOMENTUM, SOURCE, payload)
            store.claim(TARGET, DigestChannel.MOMENTUM)
            calls = []
            service = PremarketDigestService(
                _settings(Path(temporary), momentum_role_id="١٢٣"),
                state_store=store,
                momentum_source=_Source(_momentum_report()),
                notifier_factory=lambda _: _Notifier(
                    {"status": 200, "message_id": "unexpected"}, calls
                ),
                now=lambda: NOW,
                calendar=_Calendar(),
            )
            summary = service.run(
                send=True,
                requested_session=TARGET,
                channels=[DigestChannel.MOMENTUM],
            )
            self.assertEqual(summary["results"][0]["status"], "UNKNOWN")
            self.assertEqual(calls, [])

    def test_service_recovery_requires_fixed_single_manual_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _ = self._service(
                temporary,
                {"momentum": [], "sector": []},
            )
            invalid = (
                {"send": True, "retry_unknown": True},
                {
                    "send": True,
                    "scheduled": True,
                    "requested_session": TARGET,
                    "channels": [DigestChannel.MOMENTUM],
                    "retry_unknown": True,
                },
                {
                    "send": True,
                    "requested_session": TARGET,
                    "retry_unknown": True,
                },
            )
            for kwargs in invalid:
                with self.subTest(kwargs=kwargs):
                    summary = service.run(**kwargs)
                    self.assertEqual(summary["error_code"], "INVALID_RECOVERY_SCOPE")

    def test_request_timeout_margin_closes_window_before_post(self):
        with tempfile.TemporaryDirectory() as temporary:
            late = datetime(2026, 7, 16, 13, 29, 50, tzinfo=timezone.utc)
            calls = []
            store = DigestStateStore(Path(temporary) / "state.sqlite3")
            service = PremarketDigestService(
                _settings(Path(temporary)),
                state_store=store,
                momentum_source=_Source(_momentum_report()),
                notifier_factory=lambda _: _Notifier(
                    {"status": 200, "message_id": "m1"}, calls
                ),
                now=lambda: late,
                calendar=_Calendar(),
            )
            summary = service.run(
                send=True,
                scheduled=True,
                requested_session=TARGET,
                channels=[DigestChannel.MOMENTUM],
            )
            self.assertEqual(summary["exit_code"], 0)
            self.assertEqual(summary["results"][0]["status"], "SKIPPED_OUTSIDE_WINDOW")
            self.assertEqual(calls, [])
            self.assertEqual(
                store.get(TARGET, DigestChannel.MOMENTUM)["status"], "PENDING"
            )

    def test_calendar_recheck_failure_is_retryable_not_normal_skip(self):
        class FlakyCalendar(_Calendar):
            def __init__(self):
                self.calls = 0

            def is_session(self, label):
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError("calendar backend failed")
                return super().is_session(label)

        with tempfile.TemporaryDirectory() as temporary:
            calls = []
            service = PremarketDigestService(
                _settings(Path(temporary)),
                momentum_source=_Source(_momentum_report()),
                notifier_factory=lambda _: _Notifier(
                    {"status": 200, "message_id": "m1"}, calls
                ),
                now=lambda: NOW,
                calendar=FlakyCalendar(),
            )
            summary = service.run(
                send=True,
                scheduled=True,
                channels=[DigestChannel.MOMENTUM],
            )
            self.assertEqual(summary["exit_code"], 1)
            self.assertEqual(summary["results"][0]["status"], "FAILED_RETRYABLE")
            self.assertEqual(calls, [])

    def test_state_commit_failure_after_message_id_never_auto_reposts(self):
        class FailingCommitStore(DigestStateStore):
            def mark_sent(self, *args, **kwargs):
                raise OSError("simulated ledger failure")

        with tempfile.TemporaryDirectory() as temporary:
            calls = []
            store = FailingCommitStore(Path(temporary) / "state.sqlite3")
            service = PremarketDigestService(
                _settings(Path(temporary)),
                state_store=store,
                momentum_source=_Source(_momentum_report()),
                notifier_factory=lambda _: _Notifier(
                    {"status": 200, "message_id": "m1"}, calls
                ),
                now=lambda: NOW,
                calendar=_Calendar(),
            )
            first = service.run(
                send=True,
                requested_session=TARGET,
                channels=[DigestChannel.MOMENTUM],
            )
            second = service.run(
                send=True,
                requested_session=TARGET,
                channels=[DigestChannel.MOMENTUM],
            )
            self.assertEqual(first["exit_code"], 3)
            self.assertEqual(second["exit_code"], 3)
            self.assertEqual(len(calls), 1)


class CliAndEnvironmentSafetyTests(unittest.TestCase):
    def test_manual_and_unknown_sends_require_explicit_confirmations(self):
        invalid_argv = (
            ["--send"],
            ["--send", "--scheduled", "--session", TARGET],
            ["--send", "--allow-outside-window", "--session", TARGET],
            ["--send", "--allow-outside-window", "--retry-unknown"],
            ["--send", "--allow-outside-window", "--rebuild-failed"],
            [
                "--send",
                "--allow-outside-window",
                "--retry-unknown",
                "--channel",
                "momentum",
            ],
            [
                "--send",
                "--allow-outside-window",
                "--rebuild-failed",
                "--channel",
                "sector-rotation",
            ],
            [
                "--send",
                "--scheduled",
                "--retry-unknown",
                "--channel",
                "momentum",
            ],
            ["--retry-unknown", "--channel", "momentum"],
            ["--rebuild-failed"],
        )
        for argv in invalid_argv:
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    run_premarket_digest_main(argv)

    def test_env_updaters_remove_all_duplicate_target_keys(self):
        for updater in (update_premarket_env_file, update_momentum_env_file):
            with self.subTest(updater=updater.__module__), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "alerts.env"
                path.write_text(
                    "TOKEN=old-first\nKEEP=value\nexport TOKEN=old-last\n",
                    encoding="utf-8",
                )
                updater(path, {"TOKEN": "new", "ADDED": "yes"})
                content = path.read_text(encoding="utf-8")
                self.assertEqual(
                    sum(line.startswith("TOKEN=") for line in content.splitlines()),
                    1,
                )
                self.assertIn("TOKEN=new", content)
                self.assertIn("KEEP=value", content)
                self.assertNotIn("old-last", content)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class DeploymentAndIsolationTests(unittest.TestCase):
    def test_timer_is_dst_safe_and_service_has_retry_guards(self):
        root = Path(__file__).resolve().parents[1]
        timer = (root / "deploy/systemd/quant-premarket-digest.timer").read_text()
        service = (root / "deploy/systemd/quant-premarket-digest.service").read_text()
        self.assertIn("09:20:00 America/New_York", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("--send --scheduled", service)
        self.assertIn("RestartPreventExitStatus=2 3", service)
        self.assertIn("TimeoutStartSec=10min", service)
        self.assertIn("sys.version_info >= (3, 11)", service)
        self.assertIn("/etc/quant/premarket-digest.env", service)
        self.assertIn(
            "--env-file /etc/quant/premarket-digest.env --send --scheduled",
            service,
        )
        self.assertNotIn("/etc/quant/momentum-alerts.env", service)
        self.assertNotIn("discord.com/api/webhooks", service)
        self.assertTrue((root / "deploy/systemd/premarket-digest.env.example").exists())

    def test_upstream_domains_do_not_import_premarket_digest(self):
        root = Path(__file__).resolve().parents[1] / "src"
        offenders = []
        for directory in ("alerts", "group_analytics", "factors", "backtest", "papertrading"):
            path = root / directory
            if not path.exists():
                continue
            for source in path.rglob("*.py"):
                if "premarket_digest" in source.read_text(encoding="utf-8"):
                    offenders.append(str(source))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
