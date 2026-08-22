"""Explicit price semantics for research, execution and portfolio accounting.

The published daily bar contract intentionally carries two different price
families:

* ``open/high/low/close`` are executable, split-adjusted market prices.
* ``adj_close`` is a dividend-adjusted total-return close used for research.

Historically several consumers treated ``adj_close`` as if it were an executable
price, while the next-open backtest used executable ``open`` for holding PnL.
That mixes units and drops cash dividends.  This module is the single conversion
boundary: consumers must name the price family they need instead of passing an
ambiguous ``price_df``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd


class PriceSemanticsError(ValueError):
    """Published price matrices cannot satisfy the explicit semantics contract."""


def _require_matrix(
    wide: Mapping[str, pd.DataFrame],
    key: str,
) -> pd.DataFrame:
    value = wide.get(key)
    if value is None or not isinstance(value, pd.DataFrame) or value.empty:
        raise PriceSemanticsError(f"Published wide tables require non-empty {key!r}")
    out = value.apply(pd.to_numeric, errors="coerce").copy()
    out.index = pd.DatetimeIndex(out.index)
    return out.sort_index()


def _align(
    frame: pd.DataFrame,
    *,
    index: pd.Index,
    columns: pd.Index,
) -> pd.DataFrame:
    return frame.reindex(index=index, columns=columns)


@dataclass(frozen=True)
class PriceSemantics:
    """All price/return matrices with their economic meanings made explicit."""

    execution_open: pd.DataFrame
    execution_close: pd.DataFrame
    total_return_close: pd.DataFrame
    total_return_open: pd.DataFrame
    total_returns: pd.DataFrame
    dividend_adjustment_factor: pd.DataFrame
    volume: pd.DataFrame

    @classmethod
    def from_wide(cls, wide: Mapping[str, pd.DataFrame]) -> "PriceSemantics":
        execution_close = _require_matrix(wide, "close")
        index = execution_close.index
        columns = execution_close.columns
        execution_open = _align(
            _require_matrix(wide, "open"), index=index, columns=columns
        )
        total_return_close = _align(
            _require_matrix(wide, "adj_close"), index=index, columns=columns
        )
        volume = _align(_require_matrix(wide, "volume"), index=index, columns=columns)

        valid_pair = execution_close.notna() & total_return_close.notna()
        invalid_price = valid_pair & (
            (execution_close <= 0)
            | (total_return_close <= 0)
            | ~np.isfinite(execution_close)
            | ~np.isfinite(total_return_close)
        )
        if invalid_price.any(axis=None):
            locations = np.argwhere(invalid_price.to_numpy())[:10]
            sample = [
                {
                    "date": str(pd.Timestamp(index[i]).date()),
                    "ticker": str(columns[j]),
                }
                for i, j in locations
            ]
            raise PriceSemanticsError(
                "Non-positive/non-finite close prices violate the price contract; "
                f"sample={sample}"
            )

        # FMP canonical bars use split-adjusted executable OHLC and a separately
        # dividend-adjusted close.  Multiplying the executable open by the same
        # same-day adjustment factor creates a synthetic total-return open used
        # ONLY for performance attribution.  It is never a fill price.
        adjustment = total_return_close / execution_close
        adjustment = adjustment.where(valid_pair)
        invalid_factor = adjustment.notna() & (
            ~np.isfinite(adjustment) | (adjustment <= 0)
        )
        if invalid_factor.any(axis=None):
            raise PriceSemanticsError(
                "Dividend adjustment factor must be finite and positive"
            )
        total_return_open = execution_open * adjustment
        total_returns = total_return_close.pct_change(fill_method=None)

        return cls(
            execution_open=execution_open,
            execution_close=execution_close,
            total_return_close=total_return_close,
            total_return_open=total_return_open,
            total_returns=total_returns,
            dividend_adjustment_factor=adjustment,
            volume=volume,
        )

    def forward_open_to_open_total_returns(self) -> pd.DataFrame:
        """Return [t open, t+1 open) total-return PnL labelled on decision row t."""
        return self.total_return_open.pct_change(fill_method=None).shift(-1)

    def execution_dollar_volume(self) -> pd.DataFrame:
        """Dollar volume in executable-price units, never dividend-adjusted units."""
        return self.execution_close * self.volume


__all__ = ["PriceSemantics", "PriceSemanticsError"]
