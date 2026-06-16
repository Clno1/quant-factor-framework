"""Manual runner for internal paper trading accounts."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd

from src.papertrading.definition import (
    ORDER_FILLED,
    ORDER_PENDING,
    ORDER_REJECTED,
    STATUS_ACTIVE,
    account_strategy,
    now_iso,
)
from src.papertrading.store import load_account, load_table, save_table, update_account
from src.papertrading.target import TargetResult, generate_target_weights
from src.execution import (
    calculate_execution,
    max_buy_quantity_for_cash,
    resolve_execution_config,
)
from src.utils.logger import get_logger

log = get_logger(__name__)


def _empty_positions() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "ticker", "quantity", "avg_price", "market_price", "market_value",
        "cost_basis", "unrealized_pnl", "weight", "updated_at",
    ])


def _positions_to_map(positions: pd.DataFrame) -> dict[str, dict[str, float]]:
    if positions is None or positions.empty:
        return {}
    out: dict[str, dict[str, float]] = {}
    for row in positions.itertuples(index=False):
        qty = float(getattr(row, "quantity", 0.0) or 0.0)
        if qty <= 1e-12:
            continue
        ticker = str(getattr(row, "ticker"))
        avg = float(getattr(row, "avg_price", 0.0) or 0.0)
        out[ticker] = {"quantity": qty, "avg_price": avg}
    return out


def _positions_from_map(
    pos: dict[str, dict[str, float]],
    *,
    prices: pd.Series,
    equity: float,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    now = now_iso()
    for ticker in sorted(pos):
        qty = float(pos[ticker].get("quantity", 0.0) or 0.0)
        if qty <= 1e-12:
            continue
        avg = float(pos[ticker].get("avg_price", 0.0) or 0.0)
        px = float(prices.get(ticker, np.nan))
        mv = qty * px if np.isfinite(px) and px > 0 else 0.0
        cost_basis = qty * avg
        rows.append({
            "ticker": ticker,
            "quantity": qty,
            "avg_price": avg,
            "market_price": px if np.isfinite(px) else np.nan,
            "market_value": mv,
            "cost_basis": cost_basis,
            "unrealized_pnl": mv - cost_basis if mv else np.nan,
            "weight": mv / equity if equity > 0 else 0.0,
            "updated_at": now,
        })
    return pd.DataFrame(rows) if rows else _empty_positions()


def _latest_price_row(prices: pd.DataFrame, asof: str | None = None) -> tuple[str | None, pd.Series]:
    if prices is None or prices.empty:
        return None, pd.Series(dtype="float64")
    px = prices.dropna(how="all")
    if px.empty:
        return None, pd.Series(dtype="float64")
    if asof:
        px = px.loc[px.index <= pd.Timestamp(asof)]
        if px.empty:
            return None, pd.Series(dtype="float64")
    dt = pd.Timestamp(px.index.max())
    return dt.strftime("%Y-%m-%d"), px.loc[dt]


def _first_open_after(
    open_prices: pd.DataFrame,
    decision_date: str,
    ticker: str,
) -> tuple[str | None, float | None]:
    if open_prices is None or open_prices.empty or ticker not in open_prices.columns:
        return None, None
    after = open_prices.loc[open_prices.index > pd.Timestamp(decision_date), ticker].dropna()
    after = after[after > 0]
    if after.empty:
        return None, None
    dt = pd.Timestamp(after.index.min())
    return dt.strftime("%Y-%m-%d"), float(after.iloc[0])


def _bar_volume(
    volumes: pd.DataFrame | None,
    date: str,
    ticker: str,
) -> float | None:
    if volumes is None or volumes.empty or ticker not in volumes.columns:
        return None
    try:
        value = volumes.loc[pd.Timestamp(date), ticker]
    except KeyError:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) and value > 0 else None


def _mark_equity(
    *,
    account_id: str,
    cash: float,
    positions_map: dict[str, dict[str, float]],
    latest_prices: pd.Series,
    mark_date: str | None,
) -> tuple[pd.DataFrame, float]:
    provisional_value = 0.0
    for ticker, p in positions_map.items():
        px = float(latest_prices.get(ticker, np.nan))
        if np.isfinite(px) and px > 0:
            provisional_value += float(p.get("quantity", 0.0)) * px
    equity = float(cash) + provisional_value
    positions_df = _positions_from_map(positions_map, prices=latest_prices, equity=equity)
    save_table(account_id, "positions", positions_df)
    if mark_date:
        row = pd.DataFrame([{
            "date": mark_date,
            "cash": float(cash),
            "positions_value": float(provisional_value),
            "equity": float(equity),
            "updated_at": now_iso(),
        }])
        existing = load_table(account_id, "equity_curve")
        if not existing.empty and "date" in existing.columns:
            existing = existing[existing["date"].astype(str) != str(mark_date)]
            equity_curve = pd.concat([existing, row], ignore_index=True)
        else:
            equity_curve = row
        equity_curve = equity_curve.sort_values("date").reset_index(drop=True)
        save_table(account_id, "equity_curve", equity_curve)
    return positions_df, equity


def _fill_pending_orders(
    *,
    account: dict[str, Any],
    target: TargetResult,
    cash: float,
    positions_map: dict[str, dict[str, float]],
) -> tuple[float, list[dict[str, Any]], pd.DataFrame]:
    account_id = str(account["id"])
    orders = load_table(account_id, "orders")
    if orders.empty:
        return cash, [], orders
    if "status" not in orders.columns:
        return cash, [], orders

    execution = resolve_execution_config(account.get("execution") or {})
    fills: list[dict[str, Any]] = []

    pending_idx = orders.index[orders["status"].astype(str) == ORDER_PENDING].tolist()
    # Sells first, then buys, so rebalances can fund purchases.
    pending_idx.sort(key=lambda i: 0 if str(orders.loc[i, "side"]) == "SELL" else 1)
    for idx in pending_idx:
        row = orders.loc[idx]
        ticker = str(row.get("ticker") or "")
        side = str(row.get("side") or "").upper()
        decision_date = str(row.get("decision_date") or "")
        qty_requested = float(row.get("quantity", 0.0) or 0.0)
        if not ticker or side not in ("BUY", "SELL") or qty_requested <= 0:
            orders.loc[idx, "status"] = ORDER_REJECTED
            orders.loc[idx, "reject_reason"] = "invalid_order"
            continue
        fill_date, raw_open = _first_open_after(target.open_prices, decision_date, ticker)
        if fill_date is None or raw_open is None:
            continue
        volume = _bar_volume(target.volumes, fill_date, ticker)
        if side == "SELL":
            held_qty = float(positions_map.get(ticker, {}).get("quantity", 0.0) or 0.0)
            qty = min(qty_requested, held_qty)
            if qty <= 1e-12:
                orders.loc[idx, "status"] = ORDER_REJECTED
                orders.loc[idx, "reject_reason"] = "insufficient_position"
                orders.loc[idx, "filled_at"] = now_iso()
                continue
            ex = calculate_execution(
                side="SELL",
                quantity=qty,
                raw_price=raw_open,
                volume=volume,
                execution=execution,
            )
            fill_price = float(ex["fill_price"])
            gross = float(ex["notional"])
            fee = float(ex["fee"])
            cash += gross - fee
            avg = float(positions_map[ticker].get("avg_price", 0.0) or 0.0)
            remaining = held_qty - qty
            if remaining <= 1e-12:
                positions_map.pop(ticker, None)
            else:
                positions_map[ticker] = {"quantity": remaining, "avg_price": avg}
            realized = (fill_price - avg) * qty - fee
        else:
            max_qty = max_buy_quantity_for_cash(
                cash=cash,
                requested_quantity=qty_requested,
                raw_price=raw_open,
                volume=volume,
                execution=execution,
            )
            qty = min(int(qty_requested), max_qty)
            if qty <= 0:
                orders.loc[idx, "status"] = ORDER_REJECTED
                orders.loc[idx, "reject_reason"] = "insufficient_cash"
                orders.loc[idx, "filled_at"] = now_iso()
                continue
            ex = calculate_execution(
                side="BUY",
                quantity=qty,
                raw_price=raw_open,
                volume=volume,
                execution=execution,
            )
            fill_price = float(ex["fill_price"])
            gross = float(ex["notional"])
            fee = float(ex["fee"])
            cash -= gross + fee
            old = positions_map.get(ticker, {"quantity": 0.0, "avg_price": 0.0})
            old_qty = float(old.get("quantity", 0.0) or 0.0)
            old_avg = float(old.get("avg_price", 0.0) or 0.0)
            new_qty = old_qty + qty
            new_avg = ((old_qty * old_avg) + gross) / new_qty if new_qty > 0 else 0.0
            positions_map[ticker] = {"quantity": new_qty, "avg_price": new_avg}
            realized = 0.0

        notional = float(ex["notional"])
        fee_components = ex.get("fee_components") or {}
        fill_id = str(uuid4())
        order_id = str(row.get("order_id") or "")
        fills.append({
            "fill_id": fill_id,
            "order_id": order_id,
            "account_id": account_id,
            "ticker": ticker,
            "side": side,
            "quantity": int(qty),
            "raw_open_price": float(raw_open),
            "fill_price": float(fill_price),
            "notional": float(notional),
            "bar_volume": float(volume) if volume is not None else np.nan,
            "participation_rate": float(ex.get("participation_rate", 0.0) or 0.0),
            "slippage_model": str(ex.get("slippage_model") or execution.get("slippage_model")),
            "slippage_bps": float(ex.get("slippage_bps", 0.0) or 0.0),
            "impact_bps": float(ex.get("impact_bps", 0.0) or 0.0),
            "slippage_cost": float(ex.get("slippage_cost", 0.0) or 0.0),
            "fee_model": str(ex.get("fee_model") or execution.get("fee_model")),
            "broker_commission": float(fee_components.get("broker_commission", 0.0) or 0.0),
            "sec_fee": float(fee_components.get("sec_fee", 0.0) or 0.0),
            "finra_taf": float(fee_components.get("finra_taf", 0.0) or 0.0),
            "finra_cat": float(fee_components.get("finra_cat", 0.0) or 0.0),
            "clearing_fee": float(fee_components.get("clearing_fee", 0.0) or 0.0),
            "pass_through_fee": float(fee_components.get("pass_through_fee", 0.0) or 0.0),
            "exchange_fee": float(fee_components.get("exchange_fee", 0.0) or 0.0),
            "fee": float(fee),
            "total_cost_cash": float(ex.get("total_cost", 0.0) or 0.0),
            "realized_pnl": float(realized),
            "decision_date": decision_date,
            "fill_date": fill_date,
            "filled_at": now_iso(),
        })
        orders.loc[idx, "status"] = ORDER_FILLED
        orders.loc[idx, "filled_quantity"] = int(qty)
        orders.loc[idx, "fill_price"] = float(fill_price)
        orders.loc[idx, "fill_date"] = fill_date
        orders.loc[idx, "filled_at"] = now_iso()
        orders.loc[idx, "bar_volume"] = float(volume) if volume is not None else np.nan
        orders.loc[idx, "slippage_model"] = str(ex.get("slippage_model") or execution.get("slippage_model"))
        orders.loc[idx, "slippage_bps"] = float(ex.get("slippage_bps", 0.0) or 0.0)
        orders.loc[idx, "slippage_cost"] = float(ex.get("slippage_cost", 0.0) or 0.0)
        orders.loc[idx, "fee_model"] = str(ex.get("fee_model") or execution.get("fee_model"))
        orders.loc[idx, "fee"] = float(fee)
        orders.loc[idx, "total_cost_cash"] = float(ex.get("total_cost", 0.0) or 0.0)
        orders.loc[idx, "reject_reason"] = ""

    save_table(account_id, "orders", orders)
    if fills:
        existing = load_table(account_id, "fills")
        fills_df = pd.DataFrame(fills)
        out = pd.concat([existing, fills_df], ignore_index=True) if not existing.empty else fills_df
        save_table(account_id, "fills", out)
    return cash, fills, orders


def _create_rebalance_orders(
    *,
    account: dict[str, Any],
    target: TargetResult,
    cash: float,
    positions_df: pd.DataFrame,
    equity: float,
) -> list[dict[str, Any]]:
    account_id = str(account["id"])
    decision_date = target.decision_date
    existing = load_table(account_id, "orders")
    if not existing.empty and "decision_date" in existing.columns:
        same_day = existing[existing["decision_date"].astype(str) == decision_date]
        if not same_day.empty:
            return []

    execution = account.get("execution") or {}
    min_order_value = float(execution.get("min_order_value", 25.0) or 0.0)
    current_values: dict[str, float] = {}
    if positions_df is not None and not positions_df.empty:
        for row in positions_df.itertuples(index=False):
            current_values[str(row.ticker)] = float(getattr(row, "market_value", 0.0) or 0.0)

    latest_price_date, latest_prices = _latest_price_row(target.prices, asof=decision_date)
    _ = latest_price_date
    target_by_ticker = {
        str(row.ticker): float(row.target_weight)
        for row in target.target_weights.itertuples(index=False)
        if float(row.target_weight) > 0
    }
    all_tickers = sorted(set(current_values) | set(target_by_ticker))
    rows: list[dict[str, Any]] = []
    for ticker in all_tickers:
        target_value = equity * target_by_ticker.get(ticker, 0.0)
        current_value = current_values.get(ticker, 0.0)
        delta_value = target_value - current_value
        if abs(delta_value) < min_order_value:
            continue
        ref_price = float(latest_prices.get(ticker, np.nan))
        if not np.isfinite(ref_price) or ref_price <= 0:
            continue
        qty = int(abs(delta_value) // ref_price)
        if qty <= 0:
            continue
        side = "BUY" if delta_value > 0 else "SELL"
        rows.append({
            "order_id": str(uuid4()),
            "account_id": account_id,
            "created_at": now_iso(),
            "decision_date": decision_date,
            "execute_after": decision_date,
            "execution_timing": "next_open",
            "ticker": ticker,
            "side": side,
            "quantity": qty,
            "filled_quantity": 0,
            "ref_price": ref_price,
            "target_weight": target_by_ticker.get(ticker, 0.0),
            "current_value": current_value,
            "target_value": target_value,
            "estimated_notional": qty * ref_price,
            "status": ORDER_PENDING,
            "fill_date": None,
            "fill_price": None,
            "fee": 0.0,
            "reject_reason": "",
        })

    if rows:
        out = pd.concat([existing, pd.DataFrame(rows)], ignore_index=True) if not existing.empty else pd.DataFrame(rows)
        save_table(account_id, "orders", out)
    return rows


def run_account_once(account_id: str, *, asof: str | None = None) -> dict[str, Any]:
    """Run one manual paper-trading cycle for an account."""
    account = load_account(account_id)
    if account is None:
        raise FileNotFoundError(f"Paper account not found: {account_id}")
    if account.get("status") != STATUS_ACTIVE:
        raise ValueError("模拟盘账户不是 active 状态，不能运行")

    try:
        strategy = account_strategy(account)
        target = generate_target_weights(
            strategy=strategy,
            universe=str(account["universe"]),
            watchlist_snapshot=account.get("watchlist_snapshot"),
            asof=asof,
            n_groups=int(account.get("n_groups") or 5),
            top_group=int(account.get("top_group") or account.get("n_groups") or 5),
        )
        positions = load_table(account_id, "positions")
        positions_map = _positions_to_map(positions)
        cash = float(account.get("cash", account.get("initial_cash", 0.0)) or 0.0)

        cash, fills, _ = _fill_pending_orders(
            account=account,
            target=target,
            cash=cash,
            positions_map=positions_map,
        )
        mark_date, latest_prices = _latest_price_row(target.prices, asof=asof)
        positions_df, equity = _mark_equity(
            account_id=account_id,
            cash=cash,
            positions_map=positions_map,
            latest_prices=latest_prices,
            mark_date=mark_date,
        )
        new_orders = _create_rebalance_orders(
            account=account,
            target=target,
            cash=cash,
            positions_df=positions_df,
            equity=equity,
        )
        target_table = target.target_weights.copy()
        target_table["account_id"] = account_id
        target_table["generated_at"] = now_iso()
        save_table(account_id, "target_weights", target_table)

        orders_current = load_table(account_id, "orders")
        pending_count = (
            int((orders_current["status"].astype(str) == ORDER_PENDING).sum())
            if not orders_current.empty and "status" in orders_current.columns else 0
        )
        run_row = {
            "run_id": str(uuid4()),
            "account_id": account_id,
            "run_at": now_iso(),
            "decision_date": target.decision_date,
            "mark_date": mark_date,
            "cash": float(cash),
            "equity": float(equity),
            "fills_count": len(fills),
            "orders_created": len(new_orders),
            "pending_orders": pending_count,
            "tickers_used": len(target.tickers_used),
            "tickers_missing": len(target.tickers_missing),
            "error": "",
        }
        runs = load_table(account_id, "runs")
        runs = pd.concat([runs, pd.DataFrame([run_row])], ignore_index=True) if not runs.empty else pd.DataFrame([run_row])
        save_table(account_id, "runs", runs)

        diagnostics = {
            "normalized_weights": target.normalized_weights,
            "effective_n_groups": target.effective_n_groups,
            "top_group": target.top_group,
            "tickers_used": len(target.tickers_used),
            "tickers_missing": target.tickers_missing,
            "warnings": target.warnings,
            "last_orders_created": len(new_orders),
            "last_fills_count": len(fills),
            "pending_orders": pending_count,
        }
        account = update_account(account_id, {
            "cash": float(cash),
            "last_equity": float(equity),
            "last_run_at": run_row["run_at"],
            "last_decision_date": target.decision_date,
            "last_mark_date": mark_date,
            "last_error": None,
            "diagnostics": diagnostics,
        })
        return {
            "account": account,
            "run": run_row,
            "diagnostics": diagnostics,
        }
    except Exception as e:  # noqa: BLE001
        log.exception("Paper account run failed: account_id=%s error=%s", account_id, e)
        try:
            update_account(account_id, {
                "last_run_at": datetime.now().isoformat(timespec="seconds"),
                "last_error": f"{type(e).__name__}: {e}",
            })
        except Exception:  # noqa: BLE001
            pass
        raise


__all__ = ["run_account_once"]
