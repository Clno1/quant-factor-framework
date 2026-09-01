from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from src.group_analytics.artifacts import ArtifactReader, FileGroupArtifactStore
from src.group_analytics.classification import (
    classification_hash,
    load_stable_group_id_mapping,
    normalize_classification_frame,
)
from src.group_analytics.models import (
    ClassificationSnapshot,
    EODMarketSnapshot,
    NoSuccessfulRunError,
    RunRequest,
    RunStatus,
)
from src.group_analytics.service import GroupAnalyticsService
from src.group_analytics.settings import GroupAnalyticsSettings


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GROUP_ID_MAPPING = load_stable_group_id_mapping(
    PROJECT_ROOT / "configs/classifications/fmp_group_ids.yaml"
)


class _FakeXNYSCalendar:
    _sessions = pd.DatetimeIndex(
        ["2026-07-10", "2026-07-13", "2026-07-14", "2026-07-15"]
    )

    def sessions_in_range(self, start, end):
        start_label = pd.Timestamp(start).tz_localize(None).normalize()
        end_label = pd.Timestamp(end).tz_localize(None).normalize()
        return self._sessions[
            (self._sessions >= start_label) & (self._sessions <= end_label)
        ]

    def session_close(self, session):
        label = pd.Timestamp(session).tz_localize(None).normalize()
        if label not in self._sessions:
            raise ValueError(f"not a session: {label}")
        return label.tz_localize("UTC") + pd.Timedelta(hours=20)

    def previous_session(self, session):
        label = pd.Timestamp(session).tz_localize(None).normalize()
        location = self._sessions.get_loc(label)
        if location == 0:
            raise ValueError("no previous session")
        return self._sessions[location - 1]


def _classification_frame() -> pd.DataFrame:
    raw = pd.DataFrame(
        {
            "symbol": ["AAPL", "GOOG", "GOOGL", "META", "MSFT", "NVDA"],
            "name": [
                "Apple Inc.",
                "Alphabet Class C",
                "Alphabet Class A",
                "Meta Platforms",
                "Microsoft",
                "NVIDIA",
            ],
            "sector": ["Information Technology"] * 6,
            "industry": ["Technology Hardware"] * 6,
            "asset_type": ["STOCK"] * 6,
        }
    )
    return normalize_classification_frame(
        raw,
        taxonomy="FMP",
        level="sector",
        classification_asof="2026-07-15",
        fetched_at="2026-07-15T20:30:00Z",
        group_id_mapping=GROUP_ID_MAPPING,
    )


class _MemoryClassificationProvider:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.calls = 0

    def snapshot(self, **_kwargs) -> ClassificationSnapshot:
        self.calls += 1
        frame = self.frame.copy(deep=True)
        return ClassificationSnapshot(
            frame=frame,
            provider="TEST_MEMORY",
            taxonomy_version="test-taxonomy-v1",
            classification_hash=classification_hash(frame),
            classification_asof="2026-07-15",
            fetched_at="2026-07-15T20:30:00Z",
        )


class _MemoryMarketProvider:
    def __init__(self, *, missing_on_latest: set[str] | None = None):
        self.missing_on_latest = set(missing_on_latest or ())
        self.calls = 0
        self.last_diagnostics = {"provider": "TEST_MEMORY"}

    def snapshot(self, *, symbols, benchmark, asof=None, force=False) -> EODMarketSnapshot:
        del force
        self.calls += 1
        sessions = pd.to_datetime(
            ["2026-07-10", "2026-07-13", "2026-07-14", "2026-07-15"]
        )
        prices: dict[str, list[float]] = {}
        volumes: dict[str, list[float]] = {}
        for offset, symbol in enumerate(symbols):
            base = 100.0 + offset * 10.0
            prices[symbol] = [base * 0.98, base * 0.99, base, base * 1.01]
            # Prefer GOOGL over GOOG using only information available through t-1.
            liquidity = 5_000_000.0 if symbol == "GOOGL" else 1_000_000.0
            volumes[symbol] = [liquidity] * len(sessions)
        adj_close = pd.DataFrame(prices, index=sessions, dtype=float)
        volume = pd.DataFrame(volumes, index=sessions, dtype=float)
        for symbol in self.missing_on_latest.intersection(symbols):
            adj_close.loc[pd.Timestamp("2026-07-15"), symbol] = np.nan
        benchmark_adj_close = pd.DataFrame(
            {benchmark: [490.0, 495.0, 500.0, 505.0]},
            index=sessions,
            dtype=float,
        )
        return EODMarketSnapshot(
            adj_close=adj_close,
            volume=volume,
            benchmark_adj_close=benchmark_adj_close,
        )


