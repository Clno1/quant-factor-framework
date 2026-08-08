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


def _nested_set(d: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = d
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _validated_number(
    cfg: dict[str, Any],
    path: str,
    *,
    default: float,
    minimum: float = 0.0,
    maximum: float | None = None,
    strictly_positive: bool = False,
) -> float:
    raw = _nested_get(cfg, path, default)
    try:
        value = float(default if raw is None else raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"execution.{path} must be numeric") from exc
    if not math.isfinite(value):
        raise ValueError(f"execution.{path} must be finite")
    if strictly_positive and value <= minimum:
        raise ValueError(f"execution.{path} must be greater than {minimum}")
    if not strictly_positive and value < minimum:
        raise ValueError(f"execution.{path} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"execution.{path} must be at most {maximum}")
    _nested_set(cfg, path, value)
    return value


def _validated_bool(cfg: dict[str, Any], path: str, *, default: bool) -> bool:
    raw = _nested_get(cfg, path, default)
    if isinstance(raw, bool):
        value = raw
    elif isinstance(raw, int) and raw in {0, 1}:
        value = bool(raw)
    elif isinstance(raw, str) and raw.strip().casefold() in {
        "true", "false", "1", "0",
    }:
        value = raw.strip().casefold() in {"true", "1"}
    else:
        raise ValueError(f"execution.{path} must be boolean")
    _nested_set(cfg, path, value)
    return value


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
            "adv_window": 20,
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

    _validated_number(
        cfg,
        "portfolio_value",
        default=100000.0,
        strictly_positive=True,
    )
    for path, default in (
        ("slippage_bps", 0.0),
        ("commission_bps", 0.0),
        ("slippage.fallback_bps", 0.0),
        ("slippage.spread_bps", 0.0),
        ("slippage.price_impact", 0.0),
        ("fees.simple_bps", 0.0),
        ("fees.exchange_fee_bps", 0.0),
        ("fees.ibkr_us_pro_fixed.per_share", 0.0),
        ("fees.ibkr_us_pro_fixed.min_per_order", 0.0),
        ("fees.ibkr_us_pro_fixed.max_pct_trade_value", 0.0),
        ("fees.ibkr_us_pro_tiered.per_share", 0.0),
        ("fees.ibkr_us_pro_tiered.min_per_order", 0.0),
        ("fees.ibkr_us_pro_tiered.max_pct_trade_value", 0.0),
        ("fees.ibkr_us_lite.per_share", 0.0),
        ("fees.ibkr_us_lite.min_per_order", 0.0),
        ("fees.ibkr_us_lite.max_pct_trade_value", 0.0),
        ("fees.regulatory.sec_sell_rate", 0.0),
        ("fees.regulatory.finra_taf_per_share", 0.0),
        ("fees.regulatory.finra_taf_cap", 0.0),
        ("fees.regulatory.finra_cat_per_share", 0.0),
        ("fees.clearing.nscc_dtc_per_share", 0.0),
        ("fees.clearing.nscc_dtc_cap_pct_trade_value", 0.0),
        ("fees.pass_through.nyse_rate_on_commission", 0.0),
        ("fees.pass_through.finra_rate_on_commission", 0.0),
    ):
        if path.endswith(("max_pct_trade_value", "cap_pct_trade_value")):
            maximum = 1.0
        elif path in {
            "slippage_bps",
            "commission_bps",
            "slippage.fallback_bps",
            "slippage.spread_bps",
            "fees.simple_bps",
            "fees.exchange_fee_bps",
        }:
            maximum = 1000.0
        else:
            maximum = None
        _validated_number(
            cfg,
            path,
            default=default,
            maximum=maximum,
        )
    _validated_number(
        cfg,
        "slippage.volume_limit",
        default=0.025,
        maximum=1.0,
    )
    _validated_number(
        cfg,
        "min_open_coverage",
        default=0.95,
        maximum=1.0,
    )
    if "min_order_value" in cfg:
        _validated_number(
            cfg,
            "min_order_value",
            default=0.0,
        )
    adv_window = _validated_number(
        cfg,
        "slippage.adv_window",
        default=20.0,
        strictly_positive=True,
    )
    if not float(adv_window).is_integer():
        raise ValueError("execution.slippage.adv_window must be an integer")
    _nested_set(cfg, "slippage.adv_window", int(adv_window))
    for path in (
        "fees.include_regulatory",
        "fees.include_cat",
        "fees.include_clearing",
        "fees.include_pass_through",
    ):
        _validated_bool(cfg, path, default=True)
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
        volume_limit_value = _nested_get(
            execution,
            "slippage.volume_limit",
            0.025,
        )
        price_impact_value = _nested_get(
            execution,
            "slippage.price_impact",
            0.10,
        )
        volume_limit = float(
            0.025 if volume_limit_value is None else volume_limit_value
        )
        price_impact = float(
            0.10 if price_impact_value is None else price_impact_value
        )
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


def max_volume_fill_quantity(
    *,
    requested_quantity: float,
    volume: float | None,
    execution: dict[str, Any],
) -> float:
    """Return the maximum quantity allowed by the configured volume share."""
    requested = max(0.0, abs(float(requested_quantity or 0.0)))
    model = str(execution.get("slippage_model") or "").lower()
    if model != "volume_share":
        return requested
    try:
        reference_volume = float(volume) if volume is not None else 0.0
    except (TypeError, ValueError):
        reference_volume = 0.0
    if not math.isfinite(reference_volume) or reference_volume <= 0:
        # A volume-share model cannot prove liquidity without a volume
        # reference. Returning zero makes callers reject or defer the fill
        # instead of silently treating the cap as unlimited.
        return 0.0
    limit_value = _nested_get(execution, "slippage.volume_limit", 0.025)
    volume_limit = float(0.025 if limit_value is None else limit_value)
    return min(requested, max(0.0, reference_volume * volume_limit))


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
    qty = float(quantity or 0.0)
    raw = float(raw_price or 0.0)
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if not math.isfinite(qty) or not math.isfinite(raw) or qty < 0 or raw <= 0:
        raise ValueError(
            "quantity must be finite and non-negative; raw_price must be positive"
        )
    slip = calculate_slippage_bps(
        side=side,
        quantity=qty,
        raw_price=raw,
        volume=volume,
        execution=execution,
    )
    slip_bps = float(slip["slippage_bps"])
    if not math.isfinite(slip_bps) or slip_bps < 0 or slip_bps >= 10000:
        raise ValueError(
            "calculated slippage must be finite and below 10000 bps"
        )
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
