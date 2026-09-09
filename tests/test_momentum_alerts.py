from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from src.alerts.config import AlertSettings, load_local_env
from src.alerts.discord import DiscordNotifier, build_discord_payload
from src.alerts.engine import (
    _broad_pool,
    _completed_avg_dollar_volume,
    _provisional_daily_frame,
    run_live_alert_scan,
)
from src.alerts.state import AlertStateStore
from src.data.fmp import get_batch_quotes, get_exchange_market_hours
from scripts.configure_momentum_discord import _update_env_file


class _FakeDailyDataset:
    data_universe = "US_LIQUID_5M"
    dataset_version_id = "version-test"

    def __init__(
        self,
        universe: pd.DataFrame,
        frames: dict[str, pd.DataFrame] | None = None,
    ):
        self.universe = universe
        self.frames = frames or {}
        self.contract = Mock()
        self.contract.to_dict.return_value = {
            "schema_version": 1,
            "data_universe": self.data_universe,
            "dataset_version_id": self.dataset_version_id,
        }

    def frame(self, ticker: str) -> pd.DataFrame:
        return self.frames.get(ticker, pd.DataFrame())


class FmpLiveQuoteTests(unittest.TestCase):
    @patch("src.data.fmp._get")
    def test_batch_quotes_are_chunked_normalized_and_deduplicated(self, get_mock):
        def response(_path, params):
            return [
                {
                    "symbol": symbol,
                    "price": "10.5",
                    "volume": "2000",
                    "timestamp": "1783713600",
                }
                for symbol in params["symbols"].split(",")
            ]

        get_mock.side_effect = response
        frame = get_batch_quotes(["soxl", "PENG", "SOXL"], chunk_size=1)

        self.assertEqual(frame.index.tolist(), ["PENG", "SOXL"])
        self.assertEqual(get_mock.call_count, 2)
        self.assertEqual(frame.loc["SOXL", "price"], 10.5)
        self.assertEqual(frame.loc["PENG", "volume"], 2000)

    @patch("src.data.fmp._get")
    def test_exchange_market_hours_normalizes_boolean(self, get_mock):
        get_mock.return_value = [{"exchange": "NASDAQ", "isMarketOpen": "true"}]

        result = get_exchange_market_hours("nasdaq")

        self.assertTrue(result["isMarketOpen"])
        get_mock.assert_called_once_with("/exchange-market-hours", {"exchange": "NASDAQ"})


