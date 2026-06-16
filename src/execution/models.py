"""Execution reality models shared by backtests and paper trading.

The design follows the same separation used by mature backtesting engines:
fees, slippage, and fills are independent concerns. The current project only
uses daily bars, so the models remain approximate, but they are explicit and
auditable instead of one opaque transaction-cost number.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import math

from src.config import CONFIG


ALLOWED_FEE_MODELS = {
    "simple_bps",
    "ibkr_us_pro_fixed",
    "ibkr_us_pro_tiered",
    "ibkr_us_lite",
}

ALLOWED_SLIPPAGE_MODELS = {
    "none",
    "constant_bps",
    "simple_bps",
    "volume_share",
}


def _plain_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return {
            k: _plain_dict(v) if isinstance(v, dict) or hasattr(v, "items") else v
            for k, v in obj.items()
        }
    try:
        d = dict(obj)
        return {
            k: _plain_dict(v) if isinstance(v, dict) or hasattr(v, "items") else v
            for k, v in d.items()
        }
    except Exception:  # noqa: BLE001
        return {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        elif value is not None:
            out[key] = value
    return out


def _nested_get(d: dict[str, Any], path: str, default: Any) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def resolve_execution_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve task/account execution config against global defaults."""
    defaults = _plain_dict(getattr(CONFIG.backtest, "execution", {}))
    fallback = {
        "timing": "next_open",
        "portfolio_value": 100000.0,
        "fee_model": "ibkr_us_pro_fixed",
        "slippage_model": "volume_share",
        "slippage_bps": 5.0,
        "commission_bps": 2.0,
        "slippage": {
            "fallback_bps": 5.0,
            "spread_bps": 2.0,
            "volume_limit": 0.025,
            "price_impact": 0.10,
        },
        "fees": {
            "simple_bps": 2.0,
            "ibkr_us_pro_fixed": {
                "per_share": 0.005,
                "min_per_order": 1.00,
                "max_pct_trade_value": 0.01,
            },
            "ibkr_us_pro_tiered": {
                "per_share": 0.0035,
                "min_per_order": 0.35,
                "max_pct_trade_value": 0.01,
            },
            "ibkr_us_lite": {
                "per_share": 0.0,
                "min_per_order": 0.0,
                "max_pct_trade_value": 0.0,
            },
            "regulatory": {
                "sec_sell_rate": 0.0000206,
                "finra_taf_per_share": 0.000195,
                "finra_taf_cap": 9.79,
                "finra_cat_per_share": 0.000003,
            },
            "clearing": {
                "nscc_dtc_per_share": 0.00020,
                "nscc_dtc_cap_pct_trade_value": 0.005,
            },
            "pass_through": {
                "nyse_rate_on_commission": 0.000175,
                "finra_rate_on_commission": 0.00056,
            },
            "include_regulatory": True,
            "include_cat": True,
            "include_clearing": True,
            "include_pass_through": True,
            "exchange_fee_bps": 0.0,
        },
        "min_open_coverage": 0.95,
    }
    cfg = _deep_merge(fallback, defaults)
    cfg = _deep_merge(cfg, overrides or {})
    cfg["timing"] = str(cfg.get("timing") or "next_open").lower()
    cfg["fee_model"] = str(cfg.get("fee_model") or "simple_bps").lower()
    cfg["slippage_model"] = str(cfg.get("slippage_model") or "constant_bps").lower()
    if cfg["fee_model"] not in ALLOWED_FEE_MODELS:
        raise ValueError(f"Unknown fee_model={cfg['fee_model']!r}")
    if cfg["slippage_model"] not in ALLOWED_SLIPPAGE_MODELS:
        raise ValueError(f"Unknown slippage_model={cfg['slippage_model']!r}")
    cfg["slippage_bps"] = float(cfg.get("slippage_bps") or 0.0)
    cfg["commission_bps"] = float(cfg.get("commission_bps") or 0.0)
    cfg["portfolio_value"] = float(cfg.get("portfolio_value") or 100000.0)
    return cfg


