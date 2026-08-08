"""Read-side projections for the decision replay Web/API surfaces."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.decision_replay.models import DecisionReplaySnapshot
from src.decision_replay.store import artifact_state_token, load_snapshot


def _number(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _boolean(value: Any) -> bool:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return False
    return bool(value)


def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    return str(value)


@lru_cache(maxsize=1)
def _cached_snapshot(
    run_dir: str,
    state_token: tuple[tuple[str, int, int], ...],
) -> DecisionReplaySnapshot | None:
    _ = state_token
    return load_snapshot(run_dir)


def get_snapshot(run_dir: str | Path) -> DecisionReplaySnapshot | None:
    path = str(Path(run_dir))
    return _cached_snapshot(path, artifact_state_token(path))


def _normalized_summary(snapshot: DecisionReplaySnapshot) -> pd.DataFrame:
    summary = snapshot.daily_summary.copy()
    if summary.empty:
        return summary
    if not isinstance(summary.index, pd.DatetimeIndex):
        summary.index = pd.to_datetime(summary.index)
    return summary.sort_index()


def _latest_usable_date(summary: pd.DataFrame) -> pd.Timestamp | None:
    if summary.empty:
        return None
    if "eligible_count" in summary.columns:
        usable_dates = summary.index[
            pd.to_numeric(summary["eligible_count"], errors="coerce").fillna(0) > 0
        ]
        if len(usable_dates):
            return pd.Timestamp(usable_dates[-1])
    return pd.Timestamp(summary.index[-1])


def replay_meta(snapshot: DecisionReplaySnapshot) -> dict[str, Any]:
    summary = _normalized_summary(snapshot)
    dates = [dt.strftime("%Y-%m-%d") for dt in summary.index]
    rebalance_flags = (
        summary["is_rebalance"].fillna(False).astype(bool)
        if "is_rebalance" in summary.columns
        else pd.Series(False, index=summary.index)
    )
    decision_dates = [
        dt.strftime("%Y-%m-%d")
        for dt in summary.index[rebalance_flags]
    ]
    latest_timestamp = _latest_usable_date(summary)
    latest_date = (
        latest_timestamp.strftime("%Y-%m-%d")
        if latest_timestamp is not None
        else None
    )
    timeline = []
    for dt, row in summary.iterrows():
        timeline.append({
            "date": dt.strftime("%Y-%m-%d"),
            "is_rebalance": _boolean(row.get("is_rebalance")),
            "net_return": _number(row.get("net_return")),
            "benchmark_return": _number(row.get("benchmark_return")),
            "nav": _number(row.get("nav")),
            "eligible_count": _integer(row.get("eligible_count")),
            "held_count": _integer(row.get("held_count")),
            "total_cost_cash": _number(row.get("total_cost_cash")),
        })
    return {
        "available": True,
        "manifest": snapshot.manifest,
        "dates": dates,
        "decision_dates": decision_dates,
        "latest_date": latest_date,
        "timeline": timeline,
    }


def _matrix_row(
    matrices: dict[str, pd.DataFrame],
    name: str,
    date: pd.Timestamp,
    columns: pd.Index,
) -> pd.Series:
    values = matrices.get(name)
    if values is None or values.empty:
        return pd.Series(index=columns, dtype="float64")
    index = values.index
    if not isinstance(index, pd.DatetimeIndex):
        values = values.copy()
        values.index = pd.to_datetime(values.index)
    if date not in values.index:
        return pd.Series(index=columns, dtype="float64")
    return values.loc[date].reindex(columns)


def _matrix_column(
    matrices: dict[str, pd.DataFrame],
    name: str,
    ticker: str,
    index: pd.DatetimeIndex,
) -> pd.Series:
    values = matrices.get(name)
    if values is None or values.empty or ticker not in values.columns:
        return pd.Series(np.nan, index=index, dtype="float64")
    if not isinstance(values.index, pd.DatetimeIndex):
        values = values.copy()
        values.index = pd.to_datetime(values.index)
    return values[ticker].reindex(index)


def _summary_row(
    summary: pd.DataFrame,
    date: pd.Timestamp,
) -> dict[str, Any]:
    row = summary.loc[date]
    return {
        "date": date.strftime("%Y-%m-%d"),
        "is_rebalance": _boolean(row.get("is_rebalance")),
        "execution_date": _text(row.get("execution_date")),
        "universe_count": _integer(row.get("universe_count")),
        "eligible_count": _integer(row.get("eligible_count")),
        "signal_top_count": _integer(row.get("signal_top_count")),
        "held_count": _integer(row.get("held_count")),
        "gross_return": _number(row.get("gross_return")),
        "net_return": _number(row.get("net_return")),
        "benchmark_return": _number(row.get("benchmark_return")),
        "nav": _number(row.get("nav")),
        "turnover": _number(row.get("turnover")),
        "total_fee": _number(row.get("total_fee")),
        "total_slippage_cost": _number(row.get("total_slippage_cost")),
        "total_cost_cash": _number(row.get("total_cost_cash")),
        "cash": _number(row.get("cash")),
        "equity": _number(row.get("equity")),
    }


def _action(
    *,
    is_rebalance: bool,
    eligible: bool,
    current_weight: float | None,
    target_weight: float | None,
) -> str:
    if not is_rebalance:
        return "观察"
    old = float(current_weight or 0.0)
    new = float(target_weight or 0.0)
    tolerance = 1e-10
    if old > tolerance and new <= tolerance:
        return "卖出"
    if not eligible:
        return "排除"
    if old <= tolerance and new > tolerance:
        return "买入"
    if new > old + tolerance:
        return "调增"
    if new < old - tolerance:
        return "调减"
    return "持有"


def _event_rows(
    *,
    decision_date: str,
    trades: pd.DataFrame | None,
    orders: pd.DataFrame | None,
    fills: pd.DataFrame | None,
) -> dict[str, dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    if trades is not None and not trades.empty and "decision_date" in trades.columns:
        selected = trades[
            trades["decision_date"].astype(str) == decision_date
        ]
        for row in selected.to_dict(orient="records"):
            ticker = str(row.get("ticker") or "")
            events[ticker] = {
                "status": "filled",
                "side": _text(row.get("side")),
                "quantity": _number(row.get("estimated_quantity")),
                "execution_date": _text(row.get("date")),
                "raw_price": _number(row.get("raw_price")),
                "fill_price": _number(row.get("fill_price")),
                "slippage_bps": _number(row.get("slippage_bps")),
                "slippage_cost": _number(row.get("slippage_cost")),
                "fee": _number(row.get("fee")),
                "total_cost_cash": _number(row.get("total_cost_cash")),
            }

    if orders is not None and not orders.empty and "decision_date" in orders.columns:
        selected = orders[
            orders["decision_date"].astype(str) == decision_date
        ]
        for row in selected.to_dict(orient="records"):
            ticker = str(row.get("ticker") or "")
            events[ticker] = {
                "status": _text(row.get("status")),
                "side": _text(row.get("side")),
                "quantity": _number(row.get("quantity")),
                "execution_date": _text(row.get("fill_date")),
                "raw_price": _number(row.get("ref_price")),
                "fill_price": _number(row.get("fill_price")),
                "slippage_bps": _number(row.get("slippage_bps")),
                "slippage_cost": _number(row.get("slippage_cost")),
                "fee": _number(row.get("fee")),
                "total_cost_cash": _number(row.get("total_cost_cash")),
            }

    if fills is not None and not fills.empty and "decision_date" in fills.columns:
        selected = fills[
            fills["decision_date"].astype(str) == decision_date
        ].copy()
        for ticker, group in selected.groupby(
            selected["ticker"].astype(str),
            sort=False,
        ):
            event = events.setdefault(ticker, {})
            quantities = pd.to_numeric(
                group.get("quantity", pd.Series(dtype=float)),
                errors="coerce",
            ).fillna(0.0)
            total_quantity = float(quantities.sum())

            def weighted_average(column: str) -> float | None:
                if column not in group.columns:
                    return None
                values = pd.to_numeric(group[column], errors="coerce")
                valid = values.notna() & quantities.gt(0)
                if not valid.any():
                    return None
                return float(
                    (values[valid] * quantities[valid]).sum()
                    / float(quantities[valid].sum())
                )

            dates = (
                group["fill_date"].dropna().astype(str).sort_values()
                if "fill_date" in group.columns
                else pd.Series(dtype=str)
            )
            prior_status = _text(event.get("status"))
            event.update({
                "status": (
                    "filled"
                    if prior_status == "filled"
                    else "partial"
                ),
                "side": _text(group.iloc[-1].get("side")),
                "quantity": total_quantity,
                "execution_date": (
                    _text(dates.iloc[-1]) if not dates.empty else ""
                ),
                "raw_price": weighted_average("raw_open_price"),
                "fill_price": weighted_average("fill_price"),
                "slippage_bps": weighted_average("slippage_bps"),
                "slippage_cost": _number(
                    pd.to_numeric(
                        group.get(
                            "slippage_cost",
                            pd.Series(dtype=float),
                        ),
                        errors="coerce",
                    ).fillna(0.0).sum()
                ),
                "fee": _number(
                    pd.to_numeric(
                        group.get("fee", pd.Series(dtype=float)),
                        errors="coerce",
                    ).fillna(0.0).sum()
                ),
                "total_cost_cash": _number(
                    pd.to_numeric(
                        group.get(
                            "total_cost_cash",
                            pd.Series(dtype=float),
                        ),
                        errors="coerce",
                    ).fillna(0.0).sum()
                ),
            })
    return events


def date_snapshot(
    snapshot: DecisionReplaySnapshot,
    date: str | None,
    *,
    trades: pd.DataFrame | None = None,
    orders: pd.DataFrame | None = None,
    fills: pd.DataFrame | None = None,
) -> dict[str, Any]:
    summary = _normalized_summary(snapshot)
    if summary.empty:
        raise ValueError("决策快照没有可用日期")
    selected_date = (
        pd.Timestamp(date)
        if date
        else _latest_usable_date(summary)
    )
    assert selected_date is not None
    if selected_date not in summary.index:
        raise KeyError(f"决策快照中不存在日期 {selected_date.date()}")

    date_index = summary.index.get_loc(selected_date)
    previous_date = (
        summary.index[date_index - 1].strftime("%Y-%m-%d")
        if date_index > 0
        else None
    )
    next_date = (
        summary.index[date_index + 1].strftime("%Y-%m-%d")
        if date_index + 1 < len(summary.index)
        else None
    )
    composite_matrix = snapshot.signals.get("composite", pd.DataFrame())
    columns = pd.Index(composite_matrix.columns)
    summary_payload = _summary_row(summary, selected_date)
    is_rebalance = bool(summary_payload["is_rebalance"])

    market_rows = {
        name: _matrix_row(snapshot.market, name, selected_date, columns)
        for name in (
            "close",
            "daily_return",
            "volume",
            "effective_return",
        )
    }
    signal_rows = {
        name: _matrix_row(snapshot.signals, name, selected_date, columns)
        for name in (
            "composite",
            "rank",
            "percentile",
            "daily_signal_group",
            "decision_group",
            "held_group",
            "eligible",
            "tradable",
            "pit_membership",
            "exclusion_reason",
        )
    }
    portfolio_rows = {
        name: _matrix_row(snapshot.portfolio, name, selected_date, columns)
        for name in (
            "daily_weights",
            "return_weights",
            "daily_contributions",
            "decision_target_weights",
        )
    }

    factor_rows: dict[str, dict[str, pd.Series]] = {}
    for factor_id, matrices in snapshot.factors.items():
        factor_rows[factor_id] = {
            name: _matrix_row(matrices, name, selected_date, columns)
            for name in ("raw", "clean", "strategy_input", "contribution")
        }

    event_map = _event_rows(
        decision_date=selected_date.strftime("%Y-%m-%d"),
        trades=trades,
        orders=orders,
        fills=fills,
    )
    execution_date = summary_payload.get("execution_date")
    next_outcomes = pd.Series(index=columns, dtype="float64")
    if execution_date:
        next_outcomes = _matrix_row(
            snapshot.market,
            "effective_return",
            pd.Timestamp(execution_date),
            columns,
        )

    factor_ids = list(snapshot.manifest.get("factor_ids") or [])
    weights = snapshot.manifest.get("normalized_weights") or {}
    rows: list[dict[str, Any]] = []
    for ticker in columns:
        eligible = _boolean(signal_rows["eligible"].get(ticker))
        held_weight = _number(portfolio_rows["daily_weights"].get(ticker))
        target_weight = _number(
            portfolio_rows["decision_target_weights"].get(ticker)
        )
        factors = []
        for factor_id in factor_ids:
            values = factor_rows.get(factor_id, {})
            factors.append({
                "factor_id": factor_id,
                "weight": _number(weights.get(factor_id)),
                "raw": _number(values.get("raw", pd.Series()).get(ticker)),
                "clean": _number(values.get("clean", pd.Series()).get(ticker)),
                "strategy_input": _number(
                    values.get("strategy_input", pd.Series()).get(ticker)
                ),
                "contribution": _number(
                    values.get("contribution", pd.Series()).get(ticker)
                ),
            })
        event = event_map.get(str(ticker))
        rows.append({
            "ticker": str(ticker),
            "close": _number(market_rows["close"].get(ticker)),
            "daily_return": _number(market_rows["daily_return"].get(ticker)),
            "volume": _number(market_rows["volume"].get(ticker)),
            "in_universe": _boolean(
                signal_rows["pit_membership"].get(ticker)
            ),
            "tradable": _boolean(signal_rows["tradable"].get(ticker)),
            "eligible": eligible,
            "exclusion_reason": _text(
                signal_rows["exclusion_reason"].get(ticker)
            ),
            "score": _number(signal_rows["composite"].get(ticker)),
            "rank": _integer(signal_rows["rank"].get(ticker)),
            "percentile": _number(signal_rows["percentile"].get(ticker)),
            "signal_group": _integer(
                signal_rows["daily_signal_group"].get(ticker)
            ),
            "decision_group": _integer(
                signal_rows["decision_group"].get(ticker)
            ),
            "held_group": _integer(signal_rows["held_group"].get(ticker)),
            "held_weight": held_weight,
            "return_weight": _number(
                portfolio_rows["return_weights"].get(ticker)
            ),
            "target_weight": target_weight,
            "action": _action(
                is_rebalance=is_rebalance,
                eligible=eligible,
                current_weight=held_weight,
                target_weight=target_weight,
            ),
            "held_period_return": _number(
                market_rows["effective_return"].get(ticker)
            ),
            "next_holding_return": _number(next_outcomes.get(ticker)),
            "portfolio_contribution": _number(
                portfolio_rows["daily_contributions"].get(ticker)
            ),
            "execution": event,
            "factors": factors,
        })

    rows.sort(
        key=lambda row: (
            row["rank"] is None,
            row["rank"] if row["rank"] is not None else 10**9,
            row["ticker"],
        )
    )
    return {
        "date": selected_date.strftime("%Y-%m-%d"),
        "previous_date": previous_date,
        "next_date": next_date,
        "summary": summary_payload,
        "factor_ids": factor_ids,
        "normalized_weights": weights,
        "rows": rows,
        "row_count": len(rows),
    }


def stock_history(
    snapshot: DecisionReplaySnapshot,
    ticker: str,
) -> dict[str, Any]:
    ticker = str(ticker).strip().upper()
    composite = snapshot.signals.get("composite", pd.DataFrame())
    if ticker not in composite.columns:
        raise KeyError(f"决策快照中不存在股票 {ticker}")
    summary = _normalized_summary(snapshot)
    factor_ids = list(snapshot.manifest.get("factor_ids") or [])
    market_columns = {
        name: _matrix_column(
            snapshot.market,
            name,
            ticker,
            summary.index,
        )
        for name in ("close", "daily_return")
    }
    signal_columns = {
        name: _matrix_column(
            snapshot.signals,
            name,
            ticker,
            summary.index,
        )
        for name in (
            "composite",
            "rank",
            "percentile",
            "daily_signal_group",
        )
    }
    portfolio_columns = {
        name: _matrix_column(
            snapshot.portfolio,
            name,
            ticker,
            summary.index,
        )
        for name in (
            "daily_weights",
            "decision_target_weights",
            "daily_contributions",
        )
    }
    rows = []
    for date in summary.index:
        rows.append({
            "date": date.strftime("%Y-%m-%d"),
            "close": _number(market_columns["close"].get(date)),
            "daily_return": _number(
                market_columns["daily_return"].get(date)
            ),
            "score": _number(signal_columns["composite"].get(date)),
            "rank": _integer(signal_columns["rank"].get(date)),
            "percentile": _number(
                signal_columns["percentile"].get(date)
            ),
            "signal_group": _integer(
                signal_columns["daily_signal_group"].get(date)
            ),
            "held_weight": _number(
                portfolio_columns["daily_weights"].get(date)
            ),
            "target_weight": _number(
                portfolio_columns["decision_target_weights"].get(date)
            ),
            "portfolio_contribution": _number(
                portfolio_columns["daily_contributions"].get(date)
            ),
            "is_rebalance": _boolean(
                summary.loc[date].get("is_rebalance")
            ),
        })
    factor_history = {}
    for factor_id in factor_ids:
        matrices = snapshot.factors.get(factor_id, {})
        factor_history[factor_id] = {
            name: [
                _number(value)
                for value in _matrix_column(
                    matrices,
                    name,
                    ticker,
                    summary.index,
                )
            ]
            for name in ("raw", "clean", "strategy_input", "contribution")
        }
    return {
        "ticker": ticker,
        "factor_ids": factor_ids,
        "rows": rows,
        "factors": factor_history,
    }


__all__ = [
    "get_snapshot",
    "replay_meta",
    "date_snapshot",
    "stock_history",
]
