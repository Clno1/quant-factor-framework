from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from src.data.foundation import (
    DataFoundationError,
    DataQualityError,
    MarketDataCatalog,
    MarketDataReader,
    MarketDataWriter,
    _filter_non_xnys_bars,
)
from src.data.access import MarketDataNotReadyError, load_published_bundle
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


class DataFoundationTests(unittest.TestCase):
    def _components(self, root: Path, fetcher):
        catalog = MarketDataCatalog(root / "catalog.duckdb")
        writer = MarketDataWriter(
            catalog=catalog,
            lake_dir=root / "lake",
            fetcher=fetcher,
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
                {"AAA", "BBB", "CCC"},
            )

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
