"""Stable identifiers for versioned dynamic market-data universes."""
from __future__ import annotations

import hashlib

from src.utils.identifiers import canonical_uuid, safe_path_component


US_LIQUID_5M = "US_LIQUID_5M"
US_EQUITY_COVERAGE = "US_EQUITY_COVERAGE"
LEGACY_US_ACTIVE = "US_ACTIVE"
WATCHLIST_PREFIX = "WATCHLIST_"


def watchlist_data_universe(
    watchlist_id: str,
    *,
    tickers: list[str] | tuple[str, ...] | None = None,
) -> str:
    canonical = canonical_uuid(watchlist_id, label="watchlist_id")
    revision = ""
    if tickers is not None:
        normalized = sorted(
            {
                str(ticker).strip().upper()
                for ticker in tickers
                if str(ticker).strip()
            }
        )
        digest = hashlib.sha256(",".join(normalized).encode("utf-8")).hexdigest()[:12]
        revision = f"_{digest.upper()}"
    return safe_path_component(
        f"{WATCHLIST_PREFIX}{canonical.replace('-', '').upper()}{revision}",
        label="data_universe",
    )


def watchlist_snapshot_data_universe(snapshot: dict) -> str:
    items = snapshot.get("items") or []
    tickers = [str(item.get("ticker") or "") for item in items]
    return watchlist_data_universe(str(snapshot.get("id") or ""), tickers=tickers)


def resolve_market_data_universe(
    universe: str,
    *,
    watchlist_id: str | None = None,
) -> str:
    value = str(universe or "").strip()
    if value.lower().startswith("watchlist:"):
        identifier = watchlist_id or value.split(":", 1)[1]
        return watchlist_data_universe(identifier)
    upper = value.upper()
    if upper == LEGACY_US_ACTIVE:
        return US_LIQUID_5M
    return safe_path_component(upper, label="data_universe")


__all__ = [
    "LEGACY_US_ACTIVE",
    "US_EQUITY_COVERAGE",
    "US_LIQUID_5M",
    "WATCHLIST_PREFIX",
    "resolve_market_data_universe",
    "watchlist_data_universe",
    "watchlist_snapshot_data_universe",
]
