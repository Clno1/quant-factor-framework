"""Strategy-to-target-weight generation for paper trading."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.backtest.adhoc import adhoc_compose
from src.backtest.composer import compose_factor
from src.backtest.quintile import build_tradable_mask
from src.config import CONFIG
from src.data.access import load_published_bundle
from src.data.pit import build_membership_mask, point_in_time_required
from src.data.universe_ids import watchlist_snapshot_data_universe
from src.factors import get_factor
from src.factors.publication import validate_factor_research_publication
from src.strategies.definition import StrategyDefinition
from src.utils.date_utils import resolve_date_range


@dataclass
class TargetResult:
    target_weights: pd.DataFrame
    decision_date: str
    prices: pd.DataFrame
    open_prices: pd.DataFrame
    volumes: pd.DataFrame
    normalized_weights: dict[str, float]
    effective_n_groups: int
    top_group: int
    tickers_used: list[str] = field(default_factory=list)
    tickers_missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    market_returns: pd.DataFrame = field(default_factory=pd.DataFrame)
    composite: pd.DataFrame = field(default_factory=pd.DataFrame)
    membership_mask: pd.DataFrame = field(default_factory=pd.DataFrame)
    tradable_mask: pd.DataFrame = field(default_factory=pd.DataFrame)
    factor_raw: dict[str, pd.DataFrame] = field(default_factory=dict)
    factor_clean: dict[str, pd.DataFrame] = field(default_factory=dict)
    factor_inputs: dict[str, pd.DataFrame] = field(default_factory=dict)
    factor_contributions: dict[str, pd.DataFrame] = field(default_factory=dict)
    pit_diagnostics: dict[str, Any] = field(default_factory=dict)
    data_contract: dict[str, Any] = field(default_factory=dict)


def _watchlist_tickers(snapshot: dict[str, Any] | None) -> list[str]:
    items = (snapshot or {}).get("items") or []
    tickers = [str(it.get("ticker") or "").strip().upper() for it in items]
    return [t for t in tickers if t]


def _latest_row_at_or_before(df: pd.DataFrame, asof: str | None) -> tuple[pd.Timestamp, pd.Series]:
    non_empty = df.dropna(how="all")
    if non_empty.empty:
        raise ValueError("合成因子没有可用截面")
    if asof:
        cutoff = pd.Timestamp(asof)
        non_empty = non_empty.loc[non_empty.index <= cutoff]
        if non_empty.empty:
            raise ValueError(f"asof={asof} 之前没有可用合成因子截面")
    dt = pd.Timestamp(non_empty.index.max())
    return dt, non_empty.loc[dt]


def _effective_groups(n_available: int, n_groups: int) -> int:
    if n_available <= 0:
        return 0
    if n_available == 1:
        return 1
    if n_available < n_groups * 2:
        return max(1, min(n_groups, n_available // 2))
    return max(1, n_groups)


def _build_targets(
    row: pd.Series,
    *,
    prices: pd.DataFrame,
    decision_date: pd.Timestamp,
    n_groups: int,
    top_group: int,
) -> tuple[pd.DataFrame, int, int]:
    scores = row.dropna().astype("float64").sort_values(ascending=False)
    if scores.empty:
        raise ValueError("最新截面没有可用股票")
    effective = _effective_groups(len(scores), int(n_groups))
    top = min(max(int(top_group), 1), effective)

    if effective == 1:
        groups = pd.Series(1, index=scores.index, dtype="int64")
    else:
        labels = pd.qcut(
            scores.rank(method="first"),
            q=effective,
            labels=list(range(1, effective + 1)),
        )
        groups = labels.astype("int64")

    selected = groups[groups == top].index.tolist()
    target_weight = 1.0 / len(selected) if selected else 0.0
    px_row = (
        prices.reindex(columns=scores.index).ffill().loc[:decision_date].iloc[-1]
        if prices is not None and not prices.empty and len(prices.loc[:decision_date]) > 0
        else pd.Series(index=scores.index, dtype="float64")
    )
    out = pd.DataFrame({
        "ticker": scores.index,
        "score": scores.values,
        "group": groups.reindex(scores.index).astype("int64").values,
        "target_weight": [
            target_weight if ticker in selected else 0.0 for ticker in scores.index
        ],
        "decision_price": px_row.reindex(scores.index).values,
    })
    out = out.sort_values(
        ["target_weight", "score"], ascending=[False, False]
    ).reset_index(drop=True)
    return out, effective, top


def generate_target_weights(
    *,
    strategy: StrategyDefinition,
    universe: str,
    watchlist_snapshot: dict[str, Any] | None = None,
    asof: str | None = None,
    start: str | None = None,
    end: str | None = None,
    n_groups: int | None = None,
    top_group: int | None = None,
    tradability: dict[str, Any] | None = None,
    require_point_in_time: bool | None = None,
) -> TargetResult:
    """Generate the latest long-only top-group target weights."""
    strategy.validate()
    n_groups = int(n_groups or CONFIG.backtest.n_groups)
    top_group = int(top_group or n_groups)
    start_iso, end_iso, _ = resolve_date_range(
        start or CONFIG.date_range.start,
        end or asof or CONFIG.date_range.end,
    )

    warnings: list[str] = []
    tickers_used: list[str] = []
    tickers_missing: list[str] = []
    factor_ids = [component.factor_id for component in strategy.components]
    if universe.startswith("watchlist:"):
        tickers = _watchlist_tickers(watchlist_snapshot)
        if not tickers:
            raise ValueError("模拟盘 watchlist 快照为空，无法生成目标仓位")
        snapshot = watchlist_snapshot or {}
        data_universe = watchlist_snapshot_data_universe(snapshot)
        bundle = load_published_bundle(
            requested_universe=universe,
            data_universe=data_universe,
            tickers=tickers,
            start=start_iso,
            end=end_iso,
            require_open=True,
            exact_universe=True,
            factor_ids=factor_ids,
        )
        adhoc = adhoc_compose(
            components=strategy.components,
            tickers=tickers,
            start=start_iso,
            end=end_iso,
            wide=bundle.wide,
        )
        composite = adhoc.composite
        prices = adhoc.prices
        open_prices = adhoc.open_prices
        volumes = adhoc.volumes
        market_returns = adhoc.returns
        normalized = adhoc.normalized_weights
        factor_raw = adhoc.factor_raw
        factor_clean = adhoc.factor_clean
        factor_inputs = adhoc.factor_inputs
        factor_contributions = adhoc.factor_contributions
        tickers_used = adhoc.tickers_used
        tickers_missing = adhoc.tickers_missing
        warnings.extend(adhoc.warnings)
        membership_mask, pit_result = build_membership_mask(
            composite.index,
            composite.columns,
            universe=data_universe,
            required=True,
            membership_override=bundle.membership,
            membership_source=f"duckdb:{bundle.version.version_id}:membership",
            membership_source_sha256=(
                bundle.version.membership_checksum_sha256
            ),
        )
        if membership_mask is None:
            raise ValueError(
                f"Published custom universe {data_universe} has no membership"
            )
        pit_diagnostics = pit_result.to_dict()
        pit_diagnostics["warning"] = (
            "This account uses one frozen custom watchlist for all dates; "
            "it is a fixed-basket universe, not a reconstructed index."
        )
        warnings.append(pit_diagnostics["warning"])
    else:
        universe = universe.upper()
        bundle = load_published_bundle(
            requested_universe=universe,
            data_universe=universe,
            start=start_iso,
            end=end_iso,
            require_open=True,
            factor_ids=factor_ids,
            require_factor_publication=True,
        )
        comp = compose_factor(
            components=strategy.components,
            universe=universe,
            start=start_iso,
            end=end_iso,
            expected_generations=bundle.contract.factor_generations,
        )
        composite = comp.composite
        normalized = comp.normalized_weights
        wide = bundle.wide
        prices = wide.get("adj_close")
        if prices is None or prices.empty:
            prices = wide.get("close")
        if prices is None:
            prices = pd.DataFrame()
        open_prices = wide.get("open")
        if open_prices is None:
            open_prices = pd.DataFrame()
        volumes = wide.get("volume")
        if volumes is None:
            volumes = pd.DataFrame()
        market_returns = wide.get("returns")
        if market_returns is None:
            market_returns = prices.pct_change(fill_method=None)
        factor_raw = dict(comp.factor_raw)
        factor_clean = comp.factor_clean
        factor_inputs = comp.factor_inputs
        factor_contributions = comp.factor_contributions
        for component in strategy.components:
            factor_id = component.factor_id
            if factor_id not in factor_raw:
                factor_raw[factor_id] = get_factor(factor_id).compute_from_wide(
                    wide
                ).reindex(index=composite.index, columns=composite.columns)
        pit_required = point_in_time_required(
            universe,
            strict=(
                bool(require_point_in_time)
                if require_point_in_time is not None
                else bool(
                    getattr(
                        CONFIG.backtest,
                        "require_point_in_time_universe",
                        False,
                    )
                )
            ),
        )
        membership_mask, pit_result = build_membership_mask(
            composite.index,
            composite.columns,
            universe,
            required=pit_required,
            membership_override=bundle.membership,
            membership_source=f"duckdb:{bundle.version.version_id}:membership",
            membership_source_sha256=(
                bundle.version.membership_checksum_sha256
            ),
        )
        pit_diagnostics = pit_result.to_dict()
        if membership_mask is None:
            membership_mask = pd.DataFrame(
                True,
                index=composite.index,
                columns=composite.columns,
            )
        if pit_result.warning:
            warnings.append(pit_result.warning)
        tickers_used = list(composite.columns)
        validate_factor_research_publication(
            universe,
            version=bundle.version,
            factor_ids=factor_ids,
            publication_id=bundle.contract.factor_publication_id,
        )

    tradable_mask = build_tradable_mask(
        index=composite.index,
        columns=composite.columns,
        returns_df=market_returns,
        price_df=prices,
        open_df=open_prices,
        volume_df=volumes,
        # At decision time tomorrow's open is unknown. Orders may remain pending
        # until it arrives, so future open availability must not affect ranking.
        timing="close",
        tradability=tradability,
    )
    eligible_composite = composite.where(membership_mask & tradable_mask)
    decision_ts, row = _latest_row_at_or_before(eligible_composite, asof)
    targets, effective, top = _build_targets(
        row,
        prices=prices,
        decision_date=decision_ts,
        n_groups=n_groups,
        top_group=top_group,
    )
    targets.insert(0, "decision_date", decision_ts.strftime("%Y-%m-%d"))
    return TargetResult(
        target_weights=targets,
        decision_date=decision_ts.strftime("%Y-%m-%d"),
        prices=prices,
        open_prices=open_prices,
        volumes=volumes,
        normalized_weights=normalized,
        effective_n_groups=effective,
        top_group=top,
        tickers_used=tickers_used,
        tickers_missing=tickers_missing,
        warnings=warnings,
        market_returns=market_returns,
        composite=composite,
        membership_mask=membership_mask,
        tradable_mask=tradable_mask,
        factor_raw=factor_raw,
        factor_clean=factor_clean,
        factor_inputs=factor_inputs,
        factor_contributions=factor_contributions,
        pit_diagnostics=pit_diagnostics,
        data_contract=bundle.contract.to_dict(),
    )


__all__ = ["TargetResult", "generate_target_weights"]
