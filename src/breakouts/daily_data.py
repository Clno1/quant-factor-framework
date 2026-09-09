"""Version-bound daily input shared by every momentum-breakout runtime."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

import pandas as pd

from src.data.access import (
    DataContract,
    load_published_daily_data,
    load_published_universe,
)
from src.data.foundation import DatasetVersion, MarketDataReader
from src.data.universe_ids import resolve_market_data_universe


_PRICE_COLUMNS = ["open", "high", "low", "close", "adj_close", "volume"]


def daily_frames_from_bars(bars: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Convert one version-bound long table into per-ticker in-memory frames."""
    required = {
        "date",
        "ticker",
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
    }
    if bars is None or bars.empty or not required.issubset(bars.columns):
        missing = sorted(required - set(bars.columns if bars is not None else []))
        if missing:
            raise ValueError(
                "Published breakout bars are missing required price semantics "
                f"columns: {missing}. Rebuild the market-data version; adjusted "
                "prices must never silently fall back to raw close."
            )
        return {}
    data = bars.copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
    data["ticker"] = data["ticker"].astype(str).str.strip().str.upper()
    data = data.loc[data["date"].notna() & data["ticker"].ne("")].copy()
    for column in _PRICE_COLUMNS:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(
        subset=["open", "high", "low", "close", "adj_close", "volume"]
    )
    data = data.drop_duplicates(["date", "ticker"], keep="last")

    frames: dict[str, pd.DataFrame] = {}
    for ticker, group in data.groupby("ticker", sort=False):
        frames[str(ticker)] = (
            group.set_index("date")[_PRICE_COLUMNS]
            .sort_index()
        )
    return frames


@dataclass(frozen=True)
class BreakoutDailyDataset:
    requested_universe: str
    data_universe: str
    version: DatasetVersion
    contract: DataContract
    universe: pd.DataFrame
    frames: dict[str, pd.DataFrame]

    @property
    def dataset_version_id(self) -> str:
        return self.contract.dataset_version_id

    def frame(self, ticker: str) -> pd.DataFrame:
        return self.frames.get(str(ticker).strip().upper(), pd.DataFrame())


def load_breakout_daily_dataset(
    *,
    requested_universe: str,
    data_universe: str | None = None,
    tickers: Iterable[str] | None = None,
    ticker_selector: Callable[[pd.DataFrame], Iterable[str]] | None = None,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    exact_universe: bool = False,
    dataset_version_id: str | None = None,
    min_latest_coverage: float | None = None,
    lookback_calendar_days: int = 400,
    reader: MarketDataReader | None = None,
) -> BreakoutDailyDataset:
    """Resolve, validate and load exactly one published daily data version."""
    resolved = resolve_market_data_universe(data_universe or requested_universe)
    if resolved == "US_LIQUID_5M":
        from src.breakouts.broad_daily_data import load_broad_breakout_daily_dataset

        return load_broad_breakout_daily_dataset(
            requested_universe=requested_universe,
            tickers=tickers,
            ticker_selector=ticker_selector,
            start=start,
            end=end,
            exact_universe=exact_universe,
            dataset_version_id=dataset_version_id,
            min_latest_coverage=min_latest_coverage,
            lookback_calendar_days=lookback_calendar_days,
            reader=reader,
        )
    if tickers is not None and ticker_selector is not None:
        raise ValueError("tickers and ticker_selector are mutually exclusive")
    selected_version_id = dataset_version_id
    if ticker_selector is not None:
        universe_snapshot = load_published_universe(
            requested_universe=requested_universe,
            data_universe=resolved,
            dataset_version_id=dataset_version_id,
            reader=reader,
        )
        selected_version_id = universe_snapshot.version.version_id
        tickers = ticker_selector(universe_snapshot.universe.copy())
    normalized_tickers = (
        list(dict.fromkeys(
            str(ticker).strip().upper()
            for ticker in tickers
            if str(ticker).strip()
        ))
        if tickers is not None
        else None
    )
    if tickers is not None and not normalized_tickers:
        raise ValueError("the breakout daily ticker selection is empty")
    bundle = load_published_daily_data(
        requested_universe=requested_universe,
        data_universe=resolved,
        tickers=normalized_tickers,
        start=start,
        end=end,
        exact_universe=exact_universe,
        dataset_version_id=selected_version_id,
        min_latest_coverage=min_latest_coverage,
        lookback_calendar_days=lookback_calendar_days,
        reader=reader,
    )
    return BreakoutDailyDataset(
        requested_universe=requested_universe,
        data_universe=resolved,
        version=bundle.version,
        contract=bundle.contract,
        universe=bundle.universe,
        frames=daily_frames_from_bars(bundle.bars),
    )


__all__ = [
    "BreakoutDailyDataset",
    "daily_frames_from_bars",
    "load_breakout_daily_dataset",
]
