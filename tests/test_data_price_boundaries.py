from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.data import fmp
from src.data.access import load_published_bundle
from src.data.foundation import MarketDataCatalog, MarketDataReader, MarketDataWriter


@pytest.mark.parametrize("close_field", ["close", "adjClose"])
def test_nominal_price_source_is_separate_from_canonical_prices(monkeypatch, close_field):
    dates = pd.to_datetime(["2024-01-30", "2024-01-31"])
    canonical = pd.DataFrame({**{c: .2 for c in ("open", "high", "low", "close", "adj_close")}, "volume": 30_000_000.}, index=dates)
    calls = []
    def fetch(path, params):
        calls.append((path, params))
        return [{"date": str(date.date()), close_field: 2.} for date in dates]
    monkeypatch.setattr(fmp, "_get", fetch)
    monkeypatch.setattr(fmp, "get_canonical_historical_ohlcv", lambda *args: canonical)
    result = fmp.get_coverage_historical_ohlcv("AAA", "2024-01-30", "2024-01-31")
    assert calls[0][0] == "/historical-price-eod/non-split-adjusted"
    assert result.unadjusted_close.eq(2.).all()
    assert result.close.eq(.2).all()
    assert "unadjusted_close" not in canonical
    monkeypatch.setattr(fmp, "_get", lambda *args, **kwargs: [{"date": "2024-01-31", close_field: 2.}])
    with pytest.raises(ValueError, match="do not cover"):
        fmp.get_coverage_historical_ohlcv("AAA", "2024-01-30", "2024-01-31")


def test_mag7_bundle_separates_qqq_benchmark_from_research_members(tmp_path):
    import exchange_calendars as xcals
    calendar = xcals.get_calendar("XNYS")
    mag7 = ["AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA"]
    def fetch(ticker, start, end):
        index = pd.DatetimeIndex(calendar.sessions_in_range(start, end)).tz_localize(None)
        prices = np.arange(len(index)) + (100. if ticker == "QQQ" else 10.)
        return pd.DataFrame({**{c: prices for c in ("open", "high", "low", "close", "adj_close")}, "volume": 1_000_000.}, index=index)
    catalog = MarketDataCatalog(tmp_path / "catalog.duckdb")
    publication = MarketDataWriter(catalog=catalog, lake_dir=tmp_path / "lake", fetcher=fetch,
                                  fetcher_semantics_source="TEST_CANONICAL_FIXTURE").update_universe(
        "MAG7", target_session="2024-02-01", initial_start="2024-01-30", workers=1,
        universe_frame=pd.DataFrame({"ticker": mag7, "name": mag7, "sector": "Technology"}))
    reader = MarketDataReader(catalog=catalog)
    raw = reader.load_wide_tables("MAG7", version=publication.version)
    assert len(raw["close"].columns) == 8
    bundle = load_published_bundle(requested_universe="MAG7", start="2024-01-30", end="2024-02-01", reader=reader)
    assert set(bundle.prices.execution_close.columns) == set(mag7)
    assert set(bundle.universe.ticker) == set(mag7)
    assert bundle.contract.benchmark["ticker"] == "QQQ"
    expected = raw["adj_close"].QQQ.pct_change(fill_method=None).shift(-1)
    pd.testing.assert_series_equal(bundle.benchmark_returns.dropna(), expected.dropna(), check_names=False)
