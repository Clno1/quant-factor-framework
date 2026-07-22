from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from src.data import loader
from src.data import cleaner


def _frame(*, adjusted: bool = True, partial_adjusted: bool = False) -> pd.DataFrame:
    index = pd.to_datetime(["2026-07-17", "2026-07-20"])
    values: dict[str, list[float]] = {
        "open": [100.0, 101.0],
        "high": [102.0, 103.0],
        "low": [99.0, 100.0],
        "close": [101.0, 102.0],
        "volume": [1_000.0, 1_100.0],
    }
    if adjusted:
        values["adj_close"] = [float("nan"), 102.0] if partial_adjusted else [101.0, 102.0]
    return pd.DataFrame(values, index=index)


class OhlcvCacheValidationTests(unittest.TestCase):
    def test_missing_or_partial_adjusted_close_is_not_usable(self):
        self.assertFalse(loader._ohlcv_frame_is_usable(_frame(adjusted=False)))
        self.assertFalse(
            loader._ohlcv_frame_is_usable(_frame(partial_adjusted=True))
        )
        self.assertTrue(loader._ohlcv_frame_is_usable(_frame()))

    def test_short_momentum_history_does_not_satisfy_multifactor_range(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "TEST.parquet"
            _frame().to_parquet(path)
            self.assertFalse(
                loader._cache_covers_range(
                    path,
                    "2021-07-20",
                    "2026-07-20",
                )
            )
            self.assertTrue(
                loader._cache_covers_range(
                    path,
                    "2026-07-15",
                    "2026-07-20",
                )
            )

    @patch("src.data.fmp.get_historical_ohlcv")
    def test_invalid_fresh_cache_is_downloaded_again(self, fetch_mock):
        fetch_mock.return_value = _frame()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad_path = root / "TEST.parquet"
            _frame(adjusted=False).to_parquet(bad_path)
            with patch.object(loader, "_RAW_DIR", root):
                paths = loader.download_ohlcv(
                    ["TEST"],
                    start="2026-07-17",
                    end="2026-07-20",
                    force=False,
                )

            self.assertEqual(paths, {"TEST": bad_path})
            fetch_mock.assert_called_once()
            repaired = pd.read_parquet(bad_path)
            self.assertTrue(loader._ohlcv_frame_is_usable(repaired))

    def test_wide_table_force_is_propagated_to_raw_loader(self):
        frame = _frame()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(cleaner, "_PROCESSED_BASE", root),
                patch.object(cleaner, "load_or_download", return_value={"TEST": frame}) as load_mock,
                patch.object(
                    cleaner,
                    "get_sector_map",
                    return_value=pd.Series({"TEST": "Technology"}),
                ),
            ):
                cleaner.build_wide_tables(
                    tickers=["TEST"],
                    universe="TEST_UNIVERSE",
                    force=True,
                )

        load_mock.assert_called_once_with(["TEST"], force=True)


if __name__ == "__main__":
    unittest.main()
