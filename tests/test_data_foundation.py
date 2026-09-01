from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from src.data.foundation import (
    DataFoundationError,
    DataQualityError,
    MarketDataCatalog,
    MarketDataReader,
    MarketDataWriter,
    _filter_non_xnys_bars,
    _merge_candidate_bars,
    _rebase_parent_to_fetched_scale,
    _single_metadata_policy,
    validate_pit_bar_coverage,
)
from src.data.access import (
    MarketDataNotReadyError,
    load_published_bundle,
    load_published_daily_data,
    validate_daily_data_contract,
)
from src.utils.market_calendar import latest_publishable_xnys_session


def _universe() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": ["AAA", "BBB"],
            "name": ["Alpha", "Beta"],
            "sector": ["Technology", "Financials"],
            "sub_industry": ["Software", "Banks"],
        }
    )


def _bars(ticker: str, dates: list[str]) -> pd.DataFrame:
    offset = 0.0 if ticker == "AAA" else 10.0
    index = pd.to_datetime(dates)
    base = pd.Series(
        [100.0 + offset + i for i in range(len(index))],
        index=index,
    )
    return pd.DataFrame(
        {
            "open": base,
            "high": base + 2.0,
            "low": base - 1.0,
            "close": base + 1.0,
            "adj_close": base + 1.0,
            "volume": 1_000.0,
        },
        index=index,
    )


def test_full_rebuild_merge_preserves_numeric_bar_dtypes():
    fetched = pd.DataFrame({
        "date": pd.to_datetime(["2026-07-20", "2026-07-21"]),
        "ticker": ["AAA", "AAA"],
        "open": [100.0, 101.0],
        "high": [102.0, 103.0],
        "low": [99.0, 100.0],
        "close": [101.0, 102.0],
        "adj_close": [101.0, 102.0],
        "volume": [1_000.0, 1_100.0],
    })
    empty_parent = pd.DataFrame(columns=fetched.columns)

    candidate = _merge_candidate_bars(empty_parent, fetched)

    assert candidate is not fetched
    assert all(
        pd.api.types.is_numeric_dtype(candidate[column])
        for column in ("open", "high", "low", "close", "adj_close", "volume")
    )


def test_pit_bar_coverage_replays_compact_removal_events():
    membership = pd.DataFrame([
        {
            "date": "2026-07-20",
            "ticker": ticker,
            "active": True,
            "snapshot_type": "MONTH_END",
        }
        for ticker in ("AAA", "BBB")
    ] + [{
        "date": "2026-07-21",
        "ticker": "BBB",
        "active": False,
        "snapshot_type": "FORCED_EXIT",
    }])
    bars = pd.DataFrame([
        {"date": "2026-07-20", "ticker": "AAA"},
        {"date": "2026-07-20", "ticker": "BBB"},
        {"date": "2026-07-21", "ticker": "AAA"},
    ])
    bars["date"] = pd.to_datetime(bars["date"])

    checks = validate_pit_bar_coverage(
        bars,
        membership,
        start=pd.Timestamp("2026-07-20"),
        target=pd.Timestamp("2026-07-22"),
        min_daily_coverage=0.95,
    )

    daily = next(check for check in checks if check.name == "pit_daily_bar_coverage")
    assert not daily.passed
    assert daily.observed["worst_session"] == "2026-07-22"
    assert daily.observed["active"] == 1


def test_metadata_temporal_policy_is_preserved_for_audit():
    metadata = pd.DataFrame({
        "classification_policy": [
            "LATEST_KNOWN_BACKFILL_NOT_PIT",
            "LATEST_KNOWN_BACKFILL_NOT_PIT",
        ]
    })
    assert _single_metadata_policy(
        metadata,
        "classification_policy",
    ) == "LATEST_KNOWN_BACKFILL_NOT_PIT"


