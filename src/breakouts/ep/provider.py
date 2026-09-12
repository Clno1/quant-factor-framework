"""Compose the sole FMP adapter; no HTTP, credentials or trading rules here."""
from __future__ import annotations

from typing import Any, Protocol


class EpProvider(Protocol):
    def calendar(self, day: str, *, timeout: float) -> list[dict[str, Any]]: ...
    def articles(self, feed: str, page: int, limit: int, *, timeout: float) -> list[dict[str, Any]]: ...
    def profile(self, symbol: str, *, timeout: float) -> dict[str, Any] | None: ...


class FmpEpProvider:
    scope = "FMP_GLOBAL_LATEST_FEEDS_AND_DAILY_CALENDAR_NOT_MARKET_COMPLETE"

    def prices(self, symbols, *, premarket, timeout):
        from src.data.fmp import get_ep_price_batch
        return get_ep_price_batch(symbols, premarket=premarket, timeout=timeout)

    def symbol_news(self, symbol, feed, start, end, *, timeout):
        from src.data.fmp import get_ep_symbol_news
        return get_ep_symbol_news(symbol, feed, start, end, timeout=timeout)

    def calendar(self, day: str, *, timeout: float) -> list[dict[str, Any]]:
        from src.data.fmp import get_ep_earnings_calendar_day
        return get_ep_earnings_calendar_day(day, timeout=timeout)

    def articles(self, feed: str, page: int, limit: int, *, timeout: float) -> list[dict[str, Any]]:
        from src.data.fmp import get_ep_news_page
        return get_ep_news_page(feed, page=page, limit=limit, timeout=timeout)

    def profile(self, symbol: str, *, timeout: float) -> dict[str, Any] | None:
        from src.data.fmp import get_ep_security_profile
        return get_ep_security_profile(symbol, timeout=timeout)
