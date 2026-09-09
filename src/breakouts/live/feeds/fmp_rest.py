"""Low-request FMP REST feed: batch quote radar plus exact bar confirmation."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Iterable

import pandas as pd

from src.breakouts.live.models import QuoteSnapshot
from src.data.fmp import (
    get_batch_quotes,
    get_exchange_market_hours,
    get_intraday_ohlcv,
)


class FmpRestFeed:
    source_name = "fmp_rest_hybrid"

    def __init__(
        self,
        *,
        quote_chunk_size: int = 100,
        max_concurrent_requests: int = 4,
        history_calendar_days: int = 7,
    ) -> None:
        self.quote_chunk_size = max(1, min(500, int(quote_chunk_size)))
        self._semaphore = asyncio.Semaphore(
            max(1, min(8, int(max_concurrent_requests)))
        )
        self.history_calendar_days = max(3, int(history_calendar_days))
        self.quote_batches = 0
        self.exact_requests = 0
        self.failed_exact_requests = 0

    async def market_status(self, exchange: str = "NASDAQ") -> dict:
        async with self._semaphore:
            return await asyncio.to_thread(get_exchange_market_hours, exchange)

    async def quotes(self, symbols: Iterable[str]) -> dict[str, QuoteSnapshot]:
        normalized = list(dict.fromkeys(
            str(symbol).strip().upper()
            for symbol in symbols
            if str(symbol).strip()
        ))
        if not normalized:
            return {}
        async with self._semaphore:
            frame = await asyncio.to_thread(
                get_batch_quotes,
                normalized,
                chunk_size=self.quote_chunk_size,
            )
        self.quote_batches += 1
        if frame.empty:
            return {}
        snapshots: dict[str, QuoteSnapshot] = {}
        for _, row in frame.iterrows():
            try:
                quote = QuoteSnapshot.from_mapping(row.to_dict())
            except (TypeError, ValueError, OverflowError):
                continue
            if quote.ticker and quote.price > 0:
                snapshots[quote.ticker] = quote
        return snapshots

    async def intraday(
        self,
        ticker: str,
        *,
        session_date: str,
        preload: bool = False,
    ) -> pd.DataFrame | None:
        end = date.fromisoformat(session_date)
        start = (
            end - timedelta(days=self.history_calendar_days)
            if preload
            else end
        )
        async with self._semaphore:
            self.exact_requests += 1
            try:
                return await asyncio.to_thread(
                    get_intraday_ohlcv,
                    ticker,
                    interval="1min",
                    start=start.isoformat(),
                    end=end.isoformat(),
                )
            except Exception:  # noqa: BLE001
                self.failed_exact_requests += 1
                return None

    async def intraday_many(
        self,
        tickers: Iterable[str],
        *,
        session_date: str,
        preload: bool = False,
    ) -> dict[str, pd.DataFrame]:
        normalized = list(dict.fromkeys(
            str(ticker).strip().upper()
            for ticker in tickers
            if str(ticker).strip()
        ))
        results = await asyncio.gather(*(
            self.intraday(
                ticker,
                session_date=session_date,
                preload=preload,
            )
            for ticker in normalized
        ))
        return {
            ticker: frame
            for ticker, frame in zip(normalized, results, strict=True)
            if frame is not None and not frame.empty
        }

    def counters(self) -> dict[str, int]:
        return {
            "quote_batches": self.quote_batches,
            "exact_requests": self.exact_requests,
            "failed_exact_requests": self.failed_exact_requests,
        }
