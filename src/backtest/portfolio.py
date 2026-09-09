"""Stateful portfolio accounting shared by research backtests.

The engine keeps one independent long-only account per research group.  It
marks positions to market between scheduled rebalances, so a winning security
naturally becomes a larger weight until an actual order changes it.  Orders,
fees, slippage, cash and NAV therefore come from the same state transition.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np
import pandas as pd

from src.execution import (
    calculate_execution,
    max_volume_fill_quantity,
)


_EPSILON = 1e-10


@dataclass
class StatefulPortfolioResult:
    """Auditable outputs from a group-level portfolio simulation."""

    gross_returns: pd.DataFrame
    net_returns: pd.DataFrame
    cost_returns: pd.DataFrame
    turnover: pd.DataFrame
    holdings_detail: pd.DataFrame
    trades_detail: pd.DataFrame
    costs_detail: pd.DataFrame
    daily_state: pd.DataFrame
    position_daily: pd.DataFrame
    capacity_breaches: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _GroupState:
    positions: pd.Series
    cash: float
    started: bool = False

    @property
    def nav(self) -> float:
        return float(self.cash + self.positions.sum())


def _safe_price(
    prices: pd.DataFrame | None,
    date: pd.Timestamp,
    ticker: str,
) -> float | None:
    if prices is None or prices.empty or ticker not in prices.columns:
        return None
    try:
        value = float(prices.loc[date, ticker])
    except (KeyError, TypeError, ValueError):
        return None
    return value if np.isfinite(value) and value > 0 else None


def _trailing_volume(
    volume: pd.DataFrame | None,
    decision_date: pd.Timestamp,
    ticker: str,
    window: int,
) -> float | None:
    if volume is None or volume.empty or ticker not in volume.columns:
        return None
    values = pd.to_numeric(
        volume.loc[volume.index <= decision_date, ticker],
        errors="coerce",
    )
    values = values[np.isfinite(values) & (values > 0)].tail(max(1, window))
    return float(values.mean()) if not values.empty else None


def _empty_positions() -> pd.Series:
    return pd.Series(dtype="float64", index=pd.Index([], name="ticker"))


def _order_row(
    *,
    date: pd.Timestamp,
    decision_date: pd.Timestamp,
    group: str,
    ticker: str,
    old_value: float,
    new_value: float,
    nav: float,
    execution: Mapping[str, Any],
    execution_prices: pd.DataFrame | None,
    volume: pd.DataFrame | None,
    raw_price_override: float | None = None,
    event_type: str = "REBALANCE",
    pricing_method: str = "NEXT_OPEN",
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    delta_value = float(new_value) - float(old_value)
    if abs(delta_value) <= _EPSILON:
        return None, None
    if not np.isfinite(nav) or nav <= 0:
        raise ValueError(
            f"Portfolio NAV must be positive before trading: group={group} nav={nav}"
        )
    side = "BUY" if delta_value > 0 else "SELL"
    raw_price = (
        float(raw_price_override)
        if raw_price_override is not None
        else _safe_price(execution_prices, date, ticker)
    )
    if raw_price is None or not np.isfinite(raw_price) or raw_price <= 0:
        raise ValueError(
            "Missing execution price for required backtest trade: "
            f"decision_date={decision_date.date()} execution_date={date.date()} "
            f"ticker={ticker} side={side} event_type={event_type}."
        )
    quantity = abs(delta_value) / raw_price
    adv_window = int(
        ((execution.get("slippage") or {}).get("adv_window", 20)) or 20
    )
    bar_volume = _trailing_volume(volume, decision_date, ticker, adv_window)
    maximum = max_volume_fill_quantity(
        requested_quantity=quantity,
        volume=bar_volume,
        execution=dict(execution),
    )
    breach = None
    if quantity > maximum + 1e-9:
        trade_weight = abs(delta_value) / nav
        maximum_nav = (
            maximum * raw_price / trade_weight
            if maximum > 0 and trade_weight > 0
            else 0.0
        )
        breach = {
            "decision_date": decision_date.strftime("%Y-%m-%d"),
            "execution_date": date.strftime("%Y-%m-%d"),
            "group": group,
            "ticker": ticker,
            "side": side,
            "event_type": event_type,
            "portfolio_value": float(nav),
            "trade_abs_weight": float(trade_weight),
            "raw_price": float(raw_price),
            "requested_quantity": float(quantity),
            "allowed_quantity": float(maximum),
            "adv": float(bar_volume) if bar_volume is not None else None,
            "participation_rate": (
                float(quantity / bar_volume)
                if bar_volume is not None and bar_volume > 0
                else None
            ),
            "volume_limit": float(
                ((execution.get("slippage") or {}).get("volume_limit", 0.025))
                or 0.0
            ),
            "max_portfolio_value": float(maximum_nav),
        }

    result = calculate_execution(
        side=side,
        quantity=quantity,
        raw_price=raw_price,
        volume=bar_volume,
        execution=dict(execution),
    )
    components = result.get("fee_components") or {}
    total_cost = float(result["total_cost"])
    old_weight = float(old_value / nav)
    new_weight = float(new_value / nav)
    return {
        "date": date.strftime("%Y-%m-%d"),
        "decision_date": decision_date.strftime("%Y-%m-%d"),
        "group": group,
        "ticker": ticker,
        "old_weight": old_weight,
        "new_weight": new_weight,
        "trade_weight": new_weight - old_weight,
        "trade_abs_weight": abs(delta_value) / nav,
        "side": side,
        "event_type": event_type,
        "pricing_method": pricing_method,
        "portfolio_value": float(nav),
        "estimated_notional": abs(delta_value),
        "estimated_quantity": float(quantity),
        "raw_price": float(raw_price),
        "fill_price": float(result["fill_price"]),
        "bar_volume": float(bar_volume) if bar_volume is not None else np.nan,
        "volume_reference": f"ADV{adv_window}_asof_decision",
        "participation_rate": float(result["participation_rate"]),
        "slippage_model": execution.get("slippage_model"),
        "slippage_bps": float(result["slippage_bps"]),
        "impact_bps": float(result["impact_bps"]),
        "slippage_cost": float(result["slippage_cost"]),
        "fee_model": execution.get("fee_model"),
        "broker_commission": float(components.get("broker_commission", 0.0) or 0.0),
        "sec_fee": float(components.get("sec_fee", 0.0) or 0.0),
        "finra_taf": float(components.get("finra_taf", 0.0) or 0.0),
        "finra_cat": float(components.get("finra_cat", 0.0) or 0.0),
        "clearing_fee": float(components.get("clearing_fee", 0.0) or 0.0),
        "pass_through_fee": float(components.get("pass_through_fee", 0.0) or 0.0),
        "exchange_fee": float(components.get("exchange_fee", 0.0) or 0.0),
        "fee": float(result["fee"]),
        "total_cost_cash": total_cost,
        "cost": total_cost / nav,
    }, breach


def _cost_row(
    rows: list[dict[str, Any]],
    *,
    date: pd.Timestamp,
    decision_date: pd.Timestamp,
    group: str,
    nav: float,
    turnover: float,
    event_type: str,
    execution: Mapping[str, Any],
) -> dict[str, Any]:
    total_fee = float(sum(float(row["fee"]) for row in rows))
    total_slippage = float(sum(float(row["slippage_cost"]) for row in rows))
    total = total_fee + total_slippage
    return {
        "date": date.strftime("%Y-%m-%d"),
        "decision_date": decision_date.strftime("%Y-%m-%d"),
        "group": group,
        "event_type": event_type,
        "traded_weight": float(sum(float(row["trade_abs_weight"]) for row in rows)),
        "turnover": float(turnover),
        "portfolio_value": float(nav),
        "fee_model": execution.get("fee_model"),
        "slippage_model": execution.get("slippage_model"),
        "avg_slippage_bps": (
            float(np.mean([float(row["slippage_bps"]) for row in rows]))
            if rows
            else 0.0
        ),
        "total_slippage_cost": total_slippage,
        "total_fee": total_fee,
        "total_cost_cash": total,
        "cost": total / nav if nav > 0 else 0.0,
    }


def _target_orders(
    *,
    state: _GroupState,
    target_tickers: list[str],
    date: pd.Timestamp,
    decision_date: pd.Timestamp,
    group: str,
    execution: Mapping[str, Any],
    execution_prices: pd.DataFrame,
    volume: pd.DataFrame | None,
) -> tuple[pd.Series, float, list[dict[str, Any]], list[dict[str, Any]]]:
    """Solve a fully invested target while reserving the resulting costs."""
    nav = state.nav
    if nav <= 0:
        raise ValueError(f"Portfolio is insolvent before rebalance: group={group}")
    target_index = pd.Index(target_tickers, name="ticker")
    target_weights = (
        pd.Series(1.0 / len(target_index), index=target_index, dtype="float64")
        if len(target_index)
        else _empty_positions()
    )
    estimated_cost = 0.0
    final_rows: list[dict[str, Any]] = []
    final_breaches: list[dict[str, Any]] = []
    target_values = _empty_positions()
    for _ in range(12):
        exposure = max(0.0, nav - estimated_cost)
        target_values = target_weights * exposure
        all_tickers = state.positions.index.union(target_values.index)
        old = state.positions.reindex(all_tickers, fill_value=0.0)
        new = target_values.reindex(all_tickers, fill_value=0.0)
        rows: list[dict[str, Any]] = []
        breaches: list[dict[str, Any]] = []
        for ticker in all_tickers:
            row, breach = _order_row(
                date=date,
                decision_date=decision_date,
                group=group,
                ticker=str(ticker),
                old_value=float(old.loc[ticker]),
                new_value=float(new.loc[ticker]),
                nav=nav,
                execution=execution,
                execution_prices=execution_prices,
                volume=volume,
            )
            if row is not None:
                rows.append(row)
            if breach is not None:
                breaches.append(breach)
        new_cost = float(sum(float(row["total_cost_cash"]) for row in rows))
        final_rows = rows
        final_breaches = breaches
        if abs(new_cost - estimated_cost) <= max(1e-8, nav * 1e-12):
            estimated_cost = new_cost
            break
        estimated_cost = new_cost
    else:
        raise ValueError(
            f"Transaction-cost reserve did not converge: date={date.date()} group={group}"
        )

    # Recompute once with the converged reserve so rows and positions are exact.
    exposure = max(0.0, nav - estimated_cost)
    target_values = target_weights * exposure
    all_tickers = state.positions.index.union(target_values.index)
    old = state.positions.reindex(all_tickers, fill_value=0.0)
    new = target_values.reindex(all_tickers, fill_value=0.0)
    final_rows = []
    final_breaches = []
    for ticker in all_tickers:
        row, breach = _order_row(
            date=date,
            decision_date=decision_date,
            group=group,
            ticker=str(ticker),
            old_value=float(old.loc[ticker]),
            new_value=float(new.loc[ticker]),
            nav=nav,
            execution=execution,
            execution_prices=execution_prices,
            volume=volume,
        )
        if row is not None:
            final_rows.append(row)
        if breach is not None:
            final_breaches.append(breach)
    exact_cost = float(sum(float(row["total_cost_cash"]) for row in final_rows))
    if abs(exact_cost - estimated_cost) > max(1e-6, nav * 1e-10):
        raise ValueError(
            f"Transaction-cost reserve is inconsistent: date={date.date()} group={group}"
        )
    return target_values, exact_cost, final_rows, final_breaches


def simulate_group_portfolios(
    assignments: pd.DataFrame,
    held_returns: pd.DataFrame,
    rebalance_dates: pd.DatetimeIndex,
    *,
    group_names: Mapping[int, str],
    execution: Mapping[str, Any],
    execution_prices: pd.DataFrame,
    volume: pd.DataFrame | None = None,
    forced_exit_events: pd.DataFrame | None = None,
) -> StatefulPortfolioResult:
    """Run stateful independent accounts for assignment-coded research groups."""
    dates = pd.DatetimeIndex(held_returns.index)
    columns = pd.Index(held_returns.columns)
    assignments = assignments.reindex(index=dates, columns=columns)
    initial_nav = float(execution.get("portfolio_value", 100000.0) or 100000.0)
    states = {
        int(group_no): _GroupState(_empty_positions(), initial_nav)
        for group_no in group_names
    }
    gross = pd.DataFrame(np.nan, index=dates, columns=list(group_names.values()))
    net = gross.copy()
    cost_returns = gross.copy()
    holdings_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    cost_rows: list[dict[str, Any]] = []
    state_rows: list[dict[str, Any]] = []
    position_rows: list[dict[str, Any]] = []
    turnover_rows: dict[pd.Timestamp, dict[str, float]] = {}
    breaches: list[dict[str, Any]] = []

    effective_rebalances: dict[pd.Timestamp, pd.Timestamp] = {}
    for decision_date in pd.DatetimeIndex(rebalance_dates):
        position = int(dates.searchsorted(decision_date, side="right"))
        if position + 1 < len(dates):
            effective_rebalances[pd.Timestamp(dates[position])] = pd.Timestamp(decision_date)

    start_events: dict[pd.Timestamp, list[dict[str, Any]]] = {}
    end_events: dict[pd.Timestamp, list[dict[str, Any]]] = {}
    if forced_exit_events is not None and not forced_exit_events.empty:
        for event in forced_exit_events.to_dict("records"):
            method = str(event.get("pricing_method") or "")
            event_date = pd.Timestamp(event["execution_date"])
            target = start_events if method == "NEXT_OPEN" else end_events
            target.setdefault(event_date, []).append(event)

    def record_exit(
        event: dict[str, Any],
        *,
        date: pd.Timestamp,
        writeoff_value: float | None = None,
        writeoff_nav: float | None = None,
    ) -> tuple[int | None, float]:
        group_no = int(float(event["assignment"]))
        if group_no not in states:
            raise ValueError(f"Unknown forced-exit assignment: {group_no}")
        state = states[group_no]
        ticker = str(event["ticker"])
        old_value = float(state.positions.get(ticker, 0.0))
        method = str(event["pricing_method"])
        event_type = (
            "MEMBERSHIP_EXIT_WRITE_OFF"
            if method == "TOTAL_LOSS_WRITE_OFF"
            else "MEMBERSHIP_EXIT"
        )
        decision_date = pd.Timestamp(event["decision_date"])
        if method == "TOTAL_LOSS_WRITE_OFF":
            state.positions = state.positions.drop(ticker, errors="ignore")
            loss_value = float(writeoff_value or old_value)
            nav = float(writeoff_nav or (state.nav + loss_value))
            if loss_value <= _EPSILON or nav <= 0:
                return None, 0.0
            trade_weight = loss_value / nav
            row = {
                "date": date.strftime("%Y-%m-%d"),
                "decision_date": decision_date.strftime("%Y-%m-%d"),
                "group": group_names[group_no],
                "ticker": ticker,
                "old_weight": trade_weight,
                "new_weight": 0.0,
                "trade_weight": -trade_weight,
                "trade_abs_weight": trade_weight,
                "side": "WRITE_OFF",
                "event_type": event_type,
                "pricing_method": method,
                "portfolio_value": nav,
                "estimated_notional": loss_value,
                "estimated_quantity": np.nan,
                "raw_price": 0.0,
                "fill_price": 0.0,
                "bar_volume": np.nan,
                "volume_reference": "NOT_APPLICABLE_WRITE_OFF",
                "participation_rate": 0.0,
                "slippage_model": execution.get("slippage_model"),
                "slippage_bps": 0.0,
                "impact_bps": 0.0,
                "slippage_cost": 0.0,
                "fee_model": execution.get("fee_model"),
                "broker_commission": 0.0,
                "sec_fee": 0.0,
                "finra_taf": 0.0,
                "finra_cat": 0.0,
                "clearing_fee": 0.0,
                "pass_through_fee": 0.0,
                "exchange_fee": 0.0,
                "fee": 0.0,
                "total_cost_cash": 0.0,
                "cost": 0.0,
            }
            trade_rows.append(row)
            cost_rows.append(
                _cost_row(
                    [row],
                    date=date,
                    decision_date=decision_date,
                    group=group_names[group_no],
                    nav=nav,
                    turnover=trade_weight,
                    event_type=event_type,
                    execution=execution,
                )
            )
            return group_no, 0.0
        if old_value <= _EPSILON:
            return None, 0.0
        nav = state.nav
        row, breach = _order_row(
            date=date,
            decision_date=decision_date,
            group=group_names[group_no],
            ticker=ticker,
            old_value=old_value,
            new_value=0.0,
            nav=nav,
            execution=execution,
            execution_prices=execution_prices,
            volume=volume,
            raw_price_override=float(event["raw_price"]),
            event_type=event_type,
            pricing_method=method,
        )
        if row is None:
            return None, 0.0
        if breach is not None:
            breaches.append(breach)
        cost = float(row["total_cost_cash"])
        state.positions = state.positions.drop(ticker)
        state.cash += old_value - cost
        trade_rows.append(row)
        turnover = old_value / nav
        cost_rows.append(
            _cost_row(
                [row],
                date=date,
                decision_date=decision_date,
                group=group_names[group_no],
                nav=nav,
                turnover=turnover,
                event_type=event_type,
                execution=execution,
            )
        )
        turnover_rows.setdefault(date, {})[group_names[group_no]] = (
            turnover_rows.setdefault(date, {}).get(group_names[group_no], 0.0)
            + turnover
        )
        return group_no, cost

    for date in dates:
        row_costs = {group_no: 0.0 for group_no in group_names}
        start_navs = {group_no: state.nav for group_no, state in states.items()}

        for event in start_events.get(pd.Timestamp(date), []):
            group_no, cost = record_exit(event, date=pd.Timestamp(date))
            if group_no is not None:
                row_costs[group_no] += cost

        decision_date = effective_rebalances.get(pd.Timestamp(date))
        if decision_date is not None:
            assignment_row = assignments.loc[decision_date]
            for group_no, group_name in group_names.items():
                state = states[group_no]
                target_tickers = [
                    str(ticker)
                    for ticker in columns[assignment_row.to_numpy() == group_no]
                ]
                nav = state.nav
                old_total = float(state.positions.sum())
                target_values, cost, rows, order_breaches = _target_orders(
                    state=state,
                    target_tickers=target_tickers,
                    date=pd.Timestamp(date),
                    decision_date=decision_date,
                    group=group_name,
                    execution=execution,
                    execution_prices=execution_prices,
                    volume=volume,
                )
                traded = float(sum(abs(float(row["estimated_notional"])) for row in rows))
                if old_total <= _EPSILON or target_values.empty:
                    turnover = traded / nav if nav > 0 else 0.0
                else:
                    turnover = traded / (2.0 * nav) if nav > 0 else 0.0
                state.positions = target_values[target_values > _EPSILON].copy()
                state.cash = nav - float(state.positions.sum()) - cost
                state.started = state.started or bool(target_tickers)
                if abs(state.nav - (nav - cost)) > max(1e-6, nav * 1e-10):
                    raise ValueError(
                        f"Accounting identity failed after rebalance: date={date.date()} "
                        f"group={group_name}"
                    )
                row_costs[group_no] += cost
                trade_rows.extend(rows)
                breaches.extend(order_breaches)
                turnover_rows.setdefault(decision_date, {})[group_name] = turnover
                if rows:
                    cost_rows.append(
                        _cost_row(
                            rows,
                            date=pd.Timestamp(date),
                            decision_date=decision_date,
                            group=group_name,
                            nav=nav,
                            turnover=turnover,
                            event_type="REBALANCE",
                            execution=execution,
                        )
                    )
                current_nav = state.nav
                for ticker, value in state.positions.items():
                    raw_price = _safe_price(execution_prices, pd.Timestamp(date), str(ticker))
                    holdings_rows.append({
                        "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
                        "decision_date": decision_date.strftime("%Y-%m-%d"),
                        "group": group_name,
                        "ticker": str(ticker),
                        "target_weight": 1.0 / len(target_tickers),
                        "actual_weight": float(value / current_nav),
                        "market_value": float(value),
                        "quantity": float(value / raw_price) if raw_price else np.nan,
                        "cash_balance": float(state.cash),
                        "nav": float(current_nav),
                    })

        for group_no, group_name in group_names.items():
            state = states[group_no]
            start_nav = start_navs[group_no]
            active = state.positions[state.positions > _EPSILON]
            if not state.started:
                continue
            row_returns = held_returns.loc[date].reindex(active.index)
            if active.empty:
                market_pnl = 0.0
            elif row_returns.isna().all() and date == dates[-1]:
                continue
            elif row_returns.isna().any():
                missing = list(row_returns.index[row_returns.isna()])[:10]
                raise ValueError(
                    "Missing return for held securities; refusing ex-post "
                    f"renormalization: date={date.date()} group={group_name} "
                    f"tickers={missing}"
                )
            else:
                market_pnl = float((active * row_returns).sum())
                for ticker, start_value in active.items():
                    security_return = float(row_returns.loc[ticker])
                    position_rows.append({
                        "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
                        "group": group_name,
                        "ticker": str(ticker),
                        "start_value": float(start_value),
                        "start_weight": float(start_value / start_nav),
                        "security_return": security_return,
                        "contribution_return": float(
                            start_value * security_return / start_nav
                        ),
                        "end_value_before_exit": float(
                            start_value * (1.0 + security_return)
                        ),
                    })
                state.positions.loc[active.index] = active * (1.0 + row_returns)

            for event in end_events.get(pd.Timestamp(date), []):
                event_group = int(float(event["assignment"]))
                if event_group != group_no:
                    continue
                recorded_group, cost = record_exit(
                    event,
                    date=pd.Timestamp(date),
                    writeoff_value=float(active.get(str(event["ticker"]), 0.0)),
                    writeoff_nav=start_nav,
                )
                if recorded_group is not None:
                    row_costs[group_no] += cost

            end_nav = state.nav
            if start_nav <= 0:
                raise ValueError(
                    f"Portfolio NAV is non-positive: date={date.date()} group={group_name}"
                )
            net_value = end_nav / start_nav - 1.0
            cost_value = row_costs[group_no] / start_nav
            gross_value = net_value + cost_value
            if abs(net_value - (gross_value - cost_value)) > 1e-12:
                raise ValueError("Gross/net/cost return identity failed")
            gross.loc[date, group_name] = gross_value
            net.loc[date, group_name] = net_value
            cost_returns.loc[date, group_name] = cost_value
            state_rows.append({
                "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
                "group": group_name,
                "start_nav": float(start_nav),
                "market_pnl": float(market_pnl),
                "cost_cash": float(row_costs[group_no]),
                "end_nav": float(end_nav),
                "cash_balance": float(state.cash),
                "gross_return": float(gross_value),
                "net_return": float(net_value),
                "cost_return": float(cost_value),
                "accounting_error": float(
                    end_nav - start_nav * (1.0 + net_value)
                ),
            })

    turnover = pd.DataFrame.from_dict(turnover_rows, orient="index").sort_index()
    turnover.index.name = "date"
    return StatefulPortfolioResult(
        gross_returns=gross.dropna(how="all"),
        net_returns=net.dropna(how="all"),
        cost_returns=cost_returns.dropna(how="all"),
        turnover=turnover,
        holdings_detail=pd.DataFrame(holdings_rows),
        trades_detail=pd.DataFrame(trade_rows),
        costs_detail=pd.DataFrame(cost_rows),
        daily_state=pd.DataFrame(state_rows),
        position_daily=pd.DataFrame(position_rows),
        capacity_breaches=breaches,
    )


__all__ = ["StatefulPortfolioResult", "simulate_group_portfolios"]
