"""Structural interface for replaceable intraday market-data feeds."""
from __future__ import annotations

from typing import Any, Iterable, Protocol

import pandas as pd

from src.breakouts.live.models import QuoteSnapshot


class IntradayFeed(Protocol):
    source_name: str

    async def market_status(self, exchange: str = "NASDAQ") -> dict[str, Any]:
        ...

    async def quotes(
        self,
        symbols: Iterable[str],
    ) -> dict[str, QuoteSnapshot]:
        ...

    async def intraday_many(
        self,
        tickers: Iterable[str],
        *,
        session_date: str,
        preload: bool = False,
    ) -> dict[str, pd.DataFrame]:
        ...

    def counters(self) -> dict[str, int]:
        ...
