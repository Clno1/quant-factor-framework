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
from typing import Any, Mapping

import numpy as np
import pandas as pd


class PriceSemanticsError(ValueError):
    """Published price matrices cannot satisfy the explicit semantics contract."""


PRICE_SEMANTICS_SCHEMA_VERSION = 1
PRICE_SEMANTICS_ID = "EXECUTION_AND_TOTAL_RETURN_V1"
TOTAL_RETURN_OPEN_FORMULA = "execution_open * total_return_close / execution_close"
FMP_CANONICAL_SOURCE = "FMP_FULL_PLUS_DIVIDEND_ADJUSTED"


def build_price_semantics_contract(
    *,
    source: str,
    history_mode: str,
) -> dict[str, Any]:
    """Build the immutable manifest declaration for canonical daily bars.

    ``history_mode`` records whether the complete history was downloaded from
    canonical sources or extended from an already-authenticated semantic parent.
    It is intentionally not inferred from the column names: legacy files with an
    ``adj_close`` column are not proof that the values include dividends.
    """
    normalized_source = str(source or "").strip().upper()
    normalized_mode = str(history_mode or "").strip().upper()
    if not normalized_source:
        raise PriceSemanticsError("Price-semantics source provenance is required")
    if normalized_mode not in {"FULL_REBUILD", "INCREMENTAL_FROM_AUTHENTICATED_PARENT"}:
        raise PriceSemanticsError(
            "Price-semantics history_mode must be FULL_REBUILD or "
            "INCREMENTAL_FROM_AUTHENTICATED_PARENT"
        )
    return {
        "schema_version": PRICE_SEMANTICS_SCHEMA_VERSION,
        "semantic_id": PRICE_SEMANTICS_ID,
        "execution_columns": {
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
        },
        "total_return_close_column": "adj_close",
        "total_return_open_formula": TOTAL_RETURN_OPEN_FORMULA,
        "source": normalized_source,
        "history_mode": normalized_mode,
    }


def validate_price_semantics_contract(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate provenance before any table can be treated as total-return data."""
    if not isinstance(payload, Mapping):
        raise PriceSemanticsError(
            "Published data have no explicit price-semantics contract; run a full rebuild"
        )
    contract = dict(payload)
    expected_execution = {
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    }
    failures: list[str] = []
    if int(contract.get("schema_version") or 0) != PRICE_SEMANTICS_SCHEMA_VERSION:
        failures.append("schema_version")
    if contract.get("semantic_id") != PRICE_SEMANTICS_ID:
        failures.append("semantic_id")
    if contract.get("execution_columns") != expected_execution:
        failures.append("execution_columns")
    if contract.get("total_return_close_column") != "adj_close":
        failures.append("total_return_close_column")
    if contract.get("total_return_open_formula") != TOTAL_RETURN_OPEN_FORMULA:
        failures.append("total_return_open_formula")
    if not str(contract.get("source") or "").strip():
        failures.append("source")
    if str(contract.get("history_mode") or "").strip().upper() not in {
        "FULL_REBUILD",
        "INCREMENTAL_FROM_AUTHENTICATED_PARENT",
    }:
        failures.append("history_mode")
    if failures:
        raise PriceSemanticsError(
            f"Published price-semantics contract is invalid: {failures}"
        )
    return contract


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


__all__ = [
    "FMP_CANONICAL_SOURCE",
    "PRICE_SEMANTICS_ID",
    "PRICE_SEMANTICS_SCHEMA_VERSION",
    "PriceSemantics",
    "PriceSemanticsError",
    "TOTAL_RETURN_OPEN_FORMULA",
    "build_price_semantics_contract",
    "validate_price_semantics_contract",
]