def test_incremental_rebase_prevents_false_adjustment_boundary_return():
    previous = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"]),
        "ticker": "AAA",
        "open": [100.0, 101.0, 102.0],
        "high": [101.0, 102.0, 103.0],
        "low": [99.0, 100.0, 101.0],
        "close": [100.0, 101.0, 102.0],
        "adj_close": [90.0, 91.0, 92.0],
        "volume": [1_000.0, 1_100.0, 1_200.0],
    })
    fetched = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-06", "2026-01-07"]),
        "ticker": "AAA",
        "open": [102.0, 103.0],
        "high": [103.0, 104.0],
        "low": [101.0, 102.0],
        "close": [102.0, 103.0],
        # A newly known dividend revised all pre-event adjusted closes by 10%.
        "adj_close": [82.8, 83.7],
        "volume": [1_200.0, 1_300.0],
    })

    rebased, audit = _rebase_parent_to_fetched_scale(previous, fetched)
    combined = (
        pd.concat([rebased, fetched], ignore_index=True)
        .drop_duplicates(["date", "ticker"], keep="last")
        .sort_values("date")
    )

    assert audit[0]["anchor_date"] == "2026-01-06"
    assert audit[0]["scales"]["adj_close"] == pytest.approx(0.9)
    assert rebased.loc[0, "close"] == pytest.approx(100.0)
    assert rebased.loc[0, "adj_close"] == pytest.approx(81.0)
    boundary_return = combined.set_index("date")["adj_close"].pct_change().loc[
        pd.Timestamp("2026-01-06")
    ]
    assert boundary_return == pytest.approx(92.0 / 91.0 - 1.0)


def test_incremental_rebase_requires_an_overlap_anchor():
    previous = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-02"]),
        "ticker": ["AAA"],
        "open": [100.0],
        "high": [101.0],
        "low": [99.0],
        "close": [100.0],
        "adj_close": [100.0],
        "volume": [1_000.0],
    })
    fetched = previous.assign(date=pd.Timestamp("2026-01-05"))

    with pytest.raises(DataFoundationError, match="no overlap anchor"):
        _rebase_parent_to_fetched_scale(previous, fetched)


def test_incremental_rebase_rejects_nonuniform_overlap_revision():
    previous = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"]),
        "ticker": "AAA",
        "open": [100.0, 101.0, 102.0],
        "high": [101.0, 102.0, 103.0],
        "low": [99.0, 100.0, 101.0],
        "close": [100.0, 101.0, 102.0],
        "adj_close": [100.0, 101.0, 102.0],
        "volume": [1_000.0, 1_100.0, 1_200.0],
    })
    fetched = previous.loc[previous["date"].ge("2026-01-05")].copy()
    fetched.loc[fetched["date"].eq("2026-01-05"), "adj_close"] *= 0.90
    fetched.loc[fetched["date"].eq("2026-01-06"), "adj_close"] *= 0.80

    with pytest.raises(DataFoundationError, match="non-uniform adj_close revision"):
        _rebase_parent_to_fetched_scale(previous, fetched)


