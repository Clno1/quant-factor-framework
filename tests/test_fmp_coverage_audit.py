from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from scripts.audit_fmp_us_equity_coverage import (
    _decision,
    _redact_error,
    _write_report,
)
from scripts.benchmark_us_broad_capacity import (
    _parse_sizes,
    _run_worker,
)
from src.data.fmp import (
    get_canonical_historical_ohlcv,
    get_company_profiles_bulk,
    get_delisted_companies,
    get_eod_bulk,
    get_ipo_calendar,
    get_stock_list,
    get_symbol_changes,
    infer_us_security_asset_type,
)


class FmpDirectoryNormalizationTests(unittest.TestCase):
    def test_asset_type_uses_normalized_us_instrument_suffixes(self):
        self.assertEqual(
            infer_us_security_asset_type(ticker="AAC.UN", name="Acquisition Corp"),
            "UNIT",
        )
        self.assertEqual(
            infer_us_security_asset_type(ticker="AAIC.PB", name="Issuer Inc."),
            "PREFERRED",
        )
        self.assertEqual(
            infer_us_security_asset_type(ticker="BRK.B", name="Berkshire"),
            "STOCK",
        )
        self.assertEqual(
            infer_us_security_asset_type(
                ticker="MLAAW", name="Mountain Lake Acquisition Corp. II Wt"
            ),
            "WARRANT",
        )
        self.assertEqual(
            infer_us_security_asset_type(
                ticker="OACCW", name="Oaktree Acquisition Corp. III"
            ),
            "WARRANT",
        )
        self.assertEqual(
            infer_us_security_asset_type(ticker="AACOU", name="Acquisition Corp"),
            "UNIT",
        )

    @patch("src.data.fmp.get_historical_ohlcv")
    def test_canonical_bars_keep_executable_ohlc_and_total_return_close(self, fetch):
        index = pd.to_datetime(["2026-05-01", "2026-05-04"])
        executable = pd.DataFrame(
            {
                "open": [100.0, 101.0],
                "high": [102.0, 103.0],
                "low": [99.0, 100.0],
                "close": [101.0, 102.0],
                "adj_close": [101.0, 102.0],
                "volume": [1_000.0, 2_000.0],
            },
            index=index,
        )
        total_return = executable.copy()
        total_return.loc[:, "open"] = [98.0, 99.0]
        total_return.loc[:, "close"] = [99.0, 100.0]
        fetch.side_effect = [executable, total_return]

        result = get_canonical_historical_ohlcv(
            "AAA", "2026-05-01", "2026-05-04"
        )

        self.assertEqual(result["open"].tolist(), [100.0, 101.0])
        self.assertEqual(result["close"].tolist(), [101.0, 102.0])
        self.assertEqual(result["adj_close"].tolist(), [99.0, 100.0])
        self.assertEqual(fetch.call_args_list[0].kwargs["dividend_adjusted"], False)
        self.assertEqual(fetch.call_args_list[1].kwargs["dividend_adjusted"], True)

    @patch("src.data.fmp._request")
    def test_profile_bulk_preserves_stable_identifiers_and_asset_type(self, request_mock):
        response = Mock()
        response.headers = {"content-type": "text/csv"}
        response.text = (
            "symbol,companyName,exchange,country,currency,cik,isin,cusip,ipoDate,"
            "sector,industry,isActivelyTrading,isAdr,isEtf,isFund\n"
            "BRK.B,Berkshire,NASDAQ,US,USD,1067983,US0846707026,084670702,"
            "1980-03-17,Financial Services,Insurance,true,false,false,false\n"
            "QQQ,Nasdaq 100 ETF,NYSE Arca,US,USD,,,,1999-03-10,ETF,ETF,"
            "true,false,true,false\n"
            "SPACW,Example Acquisition Corp Warrant,NASDAQ,US,USD,12345,,,"
            "2025-01-02,Financial Services,Shell Companies,true,false,false,false\n"
            "SPACU,Example Acquisition Corp Unit,NASDAQ,US,USD,12345,,,"
            "2025-01-02,Financial Services,Shell Companies,true,false,false,false\n"
        )
        request_mock.return_value = response

        frame = get_company_profiles_bulk(parts=(0,))

        berkshire = frame.set_index("ticker").loc["BRK-B"]
        self.assertEqual(berkshire["cusip"], "084670702")
        self.assertEqual(berkshire["asset_type"], "STOCK")
        self.assertEqual(berkshire["exchange"], "NASDAQ")
        self.assertEqual(
            frame.set_index("ticker").loc["QQQ", "asset_type"],
            "ETF",
        )
        self.assertEqual(
            frame.set_index("ticker").loc["SPACW", "asset_type"],
            "WARRANT",
        )
        self.assertEqual(
            frame.set_index("ticker").loc["SPACU", "asset_type"],
            "UNIT",
        )

    @patch("src.data.fmp._request")
    def test_eod_bulk_accepts_csv_and_normalizes_adjusted_close(self, request_mock):
        response = Mock()
        response.headers = {"content-type": "text/csv"}
        response.text = (
            "date,symbol,open,high,low,close,adjClose,volume\n"
            "2026-08-11,BRK.B,500,510,495,505,504,1000\n"
            "2026-08-11,,1,1,1,1,1,1\n"
        )
        request_mock.return_value = response

        frame = get_eod_bulk("2026-08-11")

        self.assertEqual(frame["ticker"].tolist(), ["BRK-B"])
        self.assertEqual(frame.loc[0, "adj_close"], 504.0)
        self.assertEqual(frame.loc[0, "date"], pd.Timestamp("2026-08-11"))
        self.assertEqual(frame.attrs["invalid_ticker_rows"], 1)

    @patch("src.data.fmp._get")
    def test_stock_list_is_a_normalized_directory(self, get_mock):
        get_mock.return_value = [
            {"symbol": "BRK.B", "companyName": "Berkshire"},
            {"symbol": "BRK.B", "companyName": "Berkshire Hathaway"},
        ]

        frame = get_stock_list()

        self.assertEqual(frame.to_dict("records"), [{
            "ticker": "BRK-B",
            "name": "Berkshire Hathaway",
        }])

    @patch("src.data.fmp._get")
    def test_delisted_company_dates_are_explicit(self, get_mock):
        get_mock.return_value = [{
            "symbol": "OLD",
            "companyName": "Old Co",
            "exchange": "nasdaq",
            "ipoDate": "2010-01-04",
            "delistedDate": "2025-04-02",
        }]

        frame = get_delisted_companies(page=0, limit=100)

        self.assertEqual(frame.loc[0, "ticker"], "OLD")
        self.assertEqual(frame.loc[0, "exchange"], "NASDAQ")
        self.assertEqual(frame.loc[0, "delisted_date"], pd.Timestamp("2025-04-02"))

    @patch("src.data.fmp._get")
    def test_symbol_change_and_ipo_events_are_normalized(self, get_mock):
        get_mock.side_effect = [
            [{
                "date": "2026-01-02",
                "oldSymbol": "OLD.X",
                "newSymbol": "NEW.X",
                "companyName": "Renamed Co",
            }],
            [{
                "date": "2025-03-04",
                "symbol": "IPO",
                "company": "IPO Co",
                "exchange": "Nasdaq",
            }],
        ]

        changes = get_symbol_changes()
        ipos = get_ipo_calendar(start="2025-01-01", end="2026-01-01")

        self.assertEqual(changes.loc[0, "old_ticker"], "OLD-X")
        self.assertEqual(changes.loc[0, "new_ticker"], "NEW-X")
        self.assertEqual(ipos.loc[0, "ticker"], "IPO")
        self.assertEqual(ipos.loc[0, "exchange"], "NASDAQ")