class _CapturingArtifactStore(FileGroupArtifactStore):
    def __init__(self, settings):
        super().__init__(settings)
        self.published_bundles = []

    def publish(self, *, run_id, combination, bundle, dry_run=False):
        self.published_bundles.append(bundle)
        return super().publish(
            run_id=run_id,
            combination=combination,
            bundle=bundle,
            dry_run=dry_run,
        )


class GroupAnalyticsServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.settings = replace(
            GroupAnalyticsSettings(),
            enabled=True,
            output_root=Path(self._temporary_directory.name),
        )
        self.classification_provider = _MemoryClassificationProvider(
            _classification_frame()
        )
        self.market_provider = _MemoryMarketProvider()
        self.store = _CapturingArtifactStore(self.settings)
        self.reader = ArtifactReader(self.settings)
        self.calendar = _FakeXNYSCalendar()

    def _service(self, *, market_provider=None) -> GroupAnalyticsService:
        return GroupAnalyticsService(
            self.settings,
            classification_provider=self.classification_provider,
            market_provider=market_provider or self.market_provider,
            artifact_store=self.store,
            now=lambda: datetime(2026, 7, 15, 21, 0, tzinfo=timezone.utc),
            exchange_calendar=self.calendar,
        )

    def _publish_baseline(self):
        outcome = self._service().run(RunRequest())
        self.assertEqual(outcome.status, RunStatus.SUCCESS, outcome.error)
        self.assertTrue(outcome.published)
        return outcome

    def test_successful_formal_run_is_published_and_readable(self):
        outcome = self._publish_baseline()

        loaded = self.reader.load_latest(outcome.combination)

        self.assertEqual(loaded.run_id, outcome.run_id)
        self.assertEqual(loaded.manifest["asof"], "2026-07-15")
        self.assertEqual(loaded.manifest["session_status"], "FINAL")
        self.assertFalse(loaded.manifest["research_only"])
        self.assertTrue(
            {
                "schema_version", "algorithm_version", "run_id", "parameter_hash",
                "runtime_config_hash", "code_version", "git_commit", "dirty_hash",
                "generated_at", "asof", "snapshot_id", "snapshot_time", "mode",
                "universe", "universe_version", "taxonomy", "taxonomy_level",
                "taxonomy_version", "classification_asof", "classification_hash",
                "classification_provider", "group_id_mapping_version", "fallback", "fetched_at",
                "pit_universe_applied", "pit_classification_applied", "counting_unit",
                "issuer_dedupe_status", "issuer_overrides_applied",
                "issuer_override_count", "issuer_override_version", "weight_source",
                "benchmark", "input_paths", "input_mtimes", "input_max_date",
                "input_row_counts", "input_fingerprint", "quality_summary",
                "output_files", "file_hashes", "row_counts",
            }.issubset(loaded.manifest)
        )
        self.assertFalse(loaded.metrics.empty)
        self.assertEqual(set(loaded.metrics["run_id"]), {outcome.run_id})
        self.assertAlmostEqual(
            loaded.contributions["contribution"].sum(),
            loaded.metrics["robust_ew_return_1d"].sum(),
        )

    def test_identical_second_run_is_skipped_and_latest_stays_pinned(self):
        first = self._publish_baseline()

        second = self._service().run(RunRequest())
        loaded = self.reader.load_latest(first.combination)
        attempt = self.reader.load_attempt(second.run_id)

        self.assertEqual(second.status, RunStatus.SKIPPED, second.error)
        self.assertFalse(second.published)
        self.assertEqual(loaded.run_id, first.run_id)
        self.assertEqual(attempt["execution_result"], "SKIPPED_IDEMPOTENT")
        self.assertEqual(attempt["matched_run_id"], first.run_id)

    def test_market_coverage_failure_keeps_previous_success_pointer(self):
        first = self._publish_baseline()
        incomplete_market = _MemoryMarketProvider(
            missing_on_latest={"META", "MSFT"}
        )

        failed = self._service(market_provider=incomplete_market).run(RunRequest())
        loaded = self.reader.load_latest(first.combination)

        self.assertEqual(failed.status, RunStatus.FAILED)
        self.assertEqual(failed.error["code"], "INPUT_COVERAGE_BELOW_GATE")
        self.assertEqual(loaded.run_id, first.run_id)

    def test_limit_implies_dry_run_and_does_not_switch_latest(self):
        first = self._publish_baseline()

        limited = self._service().run(RunRequest(limit=3))
        loaded = self.reader.load_latest(first.combination)
        attempt = self.reader.load_attempt(limited.run_id)

        self.assertEqual(limited.status, RunStatus.SUCCESS, limited.error)
        self.assertTrue(limited.dry_run)
        self.assertFalse(limited.published)
        self.assertEqual(loaded.run_id, first.run_id)
        self.assertTrue(attempt["dry_run"])
        self.assertIsNone(attempt["artifact_locator"])

    def test_explicit_history_is_research_only_dry_run_with_reason_code(self):
        outcome = self._service().run(RunRequest(asof="2026-07-14"))

        self.assertEqual(outcome.status, RunStatus.SUCCESS, outcome.error)
        self.assertTrue(outcome.dry_run)
        self.assertFalse(outcome.published)
        bundle = self.store.published_bundles[-1]
        self.assertTrue(bundle.manifest["research_only"])
        self.assertTrue(
            bundle.metrics["reason_codes"].map(
                lambda codes: "STATIC_MAPPING_RESEARCH_ONLY" in codes
            ).all()
        )
        with self.assertRaises(NoSuccessfulRunError):
            self.reader.load_latest(outcome.combination)

    def test_strict_pit_fails_before_reading_providers(self):
        outcome = self._service().run(RunRequest(strict_pit=True))

        self.assertEqual(outcome.status, RunStatus.FAILED)
        self.assertEqual(outcome.error["code"], "PIT_DATA_UNAVAILABLE")
        self.assertEqual(self.classification_provider.calls, 0)
        self.assertEqual(self.market_provider.calls, 0)

    def test_alphabet_share_classes_publish_as_one_counting_unit(self):
        outcome = self._publish_baseline()

        loaded = self.reader.load_latest(outcome.combination)
        alphabet = loaded.members[
            loaded.members["counting_unit_id"].eq("override:alphabet")
        ]

        self.assertEqual(len(loaded.members), 5)
        self.assertEqual(len(alphabet), 1)
        self.assertEqual(alphabet.iloc[0]["security_id"], "override:alphabet")
        self.assertEqual(alphabet.iloc[0]["ticker"], "GOOGL")
        self.assertIn("SHARE_CLASS_DEDUPED", alphabet.iloc[0]["reason_codes"])

    def test_etfs_are_excluded_from_stage_one_expected_members_and_diagnosed(self):
        frame = _classification_frame()
        etf = frame.iloc[[0]].copy()
        etf["ticker"] = "XLK"
        etf["security_id"] = "fmp:ticker:XLK"
        etf["counting_unit_id"] = "security:XLK"
        etf["name"] = "Technology Select Sector SPDR Fund"
        etf["asset_type"] = "ETF"
        frame = pd.concat([frame, etf], ignore_index=True)
        self.classification_provider = _MemoryClassificationProvider(frame)

        outcome = self._publish_baseline()
        loaded = self.reader.load_latest(outcome.combination)

        self.assertNotIn("XLK", set(loaded.members["ticker"]))
        self.assertEqual(int(loaded.manifest["quality_summary"]["n_expected"]), 5)
        self.assertTrue(
            any(
                item.get("ticker") == "XLK" and item.get("code") == "ETF_EXCLUDED"
                for item in loaded.diagnostics["classification_diagnostics"]
            )
        )


if __name__ == "__main__":
    unittest.main()