class AlertConfigTests(unittest.TestCase):
    def test_etfs_are_disabled_by_default(self):
        self.assertFalse(AlertSettings().include_etfs)

    def test_local_env_does_not_override_process_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env.local"
            path.write_text(
                "# comment\nDISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/file/token'\n"
                "DISCORD_ALERT_ROLE_ID=123\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "process-value"}, clear=True):
                loaded = load_local_env(path)
                self.assertEqual(loaded, path)
                self.assertEqual(os.environ["DISCORD_WEBHOOK_URL"], "process-value")
                self.assertEqual(os.environ["DISCORD_ALERT_ROLE_ID"], "123")

    def test_configure_script_updates_keys_and_preserves_other_lines(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env.local"
            path.write_text("# local secrets\nFMP_API_KEY=keep-me\nDISCORD_WEBHOOK_URL=old\n", encoding="utf-8")

            _update_env_file(path, {
                "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/new/token",
                "DISCORD_ALERT_ROLE_ID": "123",
            })

            content = path.read_text(encoding="utf-8")
            self.assertIn("FMP_API_KEY=keep-me", content)
            self.assertIn("DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/new/token", content)
            self.assertIn("DISCORD_ALERT_ROLE_ID=123", content)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class LiveFrameTests(unittest.TestCase):
    @patch("src.alerts.engine.load_market_regime", return_value={"asof": "2026-07-28"})
    @patch("src.alerts.engine.evaluate_daily_setup", return_value=None)
    @patch("src.alerts.engine.get_batch_quotes")
    @patch("src.alerts.engine.scan_breakouts")
    @patch("src.alerts.engine._forced_tickers", return_value=set())
    def test_live_scan_uses_one_version_bound_dataset(
        self,
        _forced_mock,
        scan_mock,
        quotes_mock,
        _evaluate_mock,
        _regime_mock,
    ):
        universe = pd.DataFrame({
            "ticker": ["AEVA", "QQQ"],
            "name": ["Aeva", "Nasdaq 100"],
            "sector": ["Technology", ""],
            "asset_type": ["STOCK", "ETF"],
            "current_dollar_volume": [20_000_000.0, 5_000_000_000.0],
        })
        frame = pd.DataFrame({
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.0],
            "volume": [1_000_000.0],
        }, index=[pd.Timestamp("2026-07-27")])
        daily = _FakeDailyDataset(universe, {"AEVA": frame, "QQQ": frame})
        selected: list[str] = []

        def dataset_loader(**kwargs):
            selected.extend(kwargs["ticker_selector"](universe))
            return daily

        scan_mock.return_value = {
            "rows": [{"ticker": "AEVA"}],
            "asof": "2026-07-27",
        }
        quotes_mock.return_value = pd.DataFrame({
            "ticker": ["AEVA"],
            "price": [10.0],
            "timestamp": [pd.Timestamp("2026-07-28 14:30", tz="UTC").timestamp()],
        }).set_index("ticker")

        result = run_live_alert_scan(
            AlertSettings(),
            market_hours={"isMarketOpen": True},
            include_intraday=False,
            dataset_loader=dataset_loader,
        )

        self.assertEqual(selected, ["AEVA", "QQQ"])
        self.assertEqual(result["dataset_version_id"], "version-test")
        self.assertEqual(result["data_contract"]["data_universe"], "US_LIQUID_5M")

    @patch("src.alerts.engine.scan_breakouts")
    def test_broad_pool_excludes_etfs_including_forced_tickers(
        self,
        scan_mock,
    ):
        daily = _FakeDailyDataset(pd.DataFrame({
            "ticker": ["AEVA", "SOXL"],
            "name": ["Aeva Technologies", "Direxion Semiconductor Bull 3X"],
            "sector": ["Technology", ""],
            "asset_type": ["STOCK", "ETF"],
            "current_dollar_volume": [20_000_000.0, 2_000_000_000.0],
        }))
        scan_mock.return_value = {"rows": [{"ticker": "AEVA"}], "asof": "2026-07-10"}

        tickers, universe, _, forced, excluded, source_count = _broad_pool(
            AlertSettings(include_etfs=False),
            {"AEVA", "SOXL"},
            daily,
        )

        self.assertEqual(tickers, ["AEVA"])
        self.assertEqual(universe["ticker"].tolist(), ["AEVA"])
        self.assertEqual(forced, {"AEVA"})
        self.assertEqual(excluded, {"SOXL"})
        self.assertEqual(source_count, 2)
        scan_tickers = list(scan_mock.call_args.args[0])
        self.assertEqual(scan_tickers, ["AEVA"])

    @patch("src.alerts.engine.scan_breakouts")
    def test_broad_pool_can_include_etfs(self, scan_mock):
        daily = _FakeDailyDataset(pd.DataFrame({
            "ticker": ["AEVA", "SOXL"],
            "name": ["Aeva Technologies", "Direxion Semiconductor Bull 3X"],
            "sector": ["Technology", ""],
            "asset_type": ["STOCK", "ETF"],
            "current_dollar_volume": [20_000_000.0, 2_000_000_000.0],
        }))
        scan_mock.return_value = {
            "rows": [{"ticker": "AEVA"}, {"ticker": "SOXL"}],
            "asof": "2026-07-10",
        }

        tickers, universe, _, forced, excluded, _ = _broad_pool(
            AlertSettings(include_etfs=True),
            {"SOXL"},
            daily,
        )

        self.assertEqual(tickers, ["AEVA", "SOXL"])
        self.assertEqual(universe["ticker"].tolist(), ["AEVA", "SOXL"])
        self.assertEqual(forced, {"SOXL"})
        self.assertEqual(excluded, set())

    @patch("src.alerts.engine.scan_breakouts")
    def test_broad_pool_excludes_forced_ticker_missing_from_published_version(
        self,
        scan_mock,
    ):
        daily = _FakeDailyDataset(pd.DataFrame({
            "ticker": ["AEVA"],
            "name": ["Aeva Technologies"],
            "sector": ["Technology"],
            "asset_type": ["STOCK"],
            "current_dollar_volume": [20_000_000.0],
        }))
        scan_mock.return_value = {"rows": [{"ticker": "AEVA"}], "asof": "2026-07-10"}

        tickers, universe, _, forced, excluded, _ = _broad_pool(
            AlertSettings(include_etfs=False),
            {"TSM"},
            daily,
        )

        self.assertEqual(tickers, ["AEVA"])
        self.assertNotIn("TSM", universe["ticker"].tolist())
        self.assertEqual(forced, set())
        self.assertEqual(excluded, {"TSM"})

    def test_provisional_bar_replaces_same_session_without_mutating_cache(self):
        index = pd.bdate_range("2026-01-02", periods=70)
        frame = pd.DataFrame({
            "open": 100.0,
            "high": 103.0,
            "low": 98.0,
            "close": 101.0,
            "volume": 1_000_000.0,
        }, index=index)
        quote_date = pd.Timestamp(index[-1]).normalize()
        quote = pd.Series({
            "price": 110.0,
            "open": 102.0,
            "dayHigh": 112.0,
            "dayLow": 101.0,
            "volume": 2_000_000.0,
        })

        provisional = _provisional_daily_frame(frame, quote, quote_date)

        self.assertEqual(provisional.loc[quote_date, "close"], 110.0)
        self.assertEqual(provisional.loc[quote_date, "high"], 112.0)
        self.assertEqual(frame.iloc[-1]["close"], 101.0)

    def test_completed_average_excludes_partial_live_session(self):
        index = pd.bdate_range("2026-01-02", periods=21)
        frame = pd.DataFrame({
            "close": [10.0] * 20 + [100.0],
            "volume": [1_000_000.0] * 20 + [10.0],
        }, index=index)

        average = _completed_avg_dollar_volume(
            frame,
            quote_date=pd.Timestamp(index[-1]),
            market_open=True,
        )

        self.assertEqual(average, 10_000_000.0)


class AlertStateTests(unittest.TestCase):
    def test_dry_run_remains_pending_until_delivery_and_upgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = AlertStateStore(Path(temporary) / "state.sqlite3")
            candidate = {"ticker": "SOXL", "signal_type": "CANDIDATE", "score": 50}

            first = store.observe("2026-07-10", [candidate])
            second = store.observe("2026-07-10", [candidate])
            self.assertEqual(len(first), 1)
            self.assertEqual(len(second), 1)

            store.mark_delivered("2026-07-10", first)
            self.assertEqual(store.observe("2026-07-10", [candidate]), [])

            ready = {"ticker": "SOXL", "signal_type": "READY", "score": 75}
            upgrade = store.observe("2026-07-10", [ready])
            self.assertEqual(len(upgrade), 1)
            self.assertEqual(upgrade[0]["previous_delivered_rank"], 1)


class DiscordNotifierTests(unittest.TestCase):
    def _snapshot(self):
        return {
            "generated_at": "2026-07-10T15:35:00+00:00",
            "quote_time": "2026-07-10T11:35:00-04:00",
            "market_hours": {"isMarketOpen": True},
            "broad_count": 420,
            "strict_count": 12,
            "pending_upgrade_count": 1,
        }

    def _row(self):
        return {
            "ticker": "SOXL",
            "name": "Direxion Semiconductor Bull 3X ETF",
            "signal_type": "BREAKOUT",
            "score": 80,
            "close": 200.0,
            "pivot": 198.0,
            "pivot_distance": 1.01,
            "return_20d": 24.0,
            "adr_20d": 13.7,
            "dollar_volume": 7_000_000_000,
            "avg_dollar_volume_20d": 12_000_000_000,
            "is_upgrade": True,
        }

    def test_payload_mentions_only_explicit_role(self):
        payload = build_discord_payload(
            self._snapshot(),
            [self._row()],
            role_id="123456",
            mention=True,
        )

        self.assertEqual(payload["allowed_mentions"], {"parse": [], "roles": ["123456"]})
        self.assertEqual(payload["content"], "<@&123456> 发现新的高优先级动量信号")
        self.assertIn("SOXL", payload["embeds"][0]["fields"][0]["name"])

    @patch("src.alerts.discord.requests.post")
    def test_notifier_posts_with_wait_and_returns_message_id(self, post_mock):
        response = Mock(status_code=200, content=b"{}")
        response.json.return_value = {"id": "message-1"}
        post_mock.return_value = response
        notifier = DiscordNotifier("https://discord.com/api/webhooks/123/secret")

        result = notifier.send({"content": "test"})

        self.assertEqual(result["message_id"], "message-1")
        self.assertTrue(post_mock.call_args.args[0].endswith("?wait=true"))


if __name__ == "__main__":
    unittest.main()
