from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import traceback
import unittest
from unittest import mock
from contextlib import contextmanager

import numpy as np
import pandas as pd

from src.group_analytics.adapters import (
    ClassificationSourceError,
    FMPCurrentClassificationProvider,
    UncertifiedClassificationCacheError,
)
from src.group_analytics.aggregation import aggregate_group_members
from src.group_analytics.artifacts import (
    ArtifactReader,
    ArtifactValidationError,
    ConcurrentWriterError,
    FileGroupArtifactStore,
    RunIdCollisionError,
    compute_parameter_hash,
)
from src.group_analytics.classification import (
    ClassificationValidationError,
    GroupIdMappingValidationError,
    build_counting_units,
    classification_hash,
    load_issuer_overrides,
    load_stable_group_id_mapping,
    normalize_classification_frame,
)
from src.group_analytics.models import (
    ArtifactCombination,
    GroupAnalyticsBundle,
    ReasonCode,
    RunStatus,
)
from src.group_analytics.settings import (
    DailyReturnSettings,
    FreshnessSettings,
    GroupAnalyticsSettings,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXED_NOW = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
GROUP_ID_MAPPING = load_stable_group_id_mapping(
    PROJECT_ROOT / "configs/classifications/fmp_group_ids.yaml"
)


def _normalize(raw: pd.DataFrame, level: str = "sector") -> pd.DataFrame:
    return normalize_classification_frame(
        raw,
        taxonomy="FMP",
        level=level,
        classification_asof="2026-07-15",
        fetched_at="2026-07-15T12:00:00Z",
        group_id_mapping=GROUP_ID_MAPPING,
    )


class ClassificationContractTests(unittest.TestCase):
    def test_versioned_group_id_registry_preserves_reviewed_rename_alias(self):
        canonical = _normalize(
            pd.DataFrame(
                {
                    "symbol": ["TECH"],
                    "sector": ["Technology"],
                    "industry": ["Software - Application"],
                }
            )
        )
        renamed = _normalize(
            pd.DataFrame(
                {
                    "symbol": ["TECH"],
                    "sector": ["Information Technology"],
                    "industry": ["Software - Application"],
                }
            )
        )

        self.assertEqual(canonical.loc[0, "group_id"], renamed.loc[0, "group_id"])
        self.assertEqual(canonical.loc[0, "group_id"], "fmp:sector:s010")
        self.assertEqual(
            canonical.loc[0, "group_id_mapping_version"],
            GROUP_ID_MAPPING.version,
        )

        with self.assertRaises(GroupIdMappingValidationError):
            _normalize(
                pd.DataFrame(
                    {
                        "symbol": ["NEW"],
                        "sector": ["Unreviewed Sector"],
                        "industry": ["Software - Application"],
                    }
                )
            )

    def test_classification_hash_is_independent_of_row_order(self):
        raw = pd.DataFrame(
            {
                "symbol": ["MSFT", "AAPL", "GOOG"],
                "companyName": ["Microsoft", "Apple", "Alphabet"],
                "sector": ["Technology", "Technology", "Communication Services"],
                "industry": [
                    "Software - Application",
                    "Computer Hardware",
                    "Internet Content & Information",
                ],
            }
        )

        first = _normalize(raw)
        shuffled = _normalize(raw.sample(frac=1, random_state=17))

        self.assertEqual(classification_hash(first), classification_hash(shuffled))

    def test_each_level_is_mutually_exclusive_and_missing_group_is_explicit(self):
        missing = _normalize(
            pd.DataFrame(
                {
                    "symbol": ["AAPL", "NCLS"],
                    "sector": ["Technology", None],
                    "industry": ["Computer Hardware", None],
                }
            )
        ).set_index("ticker")
        self.assertFalse(bool(missing.at["NCLS", "is_classified"]))
        self.assertTrue(pd.isna(missing.at["NCLS", "group_id"]))
        self.assertEqual(
            missing.at["NCLS", "reason_codes"],
            [ReasonCode.MISSING_CLASSIFICATION.value],
        )

        conflicts = {
            "sector": pd.DataFrame(
                {
                    "symbol": ["DUPE", "DUPE"],
                    "sector": ["Technology", "Healthcare"],
                    "industry": ["Software - Application", "Software - Application"],
                }
            ),
            "sub_industry": pd.DataFrame(
                {
                    "symbol": ["DUPE", "DUPE"],
                    "sector": ["Technology", "Technology"],
                    "industry": ["Software - Application", "Semiconductors"],
                }
            ),
        }
        for level, payload in conflicts.items():
            with self.subTest(level=level), self.assertRaises(
                ClassificationValidationError
            ):
                _normalize(payload, level=level)

    def test_alphabet_override_deduplicates_and_uses_only_pre_asof_market_cap(self):
        classification = _normalize(
            pd.DataFrame(
                {
                    "symbol": ["GOOG", "GOOGL"],
                    "companyName": ["Alphabet C", "Alphabet A"],
                    "sector": ["Communication Services"] * 2,
                    "industry": ["Internet Content & Information"] * 2,
                }
            )
        )
        # The as-of row deliberately reverses the winner.  It must not affect
        # representative selection or share-class weights for today's return.
        market_cap = pd.DataFrame(
            {"GOOG": [300.0, 1.0], "GOOGL": [100.0, 1_000.0]},
            index=pd.to_datetime(["2026-07-14", "2026-07-15"]),
        )

        units, diagnostics = build_counting_units(
            classification,
            security_returns={"GOOG": 0.10, "GOOGL": -0.20},
            asof="2026-07-15",
            overrides=load_issuer_overrides(
                PROJECT_ROOT / "configs/classifications/issuer_overrides.yaml"
            ),
            market_cap=market_cap,
        )

        self.assertEqual(len(units), 1)
        unit = units.iloc[0]
        self.assertEqual(unit["security_id"], "override:alphabet")
        self.assertEqual(unit["counting_unit_id"], "override:alphabet")
        self.assertEqual(unit["member_tickers"], ["GOOG", "GOOGL"])
        self.assertEqual(unit["representative_ticker"], "GOOG")
        self.assertEqual(unit["selection_method"], "SHARE_CLASS_MARKET_CAP")
        self.assertEqual(unit["selection_data_through"], "2026-07-14")
        self.assertAlmostEqual(unit["raw_return_1d"], 0.025)
        self.assertIn(ReasonCode.SHARE_CLASS_DEDUPED.value, unit["reason_codes"])
        self.assertEqual(diagnostics["n_security_rows"], 2)
        self.assertEqual(diagnostics["n_counting_units"], 1)


class FMPClassificationCacheTests(unittest.TestCase):
    @staticmethod
    def _payload() -> pd.DataFrame:
        return pd.DataFrame(
            {
                "symbol": ["AAPL", "MSFT"],
                "companyName": ["Apple", "Microsoft"],
                "sector": ["Technology", "Technology"],
                "industry": ["Computer Hardware", "Software - Application"],
            }
        )

    def test_second_read_uses_certified_cache_without_fetching(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = 0

            def fetch() -> pd.DataFrame:
                nonlocal calls
                calls += 1
                return self._payload()

            provider = FMPCurrentClassificationProvider(
                cache_root=directory,
                fetcher=fetch,
                now=lambda: FIXED_NOW,
            )
            first = provider.snapshot(
                universe="SP500", taxonomy="FMP", level="sector", asof="latest"
            )
            second = provider.snapshot(
                universe="SP500", taxonomy="FMP", level="sector", asof="latest"
            )

            self.assertEqual(calls, 1)
            self.assertEqual(first.classification_hash, second.classification_hash)
            self.assertTrue(second.source_path.is_file())
            self.assertTrue(second.source_path.with_name("provenance.json").is_file())
            for diagnostic in second.diagnostics:
                self.assertFalse(any(key.endswith("_path") for key in diagnostic))
                if "provenance_locator" in diagnostic:
                    self.assertFalse(Path(diagnostic["provenance_locator"]).is_absolute())

    def test_legacy_parquet_without_provenance_is_never_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_root = Path(directory) / "FMP/SP500/sector"
            legacy_root.mkdir(parents=True)
            self._payload().to_parquet(legacy_root / "legacy.parquet", index=False)

            def failed_fetch() -> pd.DataFrame:
                raise RuntimeError("network unavailable")

            provider = FMPCurrentClassificationProvider(
                cache_root=directory,
                fetcher=failed_fetch,
                now=lambda: FIXED_NOW,
            )
            with self.assertRaises(ClassificationSourceError) as raised:
                provider.snapshot(
                    universe="SP500", taxonomy="FMP", level="sector", asof="latest"
                )

            self.assertIn(
                ReasonCode.UNKNOWN_LEGACY_CACHE.value,
                raised.exception.details["reason_codes"],
            )

    def test_older_forced_fetch_cannot_roll_latest_pointer_backward(self):
        with tempfile.TemporaryDirectory() as directory:
            newer = FMPCurrentClassificationProvider(
                cache_root=directory,
                fetcher=self._payload,
                now=lambda: datetime(2026, 7, 15, 13, tzinfo=timezone.utc),
            )
            older = FMPCurrentClassificationProvider(
                cache_root=directory,
                fetcher=self._payload,
                now=lambda: datetime(2026, 7, 15, 12, tzinfo=timezone.utc),
            )

            newest = newer.snapshot(
                universe="SP500", taxonomy="FMP", level="sector", asof="latest", force=True
            )
            retained = older.snapshot(
                universe="SP500", taxonomy="FMP", level="sector", asof="latest", force=True
            )

            self.assertEqual(retained.fetched_at, newest.fetched_at)
            self.assertIn(
                "CLASSIFICATION_CACHE_NEWER_SNAPSHOT_RETAINED",
                {item["code"] for item in retained.diagnostics},
            )
            pointer = json.loads(
                (
                    Path(directory)
                    / "FMP/SP500/sector/latest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(pointer["updated_at"], newest.fetched_at)

    def test_file_lock_serializes_concurrent_latest_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            barrier = threading.Barrier(2)

            def fetch() -> pd.DataFrame:
                barrier.wait(timeout=5)
                return self._payload()

            providers = [
                FMPCurrentClassificationProvider(
                    cache_root=directory,
                    fetcher=fetch,
                    now=lambda hour=hour: datetime(
                        2026, 7, 15, hour, tzinfo=timezone.utc
                    ),
                )
                for hour in (12, 13)
            ]
            active = 0
            maximum_active = 0
            active_guard = threading.Lock()
            original = FMPCurrentClassificationProvider._newest_certified_snapshot

            def observed_selection(provider, **kwargs):
                nonlocal active, maximum_active
                with active_guard:
                    active += 1
                    maximum_active = max(maximum_active, active)
                try:
                    time.sleep(0.05)
                    return original(provider, **kwargs)
                finally:
                    with active_guard:
                        active -= 1

            def publish(provider: FMPCurrentClassificationProvider):
                return provider.snapshot(
                    universe="SP500",
                    taxonomy="FMP",
                    level="sector",
                    asof="latest",
                    force=True,
                )

            with mock.patch.object(
                FMPCurrentClassificationProvider,
                "_newest_certified_snapshot",
                new=observed_selection,
            ), ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(publish, providers))

            self.assertEqual(maximum_active, 1)
            self.assertTrue(
                (Path(directory) / "FMP/SP500/sector/.publish.lock").is_file()
            )
            pointer = json.loads(
                (Path(directory) / "FMP/SP500/sector/latest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(pointer["updated_at"], "2026-07-15T13:00:00Z")
            self.assertEqual(
                max(result.fetched_at for result in results),
                "2026-07-15T13:00:00Z",
            )

    def test_corrupt_latest_snapshot_recovers_previous_certified_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            older = FMPCurrentClassificationProvider(
                cache_root=directory,
                fetcher=self._payload,
                now=lambda: datetime(2026, 7, 15, 10, tzinfo=timezone.utc),
            ).snapshot(
                universe="SP500", taxonomy="FMP", level="sector", asof="latest", force=True
            )
            newer = FMPCurrentClassificationProvider(
                cache_root=directory,
                fetcher=self._payload,
                now=lambda: datetime(2026, 7, 15, 11, tzinfo=timezone.utc),
            ).snapshot(
                universe="SP500", taxonomy="FMP", level="sector", asof="latest", force=True
            )
            self.assertNotEqual(older.source_path, newer.source_path)
            newer.source_path.write_bytes(b"deliberately corrupt parquet")

            def failed_fetch() -> pd.DataFrame:
                raise RuntimeError("FMP_API_KEY=do-not-log /private/secret")

            recovered = FMPCurrentClassificationProvider(
                cache_root=directory,
                cache_max_age_hours=0.5,
                fetcher=failed_fetch,
                now=lambda: datetime(2026, 7, 15, 14, tzinfo=timezone.utc),
                allow_verified_stale_cache=True,
            ).snapshot(
                universe="SP500", taxonomy="FMP", level="sector", asof="latest"
            )

            self.assertEqual(recovered.fetched_at, older.fetched_at)
            self.assertTrue(recovered.fallback)
            codes = {item["code"] for item in recovered.diagnostics}
            self.assertIn("CLASSIFICATION_CACHE_POINTER_RECOVERED", codes)
            self.assertIn("FMP_FETCH_FAILED_VERIFIED_STALE_CACHE_USED", codes)
            serialized = json.dumps(recovered.diagnostics)
            self.assertNotIn(directory, serialized)
            self.assertNotIn("do-not-log", serialized)
            pointer = json.loads(
                (Path(directory) / "FMP/SP500/sector/latest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(pointer["locator"], older.source_path.parent.relative_to(
                Path(directory) / "FMP/SP500/sector"
            ).as_posix())

    def test_corrupt_cache_and_fetch_failure_do_not_leak_paths_or_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            combo = Path(directory) / "FMP/SP500/sector"
            combo.mkdir(parents=True)
            (combo / "latest.json").write_text(
                '{"locator":"/private/secret/FMP_API_KEY=top-secret"}',
                encoding="utf-8",
            )

            def failed_fetch() -> pd.DataFrame:
                raise RuntimeError(
                    f"FMP_API_KEY=top-secret cache={directory}/credentials.json"
                )

            provider = FMPCurrentClassificationProvider(
                cache_root=directory,
                fetcher=failed_fetch,
                now=lambda: FIXED_NOW,
                allow_verified_stale_cache=True,
            )
            with self.assertRaises(ClassificationSourceError) as raised:
                provider.snapshot(
                    universe="SP500", taxonomy="FMP", level="sector", asof="latest"
                )

            rendered = "".join(
                traceback.format_exception(
                    type(raised.exception),
                    raised.exception,
                    raised.exception.__traceback__,
                )
            )
            serialized = json.dumps(raised.exception.details)
            for forbidden in (directory, "top-secret", "credentials.json", "/private/secret"):
                self.assertNotIn(forbidden, rendered)
                self.assertNotIn(forbidden, serialized)
            self.assertEqual(
                raised.exception.details["cache_error_type"],
                UncertifiedClassificationCacheError.__name__,
            )


def _valid_bundle() -> GroupAnalyticsBundle:
    source = pd.DataFrame(
        {
            "group_id": ["fmp:sector:technology"] * 5,
            "group_name": ["Technology"] * 5,
            "level": ["sector"] * 5,
            "security_id": [f"security:T{i}" for i in range(5)],
            "counting_unit_id": [f"security:T{i}" for i in range(5)],
            "ticker": [f"T{i}" for i in range(5)],
            "name": [f"Test {i}" for i in range(5)],
            "raw_return_1d": [0.01, 0.02, 0.03, 0.04, 0.05],
            "reason_codes": [[] for _ in range(5)],
        }
    )
    aggregated = aggregate_group_members(source)
    return GroupAnalyticsBundle(
        metrics=pd.DataFrame([aggregated.metric]),
        members=aggregated.members,
        contributions=aggregated.contributions,
        diagnostics={"nan_probe": np.nan},
        manifest={
            "asof": "2026-07-15",
            "input_fingerprint": "sha256:test-input",
            "nan_probe": np.nan,
        },
        run={"nan_probe": np.nan},
    )


class ArtifactContractTests(unittest.TestCase):
    def test_output_subdir_cannot_escape_output_root(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = GroupAnalyticsSettings(
                enabled=True,
                output_root=Path(directory),
                output_subdir="safe/../../escaped",
            )
            with self.assertRaises(ArtifactValidationError):
                FileGroupArtifactStore(settings)

            overlap = replace(settings, output_subdir="_group_analytics_attempts")
            with self.assertRaises(ArtifactValidationError):
                FileGroupArtifactStore(overlap)

    def test_storage_symlinks_cannot_escape_output_root(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            outside_root = Path(outside)
            os.symlink(outside_root, root / "_group_analytics_attempts")
            with self.assertRaises(ArtifactValidationError):
                FileGroupArtifactStore(
                    GroupAnalyticsSettings(enabled=True, output_root=root)
                )
            self.assertEqual(list(outside_root.iterdir()), [])

        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            outside_root = Path(outside)
            store = FileGroupArtifactStore(
                GroupAnalyticsSettings(enabled=True, output_root=root)
            )
            store.artifact_root.mkdir(parents=True)
            os.symlink(outside_root, store.artifact_root / "SP500")
            with self.assertRaises(ArtifactValidationError):
                store.combination_dir(
                    ArtifactCombination("SP500", "FMP", "sector", "eod")
                )
            self.assertEqual(list(outside_root.iterdir()), [])

        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            outside_root = Path(outside)
            store = FileGroupArtifactStore(
                GroupAnalyticsSettings(enabled=True, output_root=root)
            )
            combination = ArtifactCombination("SP500", "FMP", "sector", "eod")
            combo_dir = store.combination_dir(combination)
            combo_dir.mkdir(parents=True)
            os.symlink(outside_root, combo_dir / ".staging")
            outcome = store.publish(
                run_id="staging-symlink",
                combination=combination,
                bundle=_valid_bundle(),
            )
            self.assertEqual(outcome.status, RunStatus.FAILED)
            self.assertEqual(list(outside_root.iterdir()), [])

    def test_parameter_hash_excludes_runtime_config_and_tracks_algorithm_config(self):
        base = GroupAnalyticsSettings(output_root=Path("outputs-a"))
        runtime_only = replace(
            base,
            enabled=True,
            output_root=Path("outputs-b"),
            freshness=FreshnessSettings(eod_publish_sla_minutes=999),
        )
        algorithm_change = replace(
            base,
            daily_return=replace(base.daily_return, winsorize_n=4.0),
        )

        self.assertEqual(
            compute_parameter_hash(base), compute_parameter_hash(runtime_only)
        )
        self.assertNotEqual(
            compute_parameter_hash(base), compute_parameter_hash(algorithm_change)
        )
        self.assertNotEqual(
            compute_parameter_hash(base),
            compute_parameter_hash(replace(base, benchmark="QQQ")),
        )

    def test_publish_failure_and_idempotent_skip_never_replace_success_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = GroupAnalyticsSettings(
                enabled=True,
                output_root=Path(directory),
                daily_return=DailyReturnSettings(),
            )
            store = FileGroupArtifactStore(settings)
            combination = ArtifactCombination("SP500", "FMP", "sector", "eod")

            success = store.publish(
                run_id="success-one",
                combination=combination,
                bundle=_valid_bundle(),
            )
            self.assertEqual(success.status, RunStatus.SUCCESS)
            self.assertTrue(success.published)
            pointer_path = store.combination_dir(combination) / "latest_success.json"
            original_pointer = pointer_path.read_bytes()
            self.assertEqual(json.loads(original_pointer)["run_id"], "success-one")

            failed = store.record_failure(
                "failed-two",
                combination,
                RuntimeError("deliberate failure"),
            )
            self.assertEqual(failed.status, RunStatus.FAILED)
            self.assertEqual(pointer_path.read_bytes(), original_pointer)

            with mock.patch.object(
                store,
                "_write_frame",
                side_effect=OSError("injected parquet write failure"),
            ):
                injected = store.publish(
                    run_id="failed-in-staging",
                    combination=combination,
                    bundle=_valid_bundle(),
                )
            self.assertEqual(injected.status, RunStatus.FAILED)
            self.assertEqual(pointer_path.read_bytes(), original_pointer)
            self.assertFalse(
                (store.combination_dir(combination) / "runs" / "failed-in-staging").exists()
            )

            skipped = store.record_skipped(
                "skipped-three", combination, matched_run_id="success-one"
            )
            self.assertEqual(skipped.status, RunStatus.SKIPPED)
            self.assertFalse(skipped.published)
            self.assertEqual(pointer_path.read_bytes(), original_pointer)

            run_directory = Path(directory) / success.artifact_locator
            for name in ("manifest.json", "diagnostics.json", "run.json"):
                raw = (run_directory / name).read_text(encoding="utf-8")
                self.assertNotIn("NaN", raw)
                self.assertIsNone(json.loads(raw)["nan_probe"])

            skipped_attempt = store.load_attempt("skipped-three")
            self.assertEqual(skipped_attempt["last_attempt_status"], "SUCCESS")
            self.assertEqual(
                skipped_attempt["execution_result"], "SKIPPED_IDEMPOTENT"
            )
            self.assertIsNone(skipped_attempt["artifact_locator"])

    def test_older_attempt_cannot_replace_newer_same_session_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = GroupAnalyticsSettings(enabled=True, output_root=Path(directory))
            store = FileGroupArtifactStore(settings)
            combination = ArtifactCombination("SP500", "FMP", "sector", "eod")

            store.new_run_id("older-start")
            store.record_running("older-start", combination)
            store.new_run_id("newer-start")
            store.record_running("newer-start", combination)
            newer = store.publish(
                run_id="newer-start",
                combination=combination,
                bundle=_valid_bundle(),
            )
            older = store.publish(
                run_id="older-start",
                combination=combination,
                bundle=_valid_bundle(),
            )

            self.assertEqual(newer.status, RunStatus.SUCCESS)
            self.assertEqual(older.status, RunStatus.FAILED)
            self.assertEqual(older.error["code"], "OUT_OF_ORDER_PUBLICATION")
            pointer = json.loads(
                (store.combination_dir(combination) / "latest_success.json").read_text()
            )
            self.assertEqual(pointer["run_id"], "newer-start")
            self.assertEqual(
                store.load_last_attempt(combination)["run_id"],
                "newer-start",
            )

    def test_terminal_attempts_are_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = GroupAnalyticsSettings(enabled=True, output_root=Path(directory))
            store = FileGroupArtifactStore(settings)
            combination = ArtifactCombination("SP500", "FMP", "sector", "eod")
            store.publish(
                run_id="terminal-success",
                combination=combination,
                bundle=_valid_bundle(),
            )
            with self.assertRaises(RunIdCollisionError):
                store.record_failure(
                    "terminal-success",
                    combination,
                    RuntimeError("must not overwrite success"),
                )
            success = store.load_attempt("terminal-success")
            self.assertEqual(success["last_attempt_status"], "SUCCESS")
            self.assertIsNotNone(success["artifact_locator"])

            store.record_failure(
                "terminal-failed",
                combination,
                RuntimeError("first failure"),
            )
            with self.assertRaises(RunIdCollisionError):
                store.publish(
                    run_id="terminal-failed",
                    combination=combination,
                    bundle=_valid_bundle(),
                )
            failed = store.load_attempt("terminal-failed")
            self.assertEqual(failed["last_attempt_status"], "FAILED")

    def test_artifact_dates_and_snapshot_must_match_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = GroupAnalyticsSettings(enabled=True, output_root=Path(directory))
            store = FileGroupArtifactStore(settings)
            combination = ArtifactCombination("SP500", "FMP", "sector", "eod")

            wrong_date = _valid_bundle()
            wrong_date.metrics["date"] = "2020-01-02"
            outcome = store.publish(
                run_id="wrong-date",
                combination=combination,
                bundle=wrong_date,
            )
            self.assertEqual(outcome.status, RunStatus.FAILED)
            self.assertEqual(outcome.error["code"], "ARTIFACT_VALIDATION_FAILED")

            wrong_snapshot = _valid_bundle()
            wrong_snapshot.members["snapshot_id"] = "LIVE"
            outcome = store.publish(
                run_id="wrong-snapshot",
                combination=combination,
                bundle=wrong_snapshot,
            )
            self.assertEqual(outcome.status, RunStatus.FAILED)
            self.assertEqual(outcome.error["code"], "ARTIFACT_VALIDATION_FAILED")

    def test_writer_lock_failure_does_not_leave_running_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = GroupAnalyticsSettings(enabled=True, output_root=Path(directory))
            store = FileGroupArtifactStore(settings)
            combination = ArtifactCombination("SP500", "FMP", "sector", "eod")
            run_id = store.new_run_id("lock-timeout")

            @contextmanager
            def broken_lock():
                raise ConcurrentWriterError("injected lock timeout")
                yield

            with mock.patch.object(store, "_combo_lock", return_value=broken_lock()):
                with self.assertRaises(ConcurrentWriterError):
                    store.record_running(run_id, combination)

            attempt = store.load_attempt(run_id)
            self.assertEqual(attempt["last_attempt_status"], "FAILED")
            self.assertEqual(attempt["error"]["code"], "CONCURRENT_WRITER")

    def test_latest_read_is_pinned_to_one_immutable_run(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = GroupAnalyticsSettings(enabled=True, output_root=Path(directory))
            store = FileGroupArtifactStore(settings)
            reader = ArtifactReader(settings)
            combination = ArtifactCombination("SP500", "FMP", "sector", "eod")
            store.publish(run_id="pinned-one", combination=combination, bundle=_valid_bundle())
            pointer_path = store.combination_dir(combination) / "latest_success.json"
            pointer_one = pointer_path.read_bytes()
            store.publish(run_id="pinned-two", combination=combination, bundle=_valid_bundle())
            pointer_two = pointer_path.read_bytes()
            pointer_path.write_bytes(pointer_one)
            original_resolve = reader.resolve_latest

            def resolve_then_switch(value):
                resolved = original_resolve(value)
                pointer_path.write_bytes(pointer_two)
                return resolved

            with mock.patch.object(reader, "resolve_latest", side_effect=resolve_then_switch):
                loaded = reader.load_latest(combination)

            self.assertEqual(loaded.run_id, "pinned-one")
            self.assertEqual(loaded.manifest["run_id"], "pinned-one")
            self.assertEqual(loaded.run["run_id"], "pinned-one")
            self.assertEqual(loaded.path.name, "pinned-one")


if __name__ == "__main__":
    unittest.main()