class DataFoundationTests(unittest.TestCase):
    def _components(self, root: Path, fetcher):
        catalog = MarketDataCatalog(root / "catalog.duckdb")
        writer = MarketDataWriter(
            catalog=catalog,
            lake_dir=root / "lake",
            fetcher=fetcher,
            fetcher_semantics_source="TEST_CANONICAL_FIXTURE",
        )
        reader = MarketDataReader(catalog=catalog)
        return catalog, writer, reader

    def test_publish_and_read_wide_tables(self):
        calls: list[tuple[str, str, str]] = []

        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            calls.append((ticker, start, end))
            return _bars(ticker, ["2026-07-17", "2026-07-20"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, writer, reader = self._components(root, fetcher)
            result = writer.update_universe(
                "TEST",
                target_session="2026-07-20",
                universe_frame=_universe(),
                initial_start="2026-07-17",
                workers=2,
            )

            self.assertEqual(result.status, "PUBLISHED")
            self.assertEqual(len(calls), 2)
            latest = catalog.latest_version("TEST")
            self.assertIsNotNone(latest)
            self.assertEqual(latest.version_id, result.version.version_id)
            self.assertEqual(latest.target_coverage, 1.0)

            wide = reader.load_wide_tables("TEST", require_open=True)
            self.assertEqual(list(wide["open"].columns), ["AAA", "BBB"])
            self.assertEqual(wide["open"].index.max(), pd.Timestamp("2026-07-20"))
            self.assertEqual(
                wide["sector"].loc["AAA", "sector"],
                "Technology",
            )
            self.assertTrue(wide["returns"].iloc[0].isna().all())

            filtered = reader.load_bars(
                "TEST",
                tickers=["bbb"],
                start="2026-07-20",
                end="2026-07-20",
                version=result.version,
            )
            self.assertEqual(filtered[["date", "ticker"]].to_dict("records"), [
                {"date": pd.Timestamp("2026-07-20"), "ticker": "BBB"},
            ])
            self.assertTrue(
                reader.load_bars("TEST", tickers=[], version=result.version).empty
            )

    def test_reader_rejects_tampered_version_files(self):
        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            return _bars(ticker, ["2026-07-20"])

        for target in ("bars_path", "universe_path", "manifest_path"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _, writer, reader = self._components(root, fetcher)
                result = writer.update_universe(
                    "TEST",
                    target_session="2026-07-20",
                    universe_frame=_universe(),
                    initial_start="2026-07-20",
                )
                path = Path(getattr(result.version, target))
                path.write_bytes(path.read_bytes() + b"tampered")
                with self.assertRaises(DataFoundationError, msg=target):
                    reader.require_version("TEST", result.version.version_id)

    def test_reader_rejects_tampered_membership_at_version_resolution(self):
        membership = pd.DataFrame(
            {
                "date": pd.Timestamp("2026-07-20"),
                "ticker": ["AAA", "BBB"],
                "active": True,
            }
        )

        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            return _bars(ticker, ["2026-07-20"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, writer, reader = self._components(root, fetcher)
            result = writer.update_universe(
                "TEST",
                target_session="2026-07-20",
                universe_frame=_universe(),
                initial_start="2026-07-20",
                membership_frame=membership,
                membership_source="unit-test",
            )
            path = Path(result.version.membership_path)
            path.write_bytes(path.read_bytes() + b"tampered")

            with self.assertRaises(DataFoundationError, msg="membership_path"):
                reader.require_version("TEST", result.version.version_id)

    def test_old_catalog_schema_is_readable_but_missing_v2_hashes(self):
        import duckdb

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.duckdb"
            connection = duckdb.connect(str(path))
            connection.execute(
                """
                CREATE TABLE dataset_versions (
                    version_id VARCHAR PRIMARY KEY,
                    run_id VARCHAR NOT NULL,
                    universe VARCHAR NOT NULL,
                    provider VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    target_session DATE NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    row_count BIGINT NOT NULL,
                    ticker_count BIGINT NOT NULL,
                    min_date DATE,
                    max_date DATE,
                    target_coverage DOUBLE NOT NULL,
                    bars_path VARCHAR NOT NULL,
                    universe_path VARCHAR NOT NULL,
                    membership_path VARCHAR,
                    membership_checksum_sha256 VARCHAR,
                    manifest_path VARCHAR NOT NULL,
                    checksum_sha256 VARCHAR NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE published_versions (
                    universe VARCHAR PRIMARY KEY,
                    version_id VARCHAR NOT NULL,
                    published_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO dataset_versions VALUES (
                    'legacy-v1', 'legacy-run', 'SP500', 'fmp', 'PUBLISHED',
                    DATE '2026-07-20', current_timestamp, 10, 2,
                    DATE '2026-07-17', DATE '2026-07-20', 1.0,
                    'bars.parquet', 'universe.parquet', NULL, NULL,
                    'manifest.json', 'bars-sha'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO published_versions
                VALUES ('SP500', 'legacy-v1', current_timestamp)
                """
            )
            connection.close()

            version = MarketDataCatalog(path).latest_version("SP500")
            self.assertIsNotNone(version)
            self.assertIsNone(version.universe_checksum_sha256)
            self.assertIsNone(version.manifest_checksum_sha256)

    def test_historical_metadata_uses_explicit_unknown_instead_of_null(self):
        membership = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    ["2026-07-17", "2026-07-17", "2026-07-20", "2026-07-20"]
                ),
                "ticker": ["AAA", "OLD", "AAA", "BBB"],
                "active": [True, True, True, True],
            }
        )

        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            return _bars(ticker, ["2026-07-17", "2026-07-20"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, writer, reader = self._components(root, fetcher)
            with patch(
                "src.data.foundation.point_in_time_required",
                return_value=True,
            ):
                result = writer.update_universe(
                    "TEST",
                    target_session="2026-07-20",
                    universe_frame=_universe(),
                    initial_start="2026-07-17",
                    membership_frame=membership,
                    membership_source="unit-test",
                )
            metadata = reader.load_universe(
                "TEST", current_only=False, version=result.version
            ).set_index("ticker")
            self.assertEqual(metadata.loc["OLD", "sector"], "UNKNOWN")
            self.assertEqual(metadata.loc["OLD", "source"], "explicit_unknown")
            self.assertFalse(bool(metadata.loc["OLD", "classification_known"]))
            self.assertTrue(pd.notna(metadata.loc["OLD", "effective_from"]))

    def test_observed_membership_does_not_penalize_pre_listing_sessions(self):
        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            dates = ["2026-07-17", "2026-07-20"]
            if ticker == "BBB":
                dates = ["2026-07-20"]
            return _bars(ticker, dates)

        membership = pd.DataFrame(
            {
                "date": pd.Timestamp("2026-07-17"),
                "ticker": ["AAA", "BBB"],
                "active": True,
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, writer, reader = self._components(root, fetcher)
            result = writer.update_universe(
                "WATCHLIST_TEST",
                target_session="2026-07-20",
                universe_frame=_universe(),
                initial_start="2026-07-17",
                membership_frame=membership,
                membership_source="sqlite_data_request:test",
                derive_membership_from_bars=True,
                workers=2,
            )

            self.assertEqual(result.status, "PUBLISHED")
            frozen = reader.load_membership("WATCHLIST_TEST")
            first = frozen.loc[frozen["date"].eq(pd.Timestamp("2026-07-17"))]
            last = frozen.loc[frozen["date"].eq(pd.Timestamp("2026-07-20"))]
            self.assertEqual(
                dict(zip(first["ticker"], first["active"], strict=True)),
                {"AAA": True, "BBB": False},
            )
            self.assertTrue(last["active"].all())

    def test_non_xnys_vendor_rows_are_excluded_and_audited(self):
        bars = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-07-19", "2026-07-20"]),
                "ticker": ["AAA", "AAA"],
                "open": [100.0, 101.0],
                "high": [102.0, 103.0],
                "low": [99.0, 100.0],
                "close": [101.0, 102.0],
                "adj_close": [101.0, 102.0],
                "volume": [1_000.0, 2_000.0],
            }
        )

        filtered, check = _filter_non_xnys_bars(bars)

        self.assertTrue(check.passed)
        self.assertEqual(check.observed["excluded_rows"], 1)
        self.assertEqual(check.observed["excluded_tickers"], 1)
        self.assertEqual(filtered["date"].tolist(), [pd.Timestamp("2026-07-20")])

    def test_same_target_is_idempotent_without_force(self):
        call_count = 0

        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            nonlocal call_count
            call_count += 1
            return _bars(ticker, ["2026-07-20"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, writer, _ = self._components(root, fetcher)
            first = writer.update_universe(
                "TEST",
                target_session="2026-07-20",
                universe_frame=_universe(),
                initial_start="2026-07-20",
            )
            second = writer.update_universe(
                "TEST",
                target_session="2026-07-20",
                universe_frame=_universe(),
                initial_start="2026-07-20",
            )

            self.assertEqual(first.status, "PUBLISHED")
            self.assertEqual(second.status, "NOOP")
            self.assertEqual(call_count, 2)
            self.assertEqual(
                first.version.version_id,
                second.version.version_id,
            )

    def test_rejected_candidate_does_not_advance_published_pointer(self):
        phase = "complete"

        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame | None:
            if phase == "complete":
                return _bars(ticker, ["2026-07-20"])
            if ticker == "AAA":
                return _bars(ticker, ["2026-07-20", "2026-07-21"])
            return _bars(ticker, ["2026-07-20"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, writer, reader = self._components(root, fetcher)
            first = writer.update_universe(
                "TEST",
                target_session="2026-07-20",
                universe_frame=_universe(),
                initial_start="2026-07-20",
            )
            phase = "partial"

            with self.assertRaises(DataQualityError):
                writer.update_universe(
                    "TEST",
                    target_session="2026-07-21",
                    universe_frame=_universe(),
                    initial_start="2026-07-20",
                )

            latest = catalog.latest_version("TEST")
            self.assertEqual(latest.version_id, first.version.version_id)
            self.assertEqual(latest.target_session.isoformat(), "2026-07-20")
            bars = reader.load_bars("TEST")
            self.assertEqual(bars["date"].max(), pd.Timestamp("2026-07-20"))

    def test_incremental_version_keeps_old_version_immutable(self):
        target_dates = ["2026-07-20"]

        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            return _bars(ticker, target_dates)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, writer, _ = self._components(root, fetcher)
            first = writer.update_universe(
                "TEST",
                target_session="2026-07-20",
                universe_frame=_universe(),
                initial_start="2026-07-20",
            )
            old_path = Path(first.version.bars_path)
            old_bytes = old_path.read_bytes()

            target_dates[:] = ["2026-07-20", "2026-07-21"]
            second = writer.update_universe(
                "TEST",
                target_session="2026-07-21",
                universe_frame=_universe(),
                initial_start="2026-07-20",
            )

            self.assertNotEqual(first.version.version_id, second.version.version_id)
            self.assertEqual(old_path.read_bytes(), old_bytes)
            self.assertTrue(Path(second.version.bars_path).exists())
            self.assertEqual(
                catalog.latest_version("TEST").version_id,
                second.version.version_id,
            )

    def test_incremental_version_backfills_an_earlier_requested_start(self):
        requested_starts: list[str] = []
        target_dates = ["2026-07-20"]

        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            requested_starts.append(start)
            return _bars(ticker, target_dates)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, writer, reader = self._components(root, fetcher)
            writer.update_universe(
                "TEST",
                target_session="2026-07-20",
                universe_frame=_universe(),
                initial_start="2026-07-20",
            )

            requested_starts.clear()
            target_dates[:] = ["2026-01-02", "2026-07-20", "2026-07-21"]
            writer.update_universe(
                "TEST",
                target_session="2026-07-21",
                universe_frame=_universe(),
                initial_start="2026-01-02",
            )

            self.assertEqual(len(requested_starts), 2)
            self.assertEqual(set(requested_starts), {"2026-01-02"})
            self.assertEqual(
                reader.load_bars("TEST")["date"].min(),
                pd.Timestamp("2026-01-02"),
            )

    def test_reader_can_bind_an_exact_historical_version(self):
        target_dates = ["2026-07-20"]

        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            return _bars(ticker, target_dates)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, writer, reader = self._components(root, fetcher)
            first = writer.update_universe(
                "TEST",
                target_session="2026-07-20",
                universe_frame=_universe(),
                initial_start="2026-07-20",
            )
            target_dates[:] = ["2026-07-20", "2026-07-21"]
            writer.update_universe(
                "TEST",
                target_session="2026-07-21",
                universe_frame=_universe(),
                initial_start="2026-07-20",
            )

            frozen = reader.load_bars(
                "TEST",
                version=first.version.version_id,
            )
            self.assertEqual(frozen["date"].max(), pd.Timestamp("2026-07-20"))
            with self.assertRaises(DataFoundationError):
                reader.require_version("OTHER", first.version.version_id)

    def test_explicit_membership_is_frozen_with_published_bars(self):
        membership = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    ["2026-07-17", "2026-07-17", "2026-07-20", "2026-07-20"]
                ),
                "ticker": ["AAA", "BBB", "AAA", "BBB"],
                "active": True,
            }
        )
        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            return _bars(ticker, ["2026-07-17", "2026-07-20"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, writer, reader = self._components(root, fetcher)
            result = writer.update_universe(
                "TEST",
                target_session="2026-07-20",
                universe_frame=_universe(),
                initial_start="2026-07-17",
                membership_frame=membership,
                membership_source="unit-test",
            )

            bars = reader.load_bars("TEST", version=result.version.version_id)
            self.assertEqual(
                set(bars["date"]),
                {pd.Timestamp("2026-07-17"), pd.Timestamp("2026-07-20")},
            )
            pd.testing.assert_frame_equal(
                reader.load_membership(
                    "TEST",
                    version=result.version.version_id,
                ),
                membership.sort_values(["date", "ticker"]).reset_index(drop=True),
            )

    def test_published_bundle_records_and_enforces_version_contract(self):
        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            return _bars(ticker, ["2026-07-20"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, writer, reader = self._components(root, fetcher)
            result = writer.update_universe(
                "TEST",
                target_session="2026-07-20",
                universe_frame=_universe(),
                initial_start="2026-07-20",
            )
            with patch(
                "src.data.access._expected_session",
                return_value=pd.Timestamp("2026-07-20"),
            ):
                bundle = load_published_bundle(
                    requested_universe="TEST",
                    tickers=["AAA", "BBB"],
                    start="2026-07-20",
                    end="2026-07-20",
                    require_open=True,
                    exact_universe=True,
                    dataset_version_id=result.version.version_id,
                    reader=reader,
                )
                self.assertEqual(
                    bundle.contract.dataset_version_id,
                    result.version.version_id,
                )
                self.assertTrue(bundle.contract.coverage["passed"])
                with self.assertRaises(MarketDataNotReadyError):
                    load_published_bundle(
                        requested_universe="TEST",
                        tickers=["AAA", "MISSING"],
                        end="2026-07-20",
                        dataset_version_id=result.version.version_id,
                        reader=reader,
                    )

    def test_daily_bundle_is_bulk_loaded_and_stays_pinned_after_pointer_moves(self):
        target_dates = ["2026-07-20"]

        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            return _bars(ticker, target_dates)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, writer, reader = self._components(root, fetcher)
            first = writer.update_universe(
                "TEST",
                target_session="2026-07-20",
                universe_frame=_universe(),
                initial_start="2026-07-20",
            )
            reader.load_bars = Mock(wraps=reader.load_bars)
            with patch(
                "src.data.access._expected_session",
                return_value=pd.Timestamp("2026-07-20"),
            ):
                bundle = load_published_daily_data(
                    requested_universe="TEST",
                    dataset_version_id=first.version.version_id,
                    reader=reader,
                )

            self.assertEqual(reader.load_bars.call_count, 1)
            self.assertEqual(set(bundle.bars["ticker"]), {"AAA", "BBB"})
            self.assertEqual(
                bundle.contract.dataset_version_id,
                first.version.version_id,
            )

            target_dates[:] = ["2026-07-20", "2026-07-21"]
            second = writer.update_universe(
                "TEST",
                target_session="2026-07-21",
                universe_frame=_universe(),
                initial_start="2026-07-20",
            )

            self.assertNotEqual(
                bundle.contract.dataset_version_id,
                second.version.version_id,
            )
            self.assertEqual(bundle.bars["date"].max(), pd.Timestamp("2026-07-20"))
            validated = validate_daily_data_contract(
                bundle.contract,
                reader=reader,
            )
            self.assertEqual(validated.version_id, first.version.version_id)

            tampered = bundle.contract.to_dict()
            tampered["bars_sha256"] = "sha256:tampered"
            with self.assertRaises(MarketDataNotReadyError):
                validate_daily_data_contract(tampered, reader=reader)

    def test_runtime_factor_bundle_rejects_short_membership_history(self):
        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            return _bars(ticker, ["2026-07-20"])

        membership = pd.DataFrame(
            {
                "date": pd.Timestamp("2026-07-20"),
                "ticker": ["AAA", "BBB"],
                "active": True,
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, writer, reader = self._components(root, fetcher)
            result = writer.update_universe(
                "TEST",
                target_session="2026-07-20",
                universe_frame=_universe(),
                initial_start="2026-07-20",
                membership_frame=membership,
                membership_source="unit-test",
            )
            with patch(
                "src.data.access._expected_session",
                return_value=pd.Timestamp("2026-07-20"),
            ):
                with self.assertRaises(MarketDataNotReadyError) as raised:
                    load_published_bundle(
                        requested_universe="TEST",
                        tickers=["AAA", "BBB"],
                        start="2026-07-20",
                        end="2026-07-20",
                        exact_universe=True,
                        factor_ids=["MOM_12M"],
                        dataset_version_id=result.version.version_id,
                        reader=reader,
                    )
            self.assertIn(
                "insufficient_membership_history",
                raised.exception.coverage.failures,
            )

    def test_explicit_history_contract_rejects_short_market_dataset(self):
        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            return _bars(ticker, ["2026-07-20"])

        membership = pd.DataFrame(
            {
                "date": pd.Timestamp("2026-07-20"),
                "ticker": ["AAA", "BBB"],
                "active": True,
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, writer, reader = self._components(root, fetcher)
            result = writer.update_universe(
                "TEST",
                target_session="2026-07-20",
                universe_frame=_universe(),
                initial_start="2026-07-20",
                membership_frame=membership,
                membership_source="unit-test",
            )
            with patch(
                "src.data.access._expected_session",
                return_value=pd.Timestamp("2026-07-20"),
            ):
                with self.assertRaises(MarketDataNotReadyError) as raised:
                    load_published_bundle(
                        requested_universe="TEST",
                        start="2026-01-01",
                        end="2026-07-20",
                        exact_universe=True,
                        required_history_start="2026-01-01",
                        dataset_version_id=result.version.version_id,
                        reader=reader,
                    )

            self.assertIn(
                "insufficient_bar_history",
                raised.exception.coverage.failures,
            )
            self.assertIn(
                "insufficient_membership_history",
                raised.exception.coverage.failures,
            )

    def test_pit_membership_is_frozen_inside_published_version(self):
        membership = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    [
                        "2026-07-17",
                        "2026-07-17",
                        "2026-07-20",
                        "2026-07-20",
                    ]
                ),
                "ticker": ["AAA", "CCC", "AAA", "BBB"],
                "active": [True, True, True, True],
            }
        )

        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            return _bars(ticker, ["2026-07-17", "2026-07-20"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "TEST.parquet"
            membership.to_parquet(source, index=False)
            catalog, writer, reader = self._components(root, fetcher)
            with patch(
                "src.data.foundation.load_point_in_time_membership",
                return_value=(membership, source),
            ):
                result = writer.update_universe(
                    "SP500",
                    target_session="2026-07-20",
                    universe_frame=_universe(),
                    initial_start="2026-07-17",
                )

            self.assertIsNotNone(result.version.membership_path)
            self.assertIsNotNone(result.version.membership_checksum_sha256)
            frozen = reader.load_membership("SP500")
            pd.testing.assert_frame_equal(
                frozen,
                membership.sort_values(["date", "ticker"]).reset_index(drop=True),
            )
            self.assertEqual(
                set(reader.load_wide_tables("SP500")["close"].columns),
                {"AAA", "BBB", "CCC", "SPY"},
            )
            manifest = reader.verify_version(
                result.version,
                require_price_semantics=True,
            )
            self.assertEqual(manifest["support_tickers"], ["SPY"])

    def test_pit_event_ledger_is_frozen_and_authenticated(self):
        membership = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-07-17", "2026-07-17"]),
                "ticker": ["AAA", "BBB"],
                "active": [True, True],
            }
        )
        events = pd.DataFrame(
            {
                "effective_date": pd.to_datetime(["2026-07-17"]),
                "added_ticker": ["AAA"],
                "removed_ticker": ["OLD"],
                "reason": ["Acquired by Alpha"],
            }
        )

        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            return _bars(ticker, ["2026-07-17"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "SP500.parquet"
            membership.to_parquet(source, index=False)
            _, writer, reader = self._components(root, fetcher)
            with (
                patch(
                    "src.data.foundation.load_point_in_time_membership",
                    return_value=(membership, source),
                ),
                patch(
                    "src.data.foundation._load_membership_events_for_version",
                    return_value=events,
                ),
            ):
                result = writer.update_universe(
                    "SP500",
                    target_session="2026-07-17",
                    universe_frame=_universe(),
                    initial_start="2026-07-17",
                )

            frozen = reader.load_membership_events(
                "SP500",
                version=result.version,
            )
            self.assertIsNotNone(frozen)
            self.assertEqual(frozen.iloc[0]["removed_ticker"], "OLD")
            manifest = reader.verify_version(result.version)
            event_path = Path(result.version.manifest_path).parent / str(
                manifest["membership_events_path"]
            )
            with event_path.open("ab") as stream:
                stream.write(b"modified")
            with self.assertRaisesRegex(
                DataFoundationError,
                "checksum mismatch",
            ):
                reader.verify_version(result.version)

    def test_required_pit_missing_fails_before_publication(self):
        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            return _bars(ticker, ["2026-07-20"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, writer, _ = self._components(root, fetcher)
            with patch(
                "src.data.foundation.load_point_in_time_membership",
                return_value=(None, None),
            ):
                with self.assertRaises(DataFoundationError):
                    writer.update_universe(
                        "SP500",
                        target_session="2026-07-20",
                        universe_frame=_universe(),
                        initial_start="2026-07-20",
                    )
            self.assertIsNone(catalog.latest_version("SP500"))

    def test_missing_historical_member_bars_rejects_pit_version(self):
        membership = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    [
                        "2026-07-17",
                        "2026-07-17",
                        "2026-07-20",
                        "2026-07-20",
                    ]
                ),
                "ticker": ["AAA", "CCC", "AAA", "BBB"],
                "active": [True, True, True, True],
            }
        )

        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            dates = (
                ["2026-07-20"]
                if ticker == "CCC"
                else ["2026-07-17", "2026-07-20"]
            )
            return _bars(ticker, dates)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "SP500.parquet"
            membership.to_parquet(source, index=False)
            catalog, writer, _ = self._components(root, fetcher)
            with patch(
                "src.data.foundation.load_point_in_time_membership",
                return_value=(membership, source),
            ):
                with self.assertRaises(DataQualityError):
                    writer.update_universe(
                        "SP500",
                        target_session="2026-07-20",
                        universe_frame=_universe(),
                        initial_start="2026-07-17",
                    )
            self.assertIsNone(catalog.latest_version("SP500"))

    def test_reader_rejects_modified_published_parquet(self):
        def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
            return _bars(ticker, ["2026-07-20"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, writer, reader = self._components(root, fetcher)
            result = writer.update_universe(
                "TEST",
                target_session="2026-07-20",
                universe_frame=_universe(),
                initial_start="2026-07-20",
            )
            with Path(result.version.bars_path).open("ab") as stream:
                stream.write(b"modified")

            with self.assertRaisesRegex(
                DataFoundationError,
                "checksum mismatch",
            ):
                reader.load_bars("TEST")


class _FakeCalendar:
    sessions = pd.DatetimeIndex(["2026-07-02", "2026-07-03"])
    closes = {
        pd.Timestamp("2026-07-02"): pd.Timestamp(
            "2026-07-02 20:00:00", tz="UTC"
        ),
        pd.Timestamp("2026-07-03"): pd.Timestamp(
            "2026-07-03 17:00:00", tz="UTC"
        ),
    }

    def sessions_in_range(self, start: str, end: str) -> pd.DatetimeIndex:
        return self.sessions

    def session_close(self, session: pd.Timestamp) -> pd.Timestamp:
        return self.closes[pd.Timestamp(session).tz_localize(None)]


class MarketCalendarPublicationTests(unittest.TestCase):
    def test_close_delay_respects_early_close(self):
        result = latest_publishable_xnys_session(
            now=datetime(2026, 7, 3, 18, 30, tzinfo=timezone.utc),
            delay_minutes=120,
            calendar=_FakeCalendar(),
        )
        self.assertEqual(result, pd.Timestamp("2026-07-02"))


if __name__ == "__main__":
    unittest.main()
