from __future__ import annotations

import unittest
from unittest.mock import Mock, patch
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd

from src.breakouts import BreakoutFilters, build_intraday_snapshot, evaluate_daily_setup
from src.breakouts import scan_cache
from src.data.fmp import get_intraday_ohlcv, get_us_active_equities


class DailyBreakoutScannerTests(unittest.TestCase):
    @patch("src.data.fmp.requests.get")
    @patch("src.data.fmp.get_api_key", return_value="secret-key")
    def test_fmp_auth_uses_header_not_query_string(self, _key_mock, request_mock):
        response = Mock()
        response.status_code = 200
        response.json.return_value = []
        request_mock.return_value = response

        from src.data.fmp import _get

        _get("/stock-list", params={"limit": 1})

        _, kwargs = request_mock.call_args
        self.assertEqual(kwargs["headers"], {"apikey": "secret-key"})
        self.assertEqual(kwargs["params"], {"limit": 1})

    def test_daily_metrics_use_qullamaggie_adr_formula(self):
        index = pd.bdate_range("2025-01-02", periods=100)
        close = np.concatenate([
            np.linspace(50.0, 90.0, 65),
            np.linspace(82.0, 92.0, 35),
        ])
        frame = pd.DataFrame({
            "open": close * 0.995,
            "high": close * 1.03,
            "low": close * 0.97,
            "close": close,
            "volume": np.full(len(index), 2_000_000.0),
        }, index=index)

        row = evaluate_daily_setup(
            frame,
            ticker="TEST",
            filters=BreakoutFilters(
                min_return_20d=-99,
                min_adr_20d=0,
                min_dollar_volume=0,
                min_avg_dollar_volume=0,
            ),
        )

        self.assertIsNotNone(row)
        assert row is not None
        expected_adr = (1.03 / 0.97 - 1.0) * 100.0
        self.assertAlmostEqual(row["adr_20d"], expected_adr, places=8)
        self.assertTrue(row["base_pass"])
        self.assertEqual(set(row["setup_checks"]), {
            "prior_move", "consolidation", "ma50_distance", "ma_trend",
            "tight_range", "higher_lows", "volume_dryup", "near_pivot",
            "stop_within_adr",
        })

    @patch("src.data.fmp._get")
    def test_us_active_universe_filters_exchange_type_and_liquidity(self, get_mock):
        get_mock.return_value = [
            {"symbol": "SMOL", "companyName": "Small Co", "exchangeShortName": "NASDAQ", "price": 5, "volume": 3_000_000, "marketCap": 500_000_000, "sector": "Technology", "industry": "Software"},
            {"symbol": "THIN", "companyName": "Thin Co", "exchangeShortName": "NYSE", "price": 2, "volume": 100_000, "marketCap": 50_000_000},
            {"symbol": "ETF", "companyName": "Fund", "exchangeShortName": "NASDAQ", "price": 100, "volume": 2_000_000, "isEtf": True},
            {"symbol": "FOREIGN", "companyName": "Foreign", "exchangeShortName": "LSE", "price": 50, "volume": 1_000_000},
        ]

        full_frame = get_us_active_equities()
        frame = get_us_active_equities(min_current_dollar_volume=10_000_000)

        self.assertEqual(full_frame["ticker"].tolist(), ["SMOL", "THIN"])
        self.assertEqual(frame["ticker"].tolist(), ["SMOL"])
        self.assertEqual(frame.loc[0, "current_dollar_volume"], 15_000_000)

    @patch("src.data.fmp._get")
    def test_us_active_universe_can_include_etfs(self, get_mock):
        stock = {
            "symbol": "SMOL", "companyName": "Small Co", "exchangeShortName": "NASDAQ",
            "price": 5, "volume": 3_000_000, "isEtf": False,
        }
        etf = {
            "symbol": "SOXL", "companyName": "Semiconductor Bull 3X ETF",
            "exchangeShortName": "AMEX", "price": 200, "volume": 40_000_000,
            "isEtf": True, "isFund": False,
        }
        get_mock.side_effect = lambda _path, params: [etf] if params["isEtf"] else [stock]

        frame = get_us_active_equities(include_etfs=True)

        self.assertEqual(frame["ticker"].tolist(), ["SOXL", "SMOL"])
        self.assertEqual(frame.set_index("ticker").loc["SOXL", "asset_type"], "ETF")
        self.assertEqual(get_mock.call_count, 6)

    def test_persistent_scan_cache_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(scan_cache, "_CACHE_DIR", Path(temporary)):
                parameters = {"universe": "US_ACTIVE", "min_return_20d": 20.0}
                payload = {"asof": "2026-07-10", "rows": [{"ticker": "AEVA"}]}

                scan_cache.save_scan_cache(parameters, payload)

                self.assertEqual(scan_cache.load_scan_cache(parameters), payload)
                self.assertEqual(scan_cache.clear_scan_cache(), 1)
                self.assertIsNone(scan_cache.load_scan_cache(parameters))


