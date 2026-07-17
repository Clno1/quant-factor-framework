"""Stage-1 group-return aggregation and deterministic ranking.

All functions are side-effect free.  ``aggregate_groups`` expects one row per
*expected counting unit* and keeps invalid rows in the member audit output;
missing returns therefore reduce coverage instead of disappearing.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

import numpy as np
import pandas as pd

from .confidence import compute_snapshot_quality, evaluate_ranking
from .models import ReasonCode, sorted_reason_codes
from .settings import DailyReturnSettings, GroupAnalyticsSettings, RankingSettings


@dataclass(frozen=True, slots=True)
class WinsorizationResult:
    values: pd.Series
    was_winsorized: pd.Series
    median: float | None
    mad: float | None
    robust_sigma: float | None
    lower: float | None
    upper: float | None
    applied: bool


@dataclass(slots=True)
class SingleGroupAggregation:
    metric: dict[str, object]
    members: pd.DataFrame
    contributions: pd.DataFrame


@dataclass(slots=True)
class GroupAggregationResult:
    metrics: pd.DataFrame
    members: pd.DataFrame
    contributions: pd.DataFrame
    top: pd.DataFrame
    bottom: pd.DataFrame


def _finite_numeric(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce").astype(float)
    return numeric.where(np.isfinite(numeric.to_numpy(dtype=float)))


def _reasons(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, ReasonCode)):
        return sorted_reason_codes([value])
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted_reason_codes(list(value))
    try:
        if bool(pd.isna(value)):
            return []
    except (TypeError, ValueError):
        pass
    raise TypeError("reason_codes must be a string, iterable of strings, or null")


def mad_winsorize(
    returns: pd.Series,
    *,
    n_sigma: float = 3.0,
    min_members: int = 5,
) -> WinsorizationResult:
    """MAD-clip finite returns while preserving invalid observations.

    Fewer than ``min_members`` observations or MAD=0 leaves values unchanged.
    The raw (unscaled) MAD is reported as ``mad``; ``robust_sigma`` applies the
    frozen 1.4826 normal-consistency factor.
    """

    if not isinstance(returns, pd.Series):
        raise TypeError("returns must be a pandas Series")
    if not math.isfinite(float(n_sigma)) or n_sigma < 0:
        raise ValueError("n_sigma must be finite and non-negative")
    if min_members < 1:
        raise ValueError("min_members must be positive")

    numeric = _finite_numeric(returns)
    valid = numeric.dropna()
    output = numeric.copy()
    changed = pd.Series(False, index=numeric.index, dtype=bool)
    if valid.empty:
        return WinsorizationResult(
            values=output,
            was_winsorized=changed,
            median=None,
            mad=None,
            robust_sigma=None,
            lower=None,
            upper=None,
            applied=False,
        )

    median = float(valid.median())
    mad = float((valid - median).abs().median())
    robust_sigma = 1.4826 * mad
    lower = median - float(n_sigma) * robust_sigma
    upper = median + float(n_sigma) * robust_sigma
    apply_clip = len(valid) >= min_members and mad > 0.0
    if apply_clip:
        clipped = valid.clip(lower=lower, upper=upper)
        output.loc[valid.index] = clipped
        changed.loc[valid.index] = clipped.ne(valid)
    return WinsorizationResult(
        values=output,
        was_winsorized=changed,
        median=median,
        mad=mad,
        robust_sigma=robust_sigma,
        lower=lower,
        upper=upper,
        applied=apply_clip,
    )


def compute_breadth(
    returns: pd.Series,
    *,
    unchanged_band_bps: float = 1.0,
) -> dict[str, int | float | None]:
    """Compute advance/decline breadth with strict boundaries around ±band."""

    if not math.isfinite(float(unchanged_band_bps)) or unchanged_band_bps < 0:
        raise ValueError("unchanged_band_bps must be finite and non-negative")
    valid = _finite_numeric(returns).dropna()
    if valid.empty:
        return {
            "advance_count": 0,
            "decline_count": 0,
            "unchanged_count": 0,
            "up_pct": None,
            "down_pct": None,
            "breadth_net": None,
            "ad_ratio": None,
        }
    band = float(unchanged_band_bps) / 10_000.0
    advances = int(valid.gt(band).sum())
    declines = int(valid.lt(-band).sum())
    unchanged = int(len(valid) - advances - declines)
    denominator = len(valid)
    return {
        "advance_count": advances,
        "decline_count": declines,
        "unchanged_count": unchanged,
        "up_pct": advances / denominator,
        "down_pct": declines / denominator,
        "breadth_net": (advances - declines) / denominator,
        "ad_ratio": (advances + 0.5) / (declines + 0.5),
    }


def _single_value(frame: pd.DataFrame, column: str, default: object) -> object:
    if column not in frame.columns:
        return default
    values = frame[column].dropna().unique()
    if len(values) == 0:
        return default
    if len(values) > 1:
        raise ValueError(f"group rows have conflicting {column} values")
    return values[0]


def _stable_contribution_order(
    frame: pd.DataFrame,
    *,
    ascending: bool,
) -> pd.DataFrame:
    return frame.sort_values(
        ["headline_contribution", "ticker", "security_id"],
        ascending=[ascending, True, True],
        kind="mergesort",
        na_position="last",
    )


def aggregate_group_members(
    members: pd.DataFrame,
    *,
    daily_return: DailyReturnSettings | None = None,
    ranking: RankingSettings | None = None,
    benchmark_return_1d: float | None = None,
) -> SingleGroupAggregation:
    """Aggregate one group and return metric/member/contribution audit tables.

    Required columns are ``group_id``, ``security_id``, ``ticker`` and
    ``raw_return_1d``.  ``group_name``, ``level``, ``reason_codes`` and an
    upstream ``is_valid_for_headline`` mask are optional.  Every input row is
    counted in ``n_expected`` and retained in the returned member table.
    """

    if not isinstance(members, pd.DataFrame):
        raise TypeError("members must be a pandas DataFrame")
    required = {"group_id", "security_id", "ticker", "raw_return_1d"}
    missing = sorted(required.difference(members.columns))
    if missing:
        raise ValueError(f"members missing required columns: {missing}")
    if members.empty:
        raise ValueError("aggregate_group_members requires at least one expected member")
    if members["group_id"].isna().any():
        raise ValueError("group_id cannot be null")
    if members[["group_id", "security_id"]].duplicated().any():
        raise ValueError("group_id/security_id member keys must be unique")
    if members["group_id"].nunique(dropna=False) != 1:
        raise ValueError("aggregate_group_members accepts exactly one group")

    daily = daily_return or DailyReturnSettings()
    rank_cfg = ranking or RankingSettings()
    audit = members.copy(deep=True).reset_index(drop=True)
    audit["group_id"] = audit["group_id"].astype(str)
    audit["security_id"] = audit["security_id"].astype(str)
    audit["ticker"] = audit["ticker"].astype(str)
    audit["raw_return_1d"] = _finite_numeric(audit["raw_return_1d"])
    if "reason_codes" not in audit.columns:
        audit["reason_codes"] = [[] for _ in range(len(audit))]
    else:
        audit["reason_codes"] = audit["reason_codes"].map(_reasons)

    finite_return = audit["raw_return_1d"].notna()
    if "is_valid_for_headline" in audit.columns:
        upstream_valid = audit["is_valid_for_headline"].fillna(False).astype(bool)
        valid_mask = finite_return & upstream_valid
    else:
        valid_mask = finite_return
    audit["is_valid_for_headline"] = valid_mask.astype(bool)
    for row_index in audit.index[~finite_return]:
        audit.at[row_index, "reason_codes"] = sorted_reason_codes(
            [*audit.at[row_index, "reason_codes"], ReasonCode.MISSING_RETURN]
        )

    n_expected = int(len(audit))
    n_valid = int(valid_mask.sum())
    raw_valid = audit.loc[valid_mask, "raw_return_1d"]
    winsor = mad_winsorize(
        audit["raw_return_1d"].where(valid_mask),
        n_sigma=daily.winsorize_n,
        min_members=daily.min_members_for_winsorize,
    )
    audit["winsorized_return_1d"] = winsor.values.where(valid_mask)
    audit["was_winsorized"] = winsor.was_winsorized & valid_mask
    audit["winsor_lower"] = winsor.lower
    audit["winsor_upper"] = winsor.upper
    audit["headline_weight"] = np.nan
    audit["headline_contribution"] = np.nan
    audit["contribution_bps"] = np.nan
    audit["contribution_rank"] = pd.Series(pd.NA, index=audit.index, dtype="Int64")

    if n_valid > 0:
        weight = 1.0 / n_valid
        audit.loc[valid_mask, "headline_weight"] = weight
        audit.loc[valid_mask, "headline_contribution"] = (
            audit.loc[valid_mask, "winsorized_return_1d"] * weight
        )
        audit.loc[valid_mask, "contribution_bps"] = (
            10_000.0 * audit.loc[valid_mask, "headline_contribution"]
        )
        contribution_order = _stable_contribution_order(
            audit.loc[valid_mask], ascending=False
        )
        for contribution_rank, row_index in enumerate(
            contribution_order.index, start=1
        ):
            audit.at[row_index, "contribution_rank"] = contribution_rank

    quality = compute_snapshot_quality(
        n_expected,
        n_valid,
        fresh_quote_coverage=1.0,
        min_count_coverage=rank_cfg.min_count_coverage,
    )
    rank_assessment = evaluate_ranking(
        quality,
        min_members=rank_cfg.min_members,
        min_count_coverage=rank_cfg.min_count_coverage,
        min_freshness_coverage=rank_cfg.min_freshness_coverage,
        allowed_quality_grades=rank_cfg.allowed_quality_grades,
    )
    breadth = compute_breadth(
        raw_valid,
        unchanged_band_bps=daily.unchanged_band_bps,
    )

    if n_valid == 0:
        raw_ew: float | None = None
        robust_ew: float | None = None
        median_return: float | None = None
        dispersion_mad: float | None = None
        dispersion_std: float | None = None
    else:
        raw_ew = float(raw_valid.mean())
        robust_ew = float(audit.loc[valid_mask, "winsorized_return_1d"].mean())
        median_return = float(raw_valid.median())
        dispersion_mad = winsor.mad
        # Freeze the Stage-1 contract as sample standard deviation (ddof=1).
        dispersion_std = (
            float(raw_valid.std(ddof=1)) if n_valid >= 2 else None
        )

    valid_contributions = audit.loc[valid_mask].copy()
    if valid_contributions.empty:
        top_driver: str | None = None
        bottom_driver: str | None = None
        single_name_concentration: float | None = None
    else:
        top_row = _stable_contribution_order(
            valid_contributions, ascending=False
        ).iloc[0]
        bottom_row = _stable_contribution_order(
            valid_contributions, ascending=True
        ).iloc[0]
        top_driver = str(top_row["ticker"])
        bottom_driver = str(bottom_row["ticker"])
        absolute_sum = float(valid_contributions["headline_contribution"].abs().sum())
        single_name_concentration = (
            None
            if absolute_sum == 0.0
            else float(
                valid_contributions["headline_contribution"].abs().max()
                / absolute_sum
            )
        )

    group_reasons: list[str | ReasonCode] = list(rank_assessment.reason_codes)
    if (
        single_name_concentration is not None
        and single_name_concentration
        > rank_cfg.single_name_concentration_warning
    ):
        group_reasons.append(ReasonCode.SINGLE_NAME_CONCENTRATION)

    benchmark: float | None
    relative: float | None
    if (
        benchmark_return_1d is None
        or not math.isfinite(float(benchmark_return_1d))
        or float(benchmark_return_1d) <= -1.0
    ):
        benchmark = None
        relative = None
        group_reasons.append(ReasonCode.BENCHMARK_UNAVAILABLE)
    else:
        benchmark = float(benchmark_return_1d)
        relative = (
            None
            if robust_ew is None
            else (1.0 + robust_ew) / (1.0 + benchmark) - 1.0
        )

    group_id = str(audit["group_id"].iloc[0])
    group_name = str(_single_value(audit, "group_name", group_id))
    level_value = _single_value(audit, "level", None)
    metric: dict[str, object] = {
        "group_id": group_id,
        "group_name": group_name,
        "level": None if level_value is None else str(level_value),
        **quality.as_dict(),
        "weight_coverage": quality.weight_coverage,
        "fresh_quote_coverage": quality.fresh_quote_coverage,
        "raw_ew_return_1d": raw_ew,
        "robust_ew_return_1d": robust_ew,
        "median_return_1d": median_return,
        # Stage 1 CAP is schema-only and must never silently use EW.
        "cap_return_1d": None,
        "cap_availability_coverage": None,
        "cap_return_coverage": None,
        "cap_n_effective": None,
        "cap_type": "UNAVAILABLE",
        "cap_status": "UNAVAILABLE",
        **breadth,
        "dispersion_mad": dispersion_mad,
        "dispersion_std": dispersion_std,
        "benchmark_return_1d": benchmark,
        "headline_relative_return_1d": relative,
        "driver_method": "ROBUST_EW",
        "top_driver_ticker": top_driver,
        "bottom_driver_ticker": bottom_driver,
        "single_name_concentration": single_name_concentration,
        "eligible_for_ranking": rank_assessment.eligible_for_ranking,
        "reason_codes": sorted_reason_codes(group_reasons),
    }

    contribution_columns = [
        "group_id",
        "group_name",
        "security_id",
        "ticker",
        "return_method",
        "weight",
        "input_return",
        "contribution",
        "contribution_bps",
        "rank_within_group",
        "reason_codes",
    ]
    contributions = pd.DataFrame(columns=contribution_columns)
    if n_valid > 0:
        contributions = valid_contributions[
            [
                "group_id",
                "security_id",
                "ticker",
                "winsorized_return_1d",
                "headline_weight",
                "headline_contribution",
                "contribution_bps",
                "contribution_rank",
                "reason_codes",
            ]
        ].copy()
        contributions.insert(1, "group_name", group_name)
        contributions.insert(4, "return_method", "ROBUST_EW")
        contributions = contributions.rename(
            columns={
                "winsorized_return_1d": "input_return",
                "headline_weight": "weight",
                "headline_contribution": "contribution",
                "contribution_rank": "rank_within_group",
            }
        )
        contributions = contributions[contribution_columns].sort_values(
            ["rank_within_group", "ticker", "security_id"],
            kind="mergesort",
        ).reset_index(drop=True)

    audit["reason_codes"] = audit["reason_codes"].map(_reasons)
    audit = audit.sort_values(
        ["ticker", "security_id"], kind="mergesort"
    ).reset_index(drop=True)
    return SingleGroupAggregation(metric, audit, contributions)


def rank_group_metrics(
    metrics: pd.DataFrame,
    *,
    top_n: int = 5,
    bottom_n: int = 5,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Add headline ranks and derive non-overlapping Top/Bottom tables."""

    if top_n < 0 or bottom_n < 0:
        raise ValueError("top_n and bottom_n must be non-negative")
    required = {
        "group_id",
        "robust_ew_return_1d",
        "up_pct",
        "n_valid",
        "eligible_for_ranking",
    }
    missing = sorted(required.difference(metrics.columns))
    if missing:
        raise ValueError(f"metrics missing ranking columns: {missing}")

    rows = metrics.copy(deep=True).reset_index(drop=True)
    rows["headline_rank"] = pd.Series(pd.NA, index=rows.index, dtype="Int64")
    eligible_mask = rows["eligible_for_ranking"].fillna(False).astype(bool)
    eligible_mask &= _finite_numeric(rows["robust_ew_return_1d"]).notna()
    ranked = rows.loc[eligible_mask].sort_values(
        ["robust_ew_return_1d", "up_pct", "n_valid", "group_id"],
        ascending=[False, False, False, True],
        kind="mergesort",
        na_position="last",
    )
    for headline_rank, row_index in enumerate(ranked.index, start=1):
        rows.at[row_index, "headline_rank"] = headline_rank

    ranked = rows.loc[ranked.index].copy()
    n_ranked = len(ranked)
    half = n_ranked // 2
    effective_top = min(top_n, half)
    effective_bottom = min(bottom_n, half)
    top = ranked.head(effective_top).copy().reset_index(drop=True)
    bottom = ranked.tail(effective_bottom).iloc[::-1].copy().reset_index(drop=True)
    top["view_rank"] = pd.Series(range(1, len(top) + 1), dtype="Int64")
    bottom["view_rank"] = pd.Series(range(1, len(bottom) + 1), dtype="Int64")

    eligible_rows = rows.loc[eligible_mask].sort_values(
        "headline_rank", kind="mergesort"
    )
    ineligible_rows = rows.loc[~eligible_mask].sort_values(
        "group_id", kind="mergesort"
    )
    rows = pd.concat([eligible_rows, ineligible_rows], ignore_index=True)
    return rows, top, bottom


