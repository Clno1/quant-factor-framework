"""Build an auditable daily strategy-decision snapshot from a backtest run."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping

import numpy as np
import pandas as pd

from src.backtest.quintile import QuintileResult
from src.decision_replay.models import DecisionReplaySnapshot


SCHEMA_VERSION = 1


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _align(
    values: pd.DataFrame | None,
    index: pd.DatetimeIndex,
    columns: pd.Index,
) -> pd.DataFrame:
    if values is None or values.empty:
        return pd.DataFrame(index=index, columns=columns, dtype="float64")
    out = values.copy()
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index)
    return out.reindex(index=index, columns=columns)


def _rank_and_percentile(
    scores: pd.DataFrame,
    eligible: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    valid = scores.where(eligible)
    ranks = valid.rank(axis=1, method="first", ascending=False)
    counts = valid.notna().sum(axis=1).astype("float64")
    denominator = (counts - 1.0).replace(0.0, np.nan)
    percentile = 1.0 - ranks.sub(1.0).div(denominator, axis=0)
    single = counts == 1.0
    if single.any():
        percentile.loc[single] = valid.loc[single].notna().astype(float)
    return ranks, percentile


def _daily_signal_groups(
    scores: pd.DataFrame,
    eligible: pd.DataFrame,
    n_groups: int,
) -> pd.DataFrame:
    groups = pd.DataFrame(np.nan, index=scores.index, columns=scores.columns)
    valid = scores.where(eligible)
    for dt, row in valid.iterrows():
        row = row.dropna()
        if len(row) < n_groups:
            continue
        try:
            labels = pd.qcut(
                row.rank(method="first"),
                q=n_groups,
                labels=list(range(1, n_groups + 1)),
            )
        except ValueError:
            continue
        groups.loc[dt, labels.index] = labels.astype(int).values
    return groups


def _equal_weights(
    group_matrix: pd.DataFrame,
    top_group: int,
) -> pd.DataFrame:
    selected = group_matrix.eq(top_group)
    counts = selected.sum(axis=1).replace(0, np.nan)
    return selected.astype("float64").div(counts, axis=0).fillna(0.0)


def _decision_target_weights(
    decision_group: pd.DataFrame,
    rebal_dates: pd.DatetimeIndex,
    top_group: int,
) -> pd.DataFrame:
    out = pd.DataFrame(
        np.nan,
        index=decision_group.index,
        columns=decision_group.columns,
    )
    available = decision_group.index.intersection(rebal_dates)
    if available.empty:
        return out
    selected = decision_group.loc[available].eq(top_group)
    counts = selected.sum(axis=1).replace(0, np.nan)
    out.loc[available] = (
        selected.astype("float64").div(counts, axis=0).fillna(0.0)
    )
    return out


def _exclusion_reasons(
    composite: pd.DataFrame,
    membership: pd.DataFrame,
    tradable: pd.DataFrame,
) -> pd.DataFrame:
    reasons = pd.DataFrame("", index=composite.index, columns=composite.columns)
    reasons = reasons.mask(~membership, "不在当日股票池")
    reasons = reasons.mask(membership & composite.isna(), "因子数据缺失")
    reasons = reasons.mask(
        membership & composite.notna() & ~tradable,
        "不满足可交易规则",
    )
    return reasons


def _execution_date_map(
    index: pd.DatetimeIndex,
    rebal_dates: pd.DatetimeIndex,
) -> pd.Series:
    out = pd.Series("", index=index, dtype="object")
    for decision_date in index.intersection(rebal_dates):
        pos = index.searchsorted(decision_date, side="right")
        if pos + 1 < len(index):
            out.loc[decision_date] = pd.Timestamp(index[pos]).strftime("%Y-%m-%d")
    return out


def _cost_summary(
    costs: pd.DataFrame,
    index: pd.DatetimeIndex,
    top_label: str,
) -> tuple[pd.DataFrame, pd.Series]:
    columns = [
        "turnover",
        "total_fee",
        "total_slippage_cost",
        "total_cost_cash",
        "cost",
    ]
    by_execution = pd.DataFrame(0.0, index=index, columns=columns)
    execution_by_decision = pd.Series("", index=index, dtype="object")
    if costs is None or costs.empty:
        return by_execution, execution_by_decision

    source = costs.copy()
    if "group" in source.columns:
        source = source[source["group"].astype(str) == top_label]
    for row in source.to_dict(orient="records"):
        execution_date = pd.Timestamp(row.get("date"))
        decision_date = pd.Timestamp(row.get("decision_date"))
        if execution_date in by_execution.index:
            for column in columns:
                value = row.get(column)
                if value is not None and pd.notna(value):
                    by_execution.loc[execution_date, column] += float(value)
        if decision_date in execution_by_decision.index:
            execution_by_decision.loc[decision_date] = execution_date.strftime(
                "%Y-%m-%d"
            )
    return by_execution, execution_by_decision


def _validate_contributions(
    composite: pd.DataFrame,
    contributions: Mapping[str, pd.DataFrame],
) -> float:
    if not contributions:
        raise ValueError("Decision replay requires at least one factor contribution")
    rebuilt: pd.DataFrame | None = None
    for values in contributions.values():
        rebuilt = (
            values.copy()
            if rebuilt is None
            else rebuilt + values
        )
    assert rebuilt is not None
    rebuilt = rebuilt.reindex(index=composite.index, columns=composite.columns)
    expected = composite.notna()
    missing = expected & rebuilt.isna()
    unexpected = ~expected & rebuilt.notna()
    if missing.any().any() or unexpected.any().any():
        missing_count = int(missing.sum().sum())
        unexpected_count = int(unexpected.sum().sum())
        raise ValueError(
            "Factor contribution audit failed: availability mismatch "
            f"(missing={missing_count}, unexpected={unexpected_count})"
        )
    diff = (rebuilt - composite).where(composite.notna()).abs()
    max_diff = float(diff.max().max()) if diff.notna().any().any() else 0.0
    if max_diff > 1e-8:
        raise ValueError(
            "Factor contribution audit failed: "
            f"max(abs(sum(contribution)-composite))={max_diff:.3e}"
        )
    return max_diff


def build_backtest_snapshot(
    *,
    source_id: str,
    strategy_snapshot: dict[str, Any],
    universe: str,
    composite: pd.DataFrame,
    factor_raw: Mapping[str, pd.DataFrame],
    factor_clean: Mapping[str, pd.DataFrame],
    factor_inputs: Mapping[str, pd.DataFrame],
    factor_contributions: Mapping[str, pd.DataFrame],
    close_prices: pd.DataFrame,
    market_returns: pd.DataFrame,
    volumes: pd.DataFrame | None,
    membership_mask: pd.DataFrame | None,
    result: QuintileResult,
    n_groups: int,
    top_group: int,
    normalized_weights: Mapping[str, float],
    execution: Mapping[str, Any],
    pit_diagnostics: Mapping[str, Any] | None = None,
) -> DecisionReplaySnapshot:
    """Freeze the exact matrices used by a completed backtest."""
    if composite is None or composite.empty:
        raise ValueError("composite is empty")

    index = pd.DatetimeIndex(composite.index)
    columns = pd.Index(composite.columns)
    composite = _align(composite, index, columns)
    membership = (
        _align(membership_mask, index, columns).fillna(False).astype(bool)
        if membership_mask is not None and not membership_mask.empty
        else pd.DataFrame(True, index=index, columns=columns)
    )
    tradable = _align(result.tradable_mask, index, columns).fillna(False).astype(bool)
    eligible = membership & tradable & composite.notna()

    rank, percentile = _rank_and_percentile(composite, eligible)
    daily_group = _daily_signal_groups(composite, eligible, n_groups)
    decision_group = _align(result.group_assignment, index, columns)
    held_group = _align(result.held_assignment, index, columns)
    effective_returns = _align(result.effective_returns, index, columns)
    top_label = f"Q{top_group}"
    position_daily = result.position_daily
    if position_daily is not None and not position_daily.empty:
        top_positions = position_daily.loc[
            position_daily["group"].astype(str).eq(top_label)
        ].copy()
        top_positions["date"] = pd.to_datetime(top_positions["date"])
        held_weights = top_positions.pivot_table(
            index="date",
            columns="ticker",
            values="start_weight",
            aggfunc="last",
        ).reindex(index=index, columns=columns, fill_value=0.0).fillna(0.0)
        return_weights = held_weights.copy()
        daily_contributions = top_positions.pivot_table(
            index="date",
            columns="ticker",
            values="contribution_return",
            aggfunc="sum",
        ).reindex(index=index, columns=columns)
    else:
        # Compatibility for snapshots created before the stateful portfolio
        # ledger existed. New formal backtests always take the branch above.
        held_weights = _equal_weights(held_group, top_group)
        return_weights = held_weights.copy()
        daily_contributions = (
            effective_returns.where(return_weights > 0) * return_weights
        )
    cost_summary, cost_execution_dates = _cost_summary(
        result.costs_detail,
        index,
        top_label,
    )
    execution_dates = _execution_date_map(index, result.rebalance_dates)
    execution_dates.update(cost_execution_dates[cost_execution_dates != ""])
    executable_rebalance_dates = pd.DatetimeIndex(
        execution_dates.index[execution_dates != ""]
    )
    target_weights = _decision_target_weights(
        decision_group,
        executable_rebalance_dates,
        top_group,
    )

    summary = pd.DataFrame(index=index)
    summary.index.name = "date"
    summary["is_rebalance"] = summary.index.isin(executable_rebalance_dates)
    summary["execution_date"] = execution_dates
    summary["universe_count"] = membership.sum(axis=1).astype(int)
    summary["eligible_count"] = eligible.sum(axis=1).astype(int)
    summary["signal_top_count"] = daily_group.eq(top_group).sum(axis=1).astype(int)
    summary["held_count"] = (held_weights > 0).sum(axis=1).astype(int)
    summary["gross_return"] = (
        result.gross_group_returns.get(top_label, pd.Series(dtype=float))
        .reindex(index)
    )
    summary["net_return"] = (
        result.group_daily_returns.get(top_label, pd.Series(dtype=float))
        .reindex(index)
    )
    summary["benchmark_return"] = result.benchmark_returns.reindex(index)
    summary["nav"] = (1.0 + summary["net_return"].fillna(0.0)).cumprod()
    for column in cost_summary.columns:
        summary[column] = cost_summary[column]

    aligned_factors: dict[str, dict[str, pd.DataFrame]] = {}
    factor_ids = list(normalized_weights.keys())
    for factor_id in factor_ids:
        contribution = _align(
            factor_contributions.get(factor_id),
            index,
            columns,
        ).where(composite.notna())
        aligned_factors[factor_id] = {
            "clean": _align(factor_clean.get(factor_id), index, columns),
            "strategy_input": _align(
                factor_inputs.get(factor_id),
                index,
                columns,
            ),
            "contribution": contribution,
        }
        raw = factor_raw.get(factor_id)
        if raw is not None and not raw.empty:
            aligned_factors[factor_id]["raw"] = _align(raw, index, columns)

    max_contribution_error = _validate_contributions(
        composite,
        {
            factor_id: values["contribution"]
            for factor_id, values in aligned_factors.items()
        },
    )

    gross_from_rows = daily_contributions.sum(axis=1, min_count=1)
    expected_gross = summary["gross_return"]
    missing_gross = expected_gross.notna() & gross_from_rows.isna()
    unexpected_gross = expected_gross.isna() & gross_from_rows.notna()
    if missing_gross.any() or unexpected_gross.any():
        raise ValueError(
            "Portfolio contribution audit failed: availability mismatch "
            f"(missing={int(missing_gross.sum())}, "
            f"unexpected={int(unexpected_gross.sum())})"
        )
    comparable = gross_from_rows.notna() & expected_gross.notna()
    max_gross_error = (
        float((gross_from_rows[comparable] - expected_gross[comparable]).abs().max())
        if comparable.any()
        else 0.0
    )
    if max_gross_error > 1e-8:
        raise ValueError(
            "Portfolio contribution audit failed: "
            f"max error={max_gross_error:.3e}"
        )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source_kind": "backtest",
        "source_id": source_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "strategy_snapshot": strategy_snapshot,
        "strategy_hash": _canonical_hash(strategy_snapshot),
        "universe": universe,
        "date_start": index.min().strftime("%Y-%m-%d"),
        "date_end": index.max().strftime("%Y-%m-%d"),
        "trading_days": int(len(index)),
        "tickers": int(len(columns)),
        "factor_ids": factor_ids,
        "normalized_weights": {
            str(k): float(v) for k, v in normalized_weights.items()
        },
        "raw_factor_available": {
            factor_id: "raw" in aligned_factors[factor_id]
            for factor_id in factor_ids
        },
        "n_groups": int(n_groups),
        "top_group": int(top_group),
        "execution": dict(execution),
        "point_in_time_universe": dict(pit_diagnostics or {}),
        "portfolio_contribution_available": True,
        "audit": {
            "max_factor_contribution_error": max_contribution_error,
            "max_portfolio_contribution_error": max_gross_error,
        },
    }

    return DecisionReplaySnapshot(
        manifest=manifest,
        daily_summary=summary,
        market={
            "close": _align(close_prices, index, columns),
            "daily_return": _align(market_returns, index, columns),
            "volume": _align(volumes, index, columns),
            "effective_return": effective_returns,
        },
        signals={
            "composite": composite,
            "rank": rank,
            "percentile": percentile,
            "daily_signal_group": daily_group,
            "decision_group": decision_group,
            "held_group": held_group,
            "eligible": eligible,
            "tradable": tradable,
            "pit_membership": membership,
            "exclusion_reason": _exclusion_reasons(
                composite,
                membership,
                tradable,
            ),
        },
        factors=aligned_factors,
        portfolio={
            "daily_weights": held_weights,
            "return_weights": return_weights,
            "daily_contributions": daily_contributions,
            "decision_target_weights": target_weights,
        },
    )


def build_paper_snapshot(
    *,
    source_id: str,
    account: Mapping[str, Any],
    target: Any,
    positions: pd.DataFrame,
    cash: float,
    equity: float,
    is_rebalance: bool,
) -> DecisionReplaySnapshot:
    """Build the one-day decision row appended by a paper-account run."""
    decision_date = pd.Timestamp(target.decision_date)
    index = pd.DatetimeIndex([decision_date], name="date")
    columns = pd.Index(target.composite.columns)
    composite = _align(target.composite, index, columns)
    membership = (
        _align(target.membership_mask, index, columns)
        .fillna(False)
        .astype(bool)
    )
    tradable = (
        _align(target.tradable_mask, index, columns)
        .fillna(False)
        .astype(bool)
    )
    eligible = membership & tradable & composite.notna()
    rank, percentile = _rank_and_percentile(composite, eligible)
    daily_group = _daily_signal_groups(
        composite,
        eligible,
        int(target.effective_n_groups),
    )

    target_weights = pd.DataFrame(
        0.0 if is_rebalance else np.nan,
        index=index,
        columns=columns,
    )
    if is_rebalance:
        for row in target.target_weights.itertuples(index=False):
            ticker = str(row.ticker)
            if ticker in target_weights.columns:
                target_weights.loc[decision_date, ticker] = float(
                    row.target_weight
                )

    held_weights = pd.DataFrame(0.0, index=index, columns=columns)
    if positions is not None and not positions.empty:
        for row in positions.itertuples(index=False):
            ticker = str(getattr(row, "ticker", ""))
            if ticker in held_weights.columns:
                held_weights.loc[decision_date, ticker] = float(
                    getattr(row, "weight", 0.0) or 0.0
                )
    market_return = _align(target.market_returns, index, columns)
    # End-of-day paper positions cannot exactly attribute same-day P&L when
    # orders filled at the open. Keep the field unavailable until a start-of-day
    # position and cash-flow ledger can reconstruct per-stock realized P&L.
    daily_contributions = pd.DataFrame(
        np.nan,
        index=index,
        columns=columns,
    )

    factor_ids = list(target.normalized_weights.keys())
    factors: dict[str, dict[str, pd.DataFrame]] = {}
    for factor_id in factor_ids:
        factors[factor_id] = {
            "clean": _align(
                target.factor_clean.get(factor_id),
                index,
                columns,
            ),
            "strategy_input": _align(
                target.factor_inputs.get(factor_id),
                index,
                columns,
            ),
            "contribution": _align(
                target.factor_contributions.get(factor_id),
                index,
                columns,
            ),
        }
        raw = target.factor_raw.get(factor_id)
        if raw is not None and not raw.empty:
            factors[factor_id]["raw"] = _align(raw, index, columns)

    max_contribution_error = _validate_contributions(
        composite,
        {
            factor_id: values["contribution"]
            for factor_id, values in factors.items()
        },
    )

    summary = pd.DataFrame(index=index)
    summary["is_rebalance"] = bool(is_rebalance)
    summary["execution_date"] = ""
    summary["universe_count"] = membership.sum(axis=1).astype(int)
    summary["eligible_count"] = eligible.sum(axis=1).astype(int)
    summary["signal_top_count"] = daily_group.eq(
        int(target.top_group)
    ).sum(axis=1).astype(int)
    summary["held_count"] = int(
        (held_weights.loc[decision_date] > 0).sum()
    )
    summary["gross_return"] = np.nan
    summary["net_return"] = np.nan
    summary["benchmark_return"] = np.nan
    summary["nav"] = (
        float(equity) / float(account.get("initial_cash") or equity)
        if float(account.get("initial_cash") or 0.0) > 0
        else 1.0
    )
    summary["turnover"] = np.nan
    summary["total_fee"] = 0.0
    summary["total_slippage_cost"] = 0.0
    summary["total_cost_cash"] = 0.0
    summary["cost"] = 0.0
    summary["cash"] = float(cash)
    summary["equity"] = float(equity)

    strategy_snapshot = dict(account.get("strategy_snapshot") or {})
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source_kind": "paper",
        "source_id": source_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "strategy_snapshot": strategy_snapshot,
        "strategy_hash": _canonical_hash(strategy_snapshot),
        "universe": str(account.get("universe") or ""),
        "date_start": decision_date.strftime("%Y-%m-%d"),
        "date_end": decision_date.strftime("%Y-%m-%d"),
        "trading_days": 1,
        "tickers": int(len(columns)),
        "factor_ids": factor_ids,
        "normalized_weights": {
            str(k): float(v)
            for k, v in target.normalized_weights.items()
        },
        "raw_factor_available": {
            factor_id: "raw" in factors[factor_id]
            for factor_id in factor_ids
        },
        "n_groups": int(target.effective_n_groups),
        "top_group": int(target.top_group),
        "execution": dict(account.get("execution") or {}),
        "point_in_time_universe": dict(target.pit_diagnostics or {}),
        "portfolio_contribution_available": False,
        "audit": {
            "max_factor_contribution_error": max_contribution_error,
        },
    }

    empty_group = pd.DataFrame(np.nan, index=index, columns=columns)
    return DecisionReplaySnapshot(
        manifest=manifest,
        daily_summary=summary,
        market={
            "close": _align(target.prices, index, columns),
            "daily_return": market_return,
            "volume": _align(target.volumes, index, columns),
            "effective_return": empty_group.copy(),
        },
        signals={
            "composite": composite,
            "rank": rank,
            "percentile": percentile,
            "daily_signal_group": daily_group,
            "decision_group": (
                daily_group.copy()
                if is_rebalance
                else empty_group.copy()
            ),
            "held_group": empty_group.copy(),
            "eligible": eligible,
            "tradable": tradable,
            "pit_membership": membership,
            "exclusion_reason": _exclusion_reasons(
                composite,
                membership,
                tradable,
            ),
        },
        factors=factors,
        portfolio={
            "daily_weights": held_weights,
            "return_weights": held_weights.copy(),
            "daily_contributions": daily_contributions,
            "decision_target_weights": target_weights,
        },
    )


__all__ = [
    "SCHEMA_VERSION",
    "build_backtest_snapshot",
    "build_paper_snapshot",
]