class IntradayBreakoutTests(unittest.TestCase):
    def test_opening_ranges_and_bar_moving_averages(self):
        index = pd.date_range("2026-07-10 09:30:00", periods=390, freq="min")
        close = np.linspace(100.0, 110.0, len(index))
        frame = pd.DataFrame({
            "open": close - 0.02,
            "high": close + 0.10,
            "low": close - 0.10,
            "close": close,
            "volume": np.full(len(index), 10_000.0),
        }, index=index)

        snapshot = build_intraday_snapshot(frame, interval=5)

        self.assertIsNone(snapshot["error"])
        self.assertEqual(snapshot["session_date"], "2026-07-10")
        self.assertEqual(len(snapshot["bars"]), 78)
        self.assertTrue(snapshot["opening_ranges"]["1"]["triggered"])
        self.assertTrue(snapshot["opening_ranges"]["60"]["triggered"])
        self.assertIsNotNone(snapshot["bars"][-1]["ma50"])

    def test_longer_bar_mas_are_seeded_by_prior_sessions(self):
        sessions = []
        for offset, day in enumerate(pd.bdate_range("2026-06-29", periods=10)):
            index = pd.date_range(day + pd.Timedelta(hours=9, minutes=30), periods=390, freq="min")
            close = np.linspace(100.0 + offset, 101.0 + offset, len(index))
            sessions.append(pd.DataFrame({
                "open": close - 0.02,
                "high": close + 0.10,
                "low": close - 0.10,
                "close": close,
                "volume": np.full(len(index), 10_000.0),
            }, index=index))
        frame = pd.concat(sessions)

        snapshot = build_intraday_snapshot(frame, interval=60, session_date="2026-07-10")

        self.assertEqual(len(snapshot["bars"]), 7)
        self.assertIsNotNone(snapshot["bars"][0]["ma10"])
        self.assertIsNotNone(snapshot["bars"][0]["ma20"])
        self.assertIsNotNone(snapshot["bars"][0]["ma50"])

    @patch("src.data.fmp._get")
    def test_fmp_intraday_payload_is_normalized(self, get_mock):
        get_mock.return_value = [
            {"date": "2026-07-10 09:35:00", "open": 101, "high": 102, "low": 100, "close": 101.5, "volume": 200},
            {"date": "2026-07-10 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 100},
        ]

        frame = get_intraday_ohlcv(
            "aapl",
            interval="5min",
            start="2026-07-10",
            end="2026-07-10",
        )

        self.assertIsNotNone(frame)
        assert frame is not None
        self.assertEqual(frame.index[0], pd.Timestamp("2026-07-10 09:30:00"))
        self.assertEqual(frame.columns.tolist(), ["open", "high", "low", "close", "volume"])
        get_mock.assert_called_once_with(
            "/historical-chart/5min",
            params={"symbol": "AAPL", "from": "2026-07-10", "to": "2026-07-10"},
        )


if __name__ == "__main__":
    unittest.main()