def aggregate_groups(
    members: pd.DataFrame,
    *,
    settings: GroupAnalyticsSettings | None = None,
    benchmark_return_1d: float | None = None,
) -> GroupAggregationResult:
    """Aggregate all groups and return metrics/audits/contributions/rank views."""

    if not isinstance(members, pd.DataFrame):
        raise TypeError("members must be a pandas DataFrame")
    cfg = settings or GroupAnalyticsSettings()
    if members.empty:
        return GroupAggregationResult(
            metrics=pd.DataFrame(),
            members=members.copy(),
            contributions=pd.DataFrame(),
            top=pd.DataFrame(),
            bottom=pd.DataFrame(),
        )
    if "group_id" not in members.columns:
        raise ValueError("members missing required column: group_id")

    results: list[SingleGroupAggregation] = []
    for _, group_rows in members.groupby("group_id", sort=True, dropna=False):
        results.append(
            aggregate_group_members(
                group_rows,
                daily_return=cfg.daily_return,
                ranking=cfg.ranking,
                benchmark_return_1d=benchmark_return_1d,
            )
        )
    metrics = pd.DataFrame([result.metric for result in results])
    member_audit = pd.concat(
        [result.members for result in results], ignore_index=True
    )
    non_empty_contributions: Iterable[pd.DataFrame] = (
        result.contributions
        for result in results
        if not result.contributions.empty
    )
    contribution_frames = list(non_empty_contributions)
    contributions = (
        pd.concat(contribution_frames, ignore_index=True)
        if contribution_frames
        else pd.DataFrame()
    )
    metrics, top, bottom = rank_group_metrics(
        metrics,
        top_n=cfg.ranking.top_n,
        bottom_n=cfg.ranking.bottom_n,
    )
    return GroupAggregationResult(
        metrics=metrics,
        members=member_audit,
        contributions=contributions,
        top=top,
        bottom=bottom,
    )


__all__ = [
    "GroupAggregationResult",
    "SingleGroupAggregation",
    "WinsorizationResult",
    "aggregate_group_members",
    "aggregate_groups",
    "compute_breadth",
    "mad_winsorize",
    "rank_group_metrics",
]
