"""Build research wide tables from one immutable published data version.

The historical module name is retained because factor and analysis callers use
it as their shared adapter.  It no longer downloads data or reads
``data/processed``; ingestion belongs exclusively to ``MarketDataWriter``.
"""
from __future__ import annotations

from typing import Iterable

import pandas as pd

from src.data.foundation import MarketDataReader
from src.utils.identifiers import safe_path_component
from src.utils.logger import get_logger


log = get_logger(__name__)


def build_wide_tables(
    tickers: Iterable[str] | None = None,
    *,
    universe: str = "SP500",
    force: bool = False,
) -> dict[str, pd.DataFrame]:
    """Return date-by-ticker tables from the latest published version.

    ``force`` is accepted only to keep the research CLI explicit: a reader can
    never refresh data or move the publication pointer.
    """
    universe = safe_path_component(universe.upper(), label="universe")
    if force:
        log.info(
            "[%s] force cannot download in read mode; using the latest "
            "quality-approved version",
            universe,
        )
    return MarketDataReader().load_wide_tables(
        universe,
        tickers=tickers,
    )


def load_wide_tables(
    universe: str = "SP500",
    *,
    require_open: bool = False,
) -> dict[str, pd.DataFrame]:
    """Read the latest published wide tables without network access."""
    universe = safe_path_component(universe.upper(), label="universe")
    return MarketDataReader().load_wide_tables(
        universe,
        require_open=require_open,
    )


__all__ = ["build_wide_tables", "load_wide_tables"]