def calculate_slippage_bps(
    *,
    side: str,
    quantity: float,
    raw_price: float,
    volume: float | None,
    execution: dict[str, Any],
) -> dict[str, float | str]:
    """Return signed-neutral slippage bps and diagnostics for one order."""
    _ = side
    model = str(execution.get("slippage_model") or "constant_bps").lower()
    qty = abs(float(quantity or 0.0))
    px = float(raw_price or 0.0)
    vol = float(volume) if volume is not None and math.isfinite(float(volume)) else 0.0
    fallback_bps = float(_nested_get(execution, "slippage.fallback_bps", execution.get("slippage_bps", 0.0)) or 0.0)

    if qty <= 0 or px <= 0:
        return {"model": model, "slippage_bps": 0.0, "participation_rate": 0.0, "impact_bps": 0.0}

    if model in ("none", "null"):
        return {"model": model, "slippage_bps": 0.0, "participation_rate": 0.0, "impact_bps": 0.0}

    if model in ("constant_bps", "simple_bps"):
        bps = float(execution.get("slippage_bps", fallback_bps) or 0.0)
        return {"model": model, "slippage_bps": bps, "participation_rate": 0.0, "impact_bps": bps}

    if model == "volume_share":
        if vol <= 0:
            return {
                "model": model,
                "slippage_bps": fallback_bps,
                "participation_rate": 0.0,
                "impact_bps": fallback_bps,
            }
        volume_limit = float(_nested_get(execution, "slippage.volume_limit", 0.025) or 0.025)
        price_impact = float(_nested_get(execution, "slippage.price_impact", 0.10) or 0.10)
        spread_bps = float(_nested_get(execution, "slippage.spread_bps", 0.0) or 0.0)
        participation = qty / vol
        capped_participation = min(max(participation, 0.0), max(volume_limit, 0.0))
        impact_bps = price_impact * (capped_participation ** 2) * 10000.0
        return {
            "model": model,
            "slippage_bps": spread_bps + impact_bps,
            "participation_rate": participation,
            "impact_bps": impact_bps,
        }

    # Unknown model: be conservative and use the legacy bps fallback.
    return {"model": "fallback_constant_bps", "slippage_bps": fallback_bps, "participation_rate": 0.0, "impact_bps": fallback_bps}


def _commission_with_cap(
    *,
    shares: float,
    trade_value: float,
    per_share: float,
    min_per_order: float,
    max_pct_trade_value: float,
) -> float:
    if shares <= 0 or trade_value <= 0:
        return 0.0
    commission = max(shares * per_share, min_per_order)
    if max_pct_trade_value > 0:
        commission = min(commission, trade_value * max_pct_trade_value)
    return float(commission)


