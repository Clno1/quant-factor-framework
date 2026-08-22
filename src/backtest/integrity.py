"""Compatibility routing for the price-semantics-safe formal backtest.

Direct imports of the legacy ``src.backtest.quintile.quintile_backtest`` remain
available for low-level compatibility tests. Package-level research calls and
the asynchronous runner are routed here when their data came from the published
MarketDataReader integrity boundary.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

import src.backtest.quintile as _legacy_quintile_module
from src.backtest.quintile import QuintileResult
from src.backtest.quintile_v2 import quintile_backtest_v2
from src.data.integrity import install_data_contract_benchmark_adapter


_LEGACY_QUINTILE = _legacy_quintile_module.quintile_backtest
_LEGACY_BUILD_TRADABLE = _legacy_quintile_module.build_tradable_mask
_INSTALLED = False


def _semantic_payload(price_df: pd.DataFrame | None) -> dict[str, Any] | None:
    if price_df is None:
        return None
    attrs = getattr(price_df, "attrs", {}) or {}
    execution_close = attrs.get("execution_close")
    total_return_open = attrs.get("total_return_open")
    total_return_close = attrs.get("total_return_close")
    if (
        not isinstance(execution_close, pd.DataFrame)
        or not isinstance(total_return_open, pd.DataFrame)
        or not isinstance(total_return_close, pd.DataFrame)
    ):
        return None
    return {
        "execution_close": execution_close,
        "total_return_open": total_return_open,
        "total_return_close": total_return_close,
        "benchmark_returns": attrs.get("benchmark_returns"),
        "benchmark_contract": attrs.get("benchmark_contract"),
        "benchmark_error": attrs.get("benchmark_error"),
    }


def _execution_close(price_df: pd.DataFrame | None) -> pd.DataFrame | None:
    """Return the explicit executable close without requiring attribution fields.

    Tradability needs only executable price units. Requiring the complete
    total-return payload here made this narrow adapter fall back to adjusted
    close whenever a caller attached only ``execution_close``. The formal
    backtest adapter remains strict and still requires the full semantic payload.
    """
    if price_df is None:
        return None
    candidate = (getattr(price_df, "attrs", {}) or {}).get("execution_close")
    return candidate if isinstance(candidate, pd.DataFrame) and not candidate.empty else None


def build_tradable_mask_integrity(*args, **kwargs):
    """Use executable close for price floors/dollar volume when semantics exist."""
    execution_close = _execution_close(kwargs.get("price_df"))
    if execution_close is not None:
        kwargs = dict(kwargs)
        kwargs["price_df"] = execution_close
    return _LEGACY_BUILD_TRADABLE(*args, **kwargs)


def quintile_backtest_integrity(
    factor_df: pd.DataFrame,
    returns_df: pd.DataFrame,
    n_groups=None,
    rebalance_days=None,
    factor_direction: int = +1,
    *,
    open_df: pd.DataFrame | None = None,
    price_df: pd.DataFrame | None = None,
    volume_df: pd.DataFrame | None = None,
    tradable_mask: pd.DataFrame | None = None,
    membership_mask: pd.DataFrame | None = None,
    membership_events: pd.DataFrame | None = None,
    benchmark_returns: pd.Series | None = None,
    rebalance_mode: str | None = None,
    execution: dict | None = None,
) -> QuintileResult:
    """Legacy-signature adapter that becomes strict for published formal data."""
    semantic = _semantic_payload(price_df)
    if semantic is None:
        # Synthetic/unit-test callers that do not originate from the published
        # data contract retain the legacy behavior. Formal data always carries
        # the semantic marker and therefore cannot enter this branch.
        return _LEGACY_QUINTILE(
            factor_df,
            returns_df,
            n_groups=n_groups,
            rebalance_days=rebalance_days,
            factor_direction=factor_direction,
            open_df=open_df,
            price_df=price_df,
            volume_df=volume_df,
            tradable_mask=tradable_mask,
            membership_mask=membership_mask,
            membership_events=membership_events,
            benchmark_returns=benchmark_returns,
            rebalance_mode=rebalance_mode,
            execution=execution,
        )
    if open_df is None or open_df.empty:
        raise ValueError("Published formal backtest is missing executable open prices")
    benchmark = benchmark_returns
    if benchmark is None:
        candidate = semantic.get("benchmark_returns")
        benchmark = candidate if isinstance(candidate, pd.Series) else None
    if benchmark is None or benchmark.empty:
        detail = semantic.get("benchmark_error") or "registered benchmark unavailable"
        raise ValueError(
            "Formal named-universe backtest requires its immutable registered "
            f"benchmark (SPY/QQQ); {detail}"
        )

    result = quintile_backtest_v2(
        factor_df,
        returns_df,
        n_groups=n_groups,
        rebalance_days=rebalance_days,
        factor_direction=factor_direction,
        execution_open_df=open_df,
        execution_close_df=semantic["execution_close"],
        total_return_open_df=semantic["total_return_open"],
        total_return_close_df=semantic["total_return_close"],
        volume_df=volume_df,
        tradable_mask=tradable_mask,
        membership_mask=membership_mask,
        membership_events=membership_events,
        benchmark_returns=benchmark,
        rebalance_mode=rebalance_mode,
        execution=execution,
    )
    contract = semantic.get("benchmark_contract")
    if isinstance(contract, dict):
        result.config["benchmark_data_contract"] = dict(contract)
        result.config["benchmark_ticker"] = contract.get("ticker")
    return result


def _replay_snapshot_adapter(original):
    def wrapped(*args, **kwargs):
        close_prices = kwargs.get("close_prices")
        semantic = _semantic_payload(close_prices)
        if semantic is not None:
            kwargs = dict(kwargs)
            kwargs["close_prices"] = semantic["execution_close"]
        return original(*args, **kwargs)

    return wrapped


def install_backtest_integrity_adapter() -> None:
    """Patch only the public/runner compatibility edges, not the legacy engine."""
    global _INSTALLED
    if _INSTALLED:
        return
    install_data_contract_benchmark_adapter()

    # run_mvp imports build_tradable_mask directly from the legacy submodule;
    # make that helper semantics-aware while preserving its signature.
    _legacy_quintile_module.build_tradable_mask = build_tradable_mask_integrity

    # Import the runner only after the package's base modules are initialized,
    # then replace the two local aliases it captured at import time.
    import src.backtest.runner as runner

    runner.quintile_backtest = quintile_backtest_integrity
    runner.build_tradable_mask = build_tradable_mask_integrity
    runner.build_backtest_snapshot = _replay_snapshot_adapter(
        runner.build_backtest_snapshot
    )
    _INSTALLED = True


__all__ = [
    "build_tradable_mask_integrity",
    "install_backtest_integrity_adapter",
    "quintile_backtest_integrity",
]
