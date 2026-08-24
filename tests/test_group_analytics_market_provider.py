from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from src.group_analytics.adapters import PublishedEODMarketDataProvider


class _MemoryReader:
    def __init__(self, root: Path):
        self.versions = {
            "SP500": SimpleNamespace(
                version_id="sp500-v1",
                universe="SP500",
                target_session=date(2026, 7, 31),
                checksum_sha256="sp500-bars-hash",
                bars_path=str(root / "sp500-bars.parquet"),
                universe_path=str(root / "sp500-universe.parquet"),
            ),
            "US_LIQUID_5M": SimpleNamespace(
                version_id="liquid-v1",
                universe="US_LIQUID_5M",
                target_session=date(2026, 7, 31),
                checksum_sha256="liquid-bars-hash",
                bars_path=str(root / "liquid-bars.parquet"),
                universe_path=str(root / "liquid-universe.parquet"),
            ),
        }
        self.calls: list[tuple[str, tuple[str, ...], str]] = []

    def require_latest(self, universe: str, **kwargs):
        return self.versions[universe]

    def load_bars(self, universe: str, *, tickers, version):
        requested = tuple(tickers)
        self.calls.append((universe, requested, version.version_id))
        dates = pd.to_datetime(["2026-07-30", "2026-07-31"])
        rows = []
        for offset, ticker in enumerate(requested):
            if universe == "SP500" and ticker == "MISSING":
                continue
            for day, price in zip(dates, [100.0 + offset, 101.0 + offset]):
                rows.append(
                    {
                        "date": day,
                        "ticker": ticker,
                        "adj_close": price,
                        "volume": 1_000_000.0,
                    }
                )
        return pd.DataFrame(rows)


def test_group_market_provider_reads_only_published_versions(tmp_path):
    reader = _MemoryReader(tmp_path)
    provider = PublishedEODMarketDataProvider(reader=reader)

    snapshot = provider.snapshot(
        symbols=["AAPL", "MSFT", "MISSING"],
        benchmark="SPY",
    )

    assert reader.calls == [
        ("SP500", ("AAPL", "MSFT", "MISSING"), "sp500-v1"),
        ("US_LIQUID_5M", ("SPY",), "liquid-v1"),
    ]
    assert list(snapshot.adj_close.columns) == ["AAPL", "MSFT", "MISSING"]
    assert snapshot.adj_close["MISSING"].isna().all()
    assert snapshot.benchmark_adj_close.loc[pd.Timestamp("2026-07-31"), "SPY"] == 101.0
    assert snapshot.market_cap is None
    assert provider.last_diagnostics["provider"] == "PUBLISHED_MARKET_DATA"
    assert provider.last_diagnostics["dataset_version_id"] == "sp500-v1"
    assert provider.last_diagnostics["benchmark_dataset_version_id"] == "liquid-v1"
    assert provider.last_diagnostics["missing_symbols"] == ["MISSING"]