def calculate_fee(
    *,
    side: str,
    quantity: float,
    fill_price: float,
    execution: dict[str, Any],
) -> dict[str, float | str]:
    """Calculate broker/third-party fees for one filled order."""
    model = str(execution.get("fee_model") or "simple_bps").lower()
    side = str(side or "").upper()
    shares = abs(float(quantity or 0.0))
    price = float(fill_price or 0.0)
    trade_value = shares * price
    if shares <= 0 or price <= 0:
        return {"model": model, "total_fee": 0.0}

    components: dict[str, float | str] = {"model": model}
    if model in ("simple_bps", "bps"):
        bps = float(execution.get("commission_bps", _nested_get(execution, "fees.simple_bps", 0.0)) or 0.0)
        fee = trade_value * bps / 10000.0
        components.update({"broker_commission": fee, "total_fee": fee})
        return components

    if model not in ("ibkr_us_pro_fixed", "ibkr_us_pro_tiered", "ibkr_us_lite"):
        bps = float(execution.get("commission_bps", 0.0) or 0.0)
        fee = trade_value * bps / 10000.0
        components.update({"broker_commission": fee, "total_fee": fee})
        return components

    fee_cfg = _nested_get(execution, f"fees.{model}", {})
    broker_commission = _commission_with_cap(
        shares=shares,
        trade_value=trade_value,
        per_share=float(fee_cfg.get("per_share", 0.0) or 0.0),
        min_per_order=float(fee_cfg.get("min_per_order", 0.0) or 0.0),
        max_pct_trade_value=float(fee_cfg.get("max_pct_trade_value", 0.0) or 0.0),
    )

    regulatory = _nested_get(execution, "fees.regulatory", {})
    clearing = _nested_get(execution, "fees.clearing", {})
    pass_through = _nested_get(execution, "fees.pass_through", {})

    sec_fee = 0.0
    taf_fee = 0.0
    if bool(_nested_get(execution, "fees.include_regulatory", True)) and side == "SELL":
        sec_fee = trade_value * float(regulatory.get("sec_sell_rate", 0.0) or 0.0)
        taf_fee = min(
            shares * float(regulatory.get("finra_taf_per_share", 0.0) or 0.0),
            float(regulatory.get("finra_taf_cap", float("inf")) or float("inf")),
        )

    cat_fee = 0.0
    if bool(_nested_get(execution, "fees.include_cat", True)):
        cat_fee = shares * float(regulatory.get("finra_cat_per_share", 0.0) or 0.0)

    clearing_fee = 0.0
    if bool(_nested_get(execution, "fees.include_clearing", True)):
        raw_clearing = shares * float(clearing.get("nscc_dtc_per_share", 0.0) or 0.0)
        clearing_cap = trade_value * float(clearing.get("nscc_dtc_cap_pct_trade_value", 1.0) or 1.0)
        clearing_fee = min(raw_clearing, clearing_cap)

    pass_fee = 0.0
    if bool(_nested_get(execution, "fees.include_pass_through", True)):
        pass_fee = broker_commission * (
            float(pass_through.get("nyse_rate_on_commission", 0.0) or 0.0)
            + float(pass_through.get("finra_rate_on_commission", 0.0) or 0.0)
        )

    exchange_fee = trade_value * float(_nested_get(execution, "fees.exchange_fee_bps", 0.0) or 0.0) / 10000.0
    total = broker_commission + sec_fee + taf_fee + cat_fee + clearing_fee + pass_fee + exchange_fee
    components.update({
        "broker_commission": float(broker_commission),
        "sec_fee": float(sec_fee),
        "finra_taf": float(taf_fee),
        "finra_cat": float(cat_fee),
        "clearing_fee": float(clearing_fee),
        "pass_through_fee": float(pass_fee),
        "exchange_fee": float(exchange_fee),
        "total_fee": float(total),
    })
    return components


def calculate_execution(
    *,
    side: str,
    quantity: float,
    raw_price: float,
    volume: float | None,
    execution: dict[str, Any],
) -> dict[str, Any]:
    """Calculate fill price, slippage cost, and fees for one order."""
    side = str(side or "").upper()
    qty = abs(float(quantity or 0.0))
    raw = float(raw_price or 0.0)
    slip = calculate_slippage_bps(
        side=side,
        quantity=qty,
        raw_price=raw,
        volume=volume,
        execution=execution,
    )
    slip_bps = float(slip["slippage_bps"])
    sign = 1.0 if side == "BUY" else -1.0
    fill_price = raw * (1.0 + sign * slip_bps / 10000.0)
    slippage_cost = qty * raw * slip_bps / 10000.0
    fee = calculate_fee(side=side, quantity=qty, fill_price=fill_price, execution=execution)
    total_fee = float(fee.get("total_fee", 0.0) or 0.0)
    return {
        "side": side,
        "quantity": qty,
        "raw_price": raw,
        "fill_price": fill_price,
        "notional": qty * fill_price,
        "raw_notional": qty * raw,
        "slippage_bps": slip_bps,
        "slippage_cost": float(slippage_cost),
        "participation_rate": float(slip.get("participation_rate", 0.0) or 0.0),
        "impact_bps": float(slip.get("impact_bps", 0.0) or 0.0),
        "slippage_model": slip.get("model", ""),
        "fee_model": fee.get("model", ""),
        "fee": total_fee,
        "fee_components": fee,
        "total_cost": float(slippage_cost + total_fee),
    }


def max_buy_quantity_for_cash(
    *,
    cash: float,
    requested_quantity: float,
    raw_price: float,
    volume: float | None,
    execution: dict[str, Any],
) -> int:
    """Find the largest integer buy quantity that fits available cash."""
    high = max(0, int(math.floor(float(requested_quantity or 0.0))))
    cash = float(cash or 0.0)
    if high <= 0 or cash <= 0 or raw_price <= 0:
        return 0
    low = 0
    best = 0
    while low <= high:
        mid = (low + high) // 2
        ex = calculate_execution(
            side="BUY",
            quantity=mid,
            raw_price=raw_price,
            volume=volume,
            execution=execution,
        )
        required = float(ex["notional"]) + float(ex["fee"])
        if required <= cash + 1e-9:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best
