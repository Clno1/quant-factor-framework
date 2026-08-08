"""Manual runner for internal paper trading accounts."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd

from src.backtest.rebalance import get_rebalance_dates
from src.config import CONFIG
from src.data.access import (
    MarketDataNotReadyError,
    enqueue_market_data_request,
    watchlist_universe_frame,
)
from src.data.universe_ids import watchlist_snapshot_data_universe
from src.decision_replay import build_paper_snapshot, upsert_snapshot
from src.papertrading.definition import (
    ORDER_FILLED,
    ORDER_PENDING,
    ORDER_REJECTED,
    STATUS_ACTIVE,
    account_strategy,
    now_iso,
)
from src.papertrading.store import (
    account_dir,
    account_run_lock,
    load_account,
    load_table,
    save_table,
    update_account,
)
from src.papertrading.target import TargetResult, generate_target_weights
from src.execution import (
    calculate_execution,
    max_volume_fill_quantity,
    max_buy_quantity_for_cash,
    resolve_execution_config,
)
from src.utils.logger import get_logger
from src.utils.market_calendar import latest_completed_xnys_session
from src.utils.date_utils import resolve_date_range

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


def _state_from_fill_ledger(
    account: dict[str, Any],
    fills: pd.DataFrame,
) -> tuple[float, dict[str, dict[str, float]]]:
    """Rebuild cash and positions from the append-only fill ledger."""
    cash = float(account.get("initial_cash", 0.0) or 0.0)
    positions: dict[str, dict[str, float]] = {}
    if fills is None or fills.empty:
        return cash, positions
    if "fill_id" in fills.columns and fills["fill_id"].astype(str).duplicated().any():
        raise ValueError("Duplicate fill_id detected in paper-trading ledger")

    ledger = fills.copy()
    sort_columns = [
        column
        for column in ("fill_date", "filled_at", "fill_id")
        if column in ledger.columns
    ]
    if sort_columns:
        ledger = ledger.sort_values(sort_columns, kind="stable")
    for row in ledger.to_dict(orient="records"):
        ticker = str(row.get("ticker") or "")
        side = str(row.get("side") or "").upper()
        quantity = float(row.get("quantity", 0.0) or 0.0)
        fill_price = float(row.get("fill_price", 0.0) or 0.0)
        notional = float(
            row.get("notional", quantity * fill_price)
            or quantity * fill_price
        )
        fee = float(row.get("fee", 0.0) or 0.0)
        if not ticker or side not in {"BUY", "SELL"} or quantity <= 0:
            raise ValueError(f"Invalid fill ledger row: {row}")
        if side == "BUY":
            cash -= notional + fee
            old = positions.get(
                ticker,
                {"quantity": 0.0, "avg_price": 0.0},
            )
            old_quantity = float(old["quantity"])
            new_quantity = old_quantity + quantity
            average = (
                (old_quantity * float(old["avg_price"]) + notional)
                / new_quantity
            )
            positions[ticker] = {
                "quantity": new_quantity,
                "avg_price": average,
            }
        else:
            old = positions.get(ticker)
            held = float((old or {}).get("quantity", 0.0) or 0.0)
            if quantity > held + 1e-9:
                raise ValueError(
                    f"Fill ledger oversells {ticker}: sell={quantity}, held={held}"
                )
            cash += notional - fee
            remaining = held - quantity
            if remaining <= 1e-9:
                positions.pop(ticker, None)
            else:
                positions[ticker] = {
                    "quantity": remaining,
                    "avg_price": float(old["avg_price"]),
                }
    return float(cash), positions


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
        if not np.isfinite(px) or px <= 0:
            raise ValueError(
                f"Cannot mark held position {ticker}: no price at or before mark date"
            )
        mv = qty * px
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
    return dt.strftime("%Y-%m-%d"), px.ffill().loc[dt]


def _first_open_after(
    open_prices: pd.DataFrame,
    decision_date: str,
    ticker: str,
    *,
    cutoff: str,
    after_date: str | None = None,
) -> tuple[str | None, float | None]:
    if open_prices is None or open_prices.empty or ticker not in open_prices.columns:
        return None, None
    lower_bound = max(
        pd.Timestamp(decision_date),
        pd.Timestamp(after_date) if after_date else pd.Timestamp(decision_date),
    )
    after = open_prices.loc[
        (open_prices.index > lower_bound)
        & (open_prices.index <= pd.Timestamp(cutoff)),
        ticker,
    ].dropna()
    after = after[after > 0]
    if after.empty:
        return None, None
    dt = pd.Timestamp(after.index.min())
    return dt.strftime("%Y-%m-%d"), float(after.iloc[0])


def _trailing_volume_before(
    volumes: pd.DataFrame | None,
    date: str,
    ticker: str,
    *,
    window: int,
) -> float | None:
    if volumes is None or volumes.empty or ticker not in volumes.columns:
        return None
    values = pd.to_numeric(
        volumes.loc[volumes.index < pd.Timestamp(date), ticker],
        errors="coerce",
    )
    values = values[np.isfinite(values) & (values > 0)].tail(max(1, window))
    return float(values.mean()) if not values.empty else None


def _is_rebalance_decision(
    account: dict[str, Any],
    target: TargetResult,
) -> bool:
    decision = pd.Timestamp(target.decision_date)
    mode = str(account.get("rebalance_mode") or "month_end").lower()
    if mode in {"every_n_days", "n_days", "interval"}:
        step = int(
            account.get("rebalance_days")
            or getattr(CONFIG.backtest, "rebalance_days", 5)
        )
        dates = pd.DatetimeIndex(target.composite.index)
        return decision in get_rebalance_dates(
            dates,
            mode="every_n_days",
            step_days=step,
        )

    future_dates = pd.DatetimeIndex(target.prices.index)
    future_dates = future_dates[future_dates > decision]
    next_date = pd.Timestamp(future_dates.min()) if len(future_dates) else None
    if next_date is None:
        try:
            import exchange_calendars as xcals

            calendar = xcals.get_calendar("XNYS")
            next_date = pd.Timestamp(calendar.next_session(decision))
        except (ImportError, ValueError):
            next_date = pd.Timestamp(decision + pd.offsets.BDay(1))
    if mode in {"month_end", "monthly"}:
        return next_date.to_period("M") != decision.to_period("M")
    if mode in {"week_end", "weekly"}:
        return next_date.to_period("W-FRI") != decision.to_period("W-FRI")
    raise ValueError(
        f"Unknown rebalance mode={mode!r}; "
        "expected every_n_days/month_end/week_end"
    )


def _expected_target_session(asof: str | None) -> pd.Timestamp:
    """Resolve the XNYS session a paper run is required to have processed."""
    if asof is None:
        return latest_completed_xnys_session()
    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise RuntimeError(
            "exchange-calendars is required for paper-trading freshness checks"
        ) from exc
    cutoff = pd.Timestamp(asof).tz_localize(None).normalize()
    calendar = xcals.get_calendar("XNYS")
    sessions = calendar.sessions_in_range(
        (cutoff - pd.Timedelta(days=14)).strftime("%Y-%m-%d"),
        cutoff.strftime("%Y-%m-%d"),
    )
    if len(sessions) == 0:
        raise ValueError(f"No XNYS session exists at or before asof={asof}")
    expected = pd.Timestamp(sessions[-1])
    if expected.tzinfo is not None:
        expected = expected.tz_localize(None)
    return expected.normalize()


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
        if not np.isfinite(px) or px <= 0:
            raise ValueError(
                f"Cannot mark held position {ticker}: "
                "no price at or before mark date"
            )
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
    cutoff: str,
) -> tuple[float, list[dict[str, Any]], pd.DataFrame]:
    account_id = str(account["id"])
    orders = load_table(account_id, "orders")
    if orders.empty:
        return cash, [], orders
    if "status" not in orders.columns:
        return cash, [], orders

    execution = resolve_execution_config(account.get("execution") or {})
    adv_window = int(
        ((execution.get("slippage") or {}).get("adv_window", 20)) or 20
    )
    existing_fills = load_table(account_id, "fills")
    fills: list[dict[str, Any]] = []

    pending_idx = orders.index[orders["status"].astype(str) == ORDER_PENDING].tolist()
    # Sells first, then buys, so rebalances can fund purchases.
    pending_idx.sort(key=lambda i: 0 if str(orders.loc[i, "side"]) == "SELL" else 1)
    for idx in pending_idx:
        row = orders.loc[idx]
        ticker = str(row.get("ticker") or "")
        side = str(row.get("side") or "").upper()
        decision_date = str(row.get("decision_date") or "")
        order_id = str(row.get("order_id") or "")
        total_requested = float(row.get("quantity", 0.0) or 0.0)
        if (
            not order_id
            or not ticker
            or side not in ("BUY", "SELL")
            or total_requested <= 0
        ):
            orders.loc[idx, "status"] = ORDER_REJECTED
            orders.loc[idx, "reject_reason"] = "invalid_order"
            continue

        prior = pd.DataFrame()
        if not existing_fills.empty and "order_id" in existing_fills.columns:
            prior = existing_fills[
                existing_fills["order_id"].astype(str) == order_id
            ]
        already_filled = (
            float(
                pd.to_numeric(
                    prior.get("quantity", pd.Series(dtype=float)),
                    errors="coerce",
                ).fillna(0.0).sum()
            )
            if not prior.empty
            else 0.0
        )
        remaining_requested = max(0.0, total_requested - already_filled)
        if remaining_requested <= 1e-9:
            orders.loc[idx, "status"] = ORDER_FILLED
            orders.loc[idx, "filled_quantity"] = int(round(already_filled))
            orders.loc[idx, "reject_reason"] = ""
            continue
        last_fill_date = (
            str(prior["fill_date"].dropna().astype(str).max())
            if not prior.empty
            and "fill_date" in prior.columns
            and prior["fill_date"].notna().any()
            else None
        )
        fill_date, raw_open = _first_open_after(
            target.open_prices,
            decision_date,
            ticker,
            cutoff=cutoff,
            after_date=last_fill_date,
        )
        if fill_date is None or raw_open is None:
            continue
        volume = _trailing_volume_before(
            target.volumes,
            fill_date,
            ticker,
            window=adv_window,
        )
        if (
            str(execution.get("slippage_model") or "").lower()
            == "volume_share"
            and volume is None
        ):
            raise ValueError(
                "Cannot apply volume-share execution without trailing volume: "
                f"ticker={ticker} fill_date={fill_date}"
            )
        volume_quantity = max_volume_fill_quantity(
            requested_quantity=remaining_requested,
            volume=volume,
            execution=execution,
        )
        quantity_limit = int(np.floor(volume_quantity + 1e-12))
        if quantity_limit <= 0:
            continue
        if side == "SELL":
            held_qty = float(positions_map.get(ticker, {}).get("quantity", 0.0) or 0.0)
            qty = min(remaining_requested, held_qty, quantity_limit)
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
                requested_quantity=min(remaining_requested, quantity_limit),
                raw_price=raw_open,
                volume=volume,
                execution=execution,
            )
            qty = min(int(remaining_requested), quantity_limit, max_qty)
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
        fill_record = {
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
            "volume_reference": f"ADV{adv_window}_before_fill",
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
        }
        fills.append(fill_record)
        cumulative_quantity = already_filled + float(qty)
        orders.loc[idx, "status"] = (
            ORDER_FILLED
            if cumulative_quantity >= total_requested - 1e-9
            else ORDER_PENDING
        )
        orders.loc[idx, "filled_quantity"] = int(round(cumulative_quantity))
        orders.loc[idx, "fill_price"] = float(fill_price)
        orders.loc[idx, "fill_date"] = fill_date
        orders.loc[idx, "last_fill_date"] = fill_date
        orders.loc[idx, "filled_at"] = now_iso()
        orders.loc[idx, "bar_volume"] = float(volume) if volume is not None else np.nan
        orders.loc[idx, "slippage_model"] = str(ex.get("slippage_model") or execution.get("slippage_model"))
        orders.loc[idx, "slippage_bps"] = float(ex.get("slippage_bps", 0.0) or 0.0)
        orders.loc[idx, "slippage_cost"] = float(ex.get("slippage_cost", 0.0) or 0.0)
        orders.loc[idx, "fee_model"] = str(ex.get("fee_model") or execution.get("fee_model"))
        prior_fee = (
            float(pd.to_numeric(prior["fee"], errors="coerce").fillna(0).sum())
            if not prior.empty and "fee" in prior.columns
            else 0.0
        )
        prior_cost = (
            float(
                pd.to_numeric(
                    prior["total_cost_cash"],
                    errors="coerce",
                ).fillna(0).sum()
            )
            if not prior.empty and "total_cost_cash" in prior.columns
            else 0.0
        )
        orders.loc[idx, "fee"] = prior_fee + float(fee)
        orders.loc[idx, "total_cost_cash"] = (
            prior_cost + float(ex.get("total_cost", 0.0) or 0.0)
        )
        orders.loc[idx, "reject_reason"] = ""

    if fills:
        fills_df = pd.DataFrame(fills)
        out = (
            pd.concat([existing_fills, fills_df], ignore_index=True)
            if not existing_fills.empty
            else fills_df
        )
        save_table(account_id, "fills", out)
    # Publish the order projection after fills. If this write fails, the next
    # run reconstructs filled_quantity from the durable fill ledger.
    save_table(account_id, "orders", orders)
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
    if (
        not existing.empty
        and "status" in existing.columns
        and existing["status"].astype(str).eq(ORDER_PENDING).any()
    ):
        log.info(
            "Skip rebalance order creation while prior orders are pending: "
            "account_id=%s decision_date=%s",
            account_id,
            decision_date,
        )
        return []
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


def _run_account_once_locked(
    account_id: str,
    *,
    asof: str | None = None,
) -> dict[str, Any]:
    """Run one cycle while the caller holds the account run lock."""
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
        expected_session = _expected_target_session(asof)
        actual_session = pd.Timestamp(target.decision_date).normalize()
        if actual_session != expected_session:
            raise ValueError(
                "Paper target data is stale or ahead of the requested as-of: "
                f"expected_session={expected_session.date()} "
                f"decision_date={actual_session.date()}. Refresh market data "
                "and factor artifacts before running the account."
            )
        if target.tickers_missing:
            raise ValueError(
                "Paper universe contains tickers with no usable OHLCV history: "
                f"{target.tickers_missing[:20]}"
            )
        fill_ledger = load_table(account_id, "fills")
        cash, positions_map = _state_from_fill_ledger(account, fill_ledger)

        cash, fills, _ = _fill_pending_orders(
            account=account,
            target=target,
            cash=cash,
            positions_map=positions_map,
            cutoff=target.decision_date,
        )
        # Re-read the durable ledger after execution. This also makes a retry
        # after a later projection write failure exactly idempotent.
        fill_ledger = load_table(account_id, "fills")
        cash, positions_map = _state_from_fill_ledger(account, fill_ledger)
        mark_date, latest_prices = _latest_price_row(
            target.prices,
            asof=target.decision_date,
        )
        positions_df, equity = _mark_equity(
            account_id=account_id,
            cash=cash,
            positions_map=positions_map,
            latest_prices=latest_prices,
            mark_date=mark_date,
        )
        is_rebalance = _is_rebalance_decision(account, target)
        new_orders: list[dict[str, Any]] = []
        if is_rebalance:
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
            target_history = load_table(account_id, "target_history")
            if (
                not target_history.empty
                and "decision_date" in target_history.columns
            ):
                target_history = target_history[
                    target_history["decision_date"].astype(str)
                    != str(target.decision_date)
                ]
            target_history = (
                pd.concat([target_history, target_table], ignore_index=True)
                if not target_history.empty
                else target_table.copy()
            )
            target_history = target_history.sort_values(
                ["decision_date", "ticker"]
            ).reset_index(drop=True)
            save_table(account_id, "target_history", target_history)

        position_history = load_table(account_id, "position_history")
        if not position_history.empty and "date" in position_history.columns:
            position_history = position_history[
                position_history["date"].astype(str)
                != str(mark_date or target.decision_date)
            ]
        position_rows = positions_df.copy()
        position_rows.insert(0, "date", mark_date or target.decision_date)
        position_rows["account_id"] = account_id
        position_history = (
            pd.concat([position_history, position_rows], ignore_index=True)
            if not position_history.empty
            else position_rows
        )
        save_table(account_id, "position_history", position_history)

        replay_snapshot = build_paper_snapshot(
            source_id=account_id,
            account=account,
            target=target,
            positions=positions_df,
            cash=cash,
            equity=equity,
            is_rebalance=is_rebalance,
        )
        decision_ts = pd.Timestamp(target.decision_date)
        fills_on_date = pd.DataFrame()
        if not fill_ledger.empty and "fill_date" in fill_ledger.columns:
            fills_on_date = fill_ledger[
                fill_ledger["fill_date"].astype(str)
                == target.decision_date
            ]
        if not fills_on_date.empty:
            replay_snapshot.daily_summary.loc[
                decision_ts,
                "total_fee",
            ] = float(
                pd.to_numeric(
                    fills_on_date.get("fee", pd.Series(dtype=float)),
                    errors="coerce",
                ).fillna(0.0).sum()
            )
            replay_snapshot.daily_summary.loc[
                decision_ts,
                "total_slippage_cost",
            ] = float(
                pd.to_numeric(
                    fills_on_date.get(
                        "slippage_cost",
                        pd.Series(dtype=float),
                    ),
                    errors="coerce",
                ).fillna(0.0).sum()
            )
            replay_snapshot.daily_summary.loc[
                decision_ts,
                "total_cost_cash",
            ] = float(
                pd.to_numeric(
                    fills_on_date.get(
                        "total_cost_cash",
                        pd.Series(dtype=float),
                    ),
                    errors="coerce",
                ).fillna(0.0).sum()
            )
            replay_snapshot.daily_summary.loc[
                decision_ts,
                "execution_date",
            ] = target.decision_date
        upsert_snapshot(account_dir(account_id), replay_snapshot)

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
            "expected_session": expected_session.strftime("%Y-%m-%d"),
            "mark_date": mark_date,
            "is_rebalance": bool(is_rebalance),
            "cash": float(cash),
            "equity": float(equity),
            "fills_count": len(fills),
            "orders_created": len(new_orders),
            "pending_orders": pending_count,
            "tickers_used": len(target.tickers_used),
            "tickers_missing": len(target.tickers_missing),
            "dataset_version_id": target.data_contract.get("dataset_version_id"),
            "factor_publication_id": target.data_contract.get(
                "factor_publication_id"
            ),
            "error": "",
        }
        runs = load_table(account_id, "runs")
        runs = (
            pd.concat([runs, pd.DataFrame([run_row])], ignore_index=True)
            if not runs.empty
            else pd.DataFrame([run_row])
        )
        save_table(account_id, "runs", runs)

        diagnostics = {
            "normalized_weights": target.normalized_weights,
            "effective_n_groups": target.effective_n_groups,
            "top_group": target.top_group,
            "tickers_used": len(target.tickers_used),
            "tickers_missing": target.tickers_missing,
            "warnings": target.warnings,
            "is_rebalance": bool(is_rebalance),
            "expected_session": expected_session.strftime("%Y-%m-%d"),
            "decision_replay": {
                "available": True,
                "schema_version": replay_snapshot.manifest["schema_version"],
            },
            "last_orders_created": len(new_orders),
            "last_fills_count": len(fills),
            "pending_orders": pending_count,
            "data_contract": target.data_contract,
        }
        account = update_account(
            account_id,
            {
                "cash": float(cash),
                "last_equity": float(equity),
                "last_run_at": run_row["run_at"],
                "last_decision_date": target.decision_date,
                "last_mark_date": mark_date,
                "last_error": None,
                "diagnostics": diagnostics,
                "data_contract": target.data_contract,
                "data_request_id": None,
            },
        )
        return {
            "account": account,
            "run": run_row,
            "diagnostics": diagnostics,
        }
    except MarketDataNotReadyError as e:
        request_id = e.request_id
        if str(account.get("universe") or "").startswith("watchlist:"):
            snapshot = account.get("watchlist_snapshot") or {}
            start_iso, end_iso, _ = resolve_date_range(
                CONFIG.date_range.start,
                asof or CONFIG.date_range.end,
            )
            request = enqueue_market_data_request(
                data_universe=watchlist_snapshot_data_universe(snapshot),
                universe_frame=watchlist_universe_frame(snapshot),
                start=start_iso,
                end=end_iso,
                initial_start=(
                    pd.Timestamp(start_iso) - pd.Timedelta(days=400)
                ).strftime("%Y-%m-%d"),
                consumer_kind="paper_account",
                consumer_id=account_id,
                force=True,
            )
            request_id = request.request_id
        update_account(
            account_id,
            {
                "last_run_at": datetime.now().isoformat(timespec="seconds"),
                "last_error": f"WAITING_FOR_DATA: {e}",
                "data_request_id": request_id,
                "diagnostics": {
                    "waiting_for_data": {
                        "data_universe": e.data_universe,
                        "request_id": request_id,
                        "coverage": (
                            e.coverage.to_dict()
                            if e.coverage is not None
                            else None
                        ),
                    }
                },
            },
        )
        raise
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


def run_account_once(account_id: str, *, asof: str | None = None) -> dict[str, Any]:
    """Run one serialized, retry-safe paper-trading cycle."""
    with account_run_lock(account_id):
        return _run_account_once_locked(account_id, asof=asof)


__all__ = ["run_account_once"]
