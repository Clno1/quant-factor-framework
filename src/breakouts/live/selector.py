"""Pure ranking from the broad daily pool into the active intraday pool."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from src.breakouts.live.models import DailyCandidate, QuoteSnapshot


@dataclass(frozen=True)
class ActiveSelection:
    candidate: DailyCandidate
    quote: QuoteSnapshot
    pivot_distance_pct: float
    priority: tuple[float, ...]


def select_active_pool(
    candidates: Sequence[DailyCandidate],
    quotes: Mapping[str, QuoteSnapshot],
    *,
    max_symbols: int,
    previous_tickers: Iterable[str] = (),
) -> list[ActiveSelection]:
    """Return a deterministic active pool without mutating strategy thresholds."""
    limit = max(1, int(max_symbols))
    retained = {
        str(ticker).strip().upper()
        for ticker in previous_tickers
        if str(ticker).strip()
    }
    selections: list[ActiveSelection] = []
    status_rank = {"BREAKOUT": 3.0, "READY": 2.0, "SETUP": 1.0, "FORMING": 0.0}
    for candidate in candidates:
        quote = quotes.get(candidate.ticker)
        if quote is None or quote.price <= 0:
            continue
        level = candidate.breakout_level
        distance = (
            (quote.price / level - 1.0) * 100.0
            if level > 0
            else float("-inf")
        )
        near_or_through = level > 0 and distance >= -3.0
        touched = level > 0 and quote.day_high >= level
        priority = (
            float(candidate.forced_watch),
            float(candidate.cup_qualified),
            float(candidate.setup_qualified),
            float(touched),
            float(near_or_through),
            status_rank.get(candidate.daily_status, 0.0),
            float(candidate.ticker in retained),
            -abs(distance) if level > 0 else -1_000_000.0,
            float(candidate.setup_score),
            quote.change_percentage,
            quote.dollar_volume,
        )
        selections.append(ActiveSelection(
            candidate=candidate,
            quote=quote,
            pivot_distance_pct=distance,
            priority=priority,
        ))
    selections.sort(key=lambda item: (
        *(-value for value in item.priority),
        item.candidate.ticker,
    ))
    return selections[:limit]