class CoverageAuditContractTests(unittest.TestCase):
    @staticmethod
    def _checks(status: str = "PASS") -> dict[str, dict[str, str]]:
        return {
            name: {"status": status}
            for name in (
                "active_equities",
                "stock_list",
                "eod_bulk",
                "active_eod_samples",
                "delisted_companies",
                "symbol_changes",
                "ipo_calendar",
                "delisted_eod_samples",
            )
        }

    def test_audit_advances_only_when_current_and_historical_samples_pass(self):
        checks = self._checks()
        frames = {
            "active_eod_samples": pd.DataFrame({"available": [True, True]}),
            "delisted_eod_samples": pd.DataFrame({"available": [True, True]}),
        }

        self.assertEqual(
            _decision(checks, frames),
            "GO_TO_CAPACITY_BENCHMARK",
        )

        frames["delisted_eod_samples"].loc[1, "available"] = False
        self.assertEqual(_decision(checks, frames), "PROSPECTIVE_ONLY")

        checks["eod_bulk"]["status"] = "FAIL"
        self.assertEqual(
            _decision(checks, frames),
            "ALTERNATE_PROVIDER_OR_CLIENT_FIX_REQUIRED",
        )

    def test_errors_and_written_reports_do_not_expose_api_key(self):
        secret = "top-secret-api-key"
        self.assertNotIn(secret, _redact_error(RuntimeError(secret), (secret,)))
        report = {
            "target_session": "2026-08-11",
            "decision": "GO_TO_CAPACITY_BENCHMARK",
            "api_key_value_recorded": False,
        }
        with tempfile.TemporaryDirectory() as temporary:
            path, digest_path, digest = _write_report(report, Path(temporary))
            self.assertTrue(path.exists())
            self.assertTrue(digest_path.exists())
            self.assertIn(digest, digest_path.read_text(encoding="ascii"))
            self.assertNotIn(secret, path.read_text(encoding="utf-8"))


class BroadCapacityBenchmarkTests(unittest.TestCase):
    def test_sizes_are_positive_and_deduplicated(self):
        self.assertEqual(_parse_sizes("100,500,100"), [100, 500])
        with self.assertRaises(ValueError):
            _parse_sizes("100,0")

    def test_small_worker_exercises_all_query_stages_and_cleans_scratch(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = _run_worker(
                securities=10,
                sessions=300,
                memory_limit_mb=256,
                threads=1,
                scratch_dir=Path(temporary),
            )

            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["observation_rows"], 3_000)
            self.assertEqual(result["assertions"]["snapshot_rows"], 10)
            self.assertEqual(result["assertions"]["history_rows"], 300)
            self.assertEqual(result["assertions"]["ic_sessions"], 300)
            self.assertEqual(list(Path(temporary).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
