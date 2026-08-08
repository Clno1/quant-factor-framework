"""Data containers shared by backtest, paper trading, and the Web replay page."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass
class DecisionReplaySnapshot:
    """
    A self-contained, run-scoped decision ledger.

    All matrix values use the project's standard ``date x ticker`` layout.
    ``factors`` is a two-level mapping:
    ``factor_id -> {raw, clean, strategy_input, contribution}``.
    """

    manifest: dict[str, Any]
    daily_summary: pd.DataFrame
    market: dict[str, pd.DataFrame] = field(default_factory=dict)
    signals: dict[str, pd.DataFrame] = field(default_factory=dict)
    factors: dict[str, dict[str, pd.DataFrame]] = field(default_factory=dict)
    portfolio: dict[str, pd.DataFrame] = field(default_factory=dict)


__all__ = ["DecisionReplaySnapshot"]
