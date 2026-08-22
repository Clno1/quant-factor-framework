"""Replay complete PIT snapshots plus compact intramonth removal events."""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import pandas as pd

COMPLETE_SNAPSHOT_TYPES = frozenset({
    "MONTH_END",
    "FORCED_EXIT_CARRY_FORWARD",
})
REMOVAL_EVENT_TYPES = frozenset({"FORCED_EXIT"})


class MembershipContractError(ValueError):
    """Raised when a compact PIT membership artifact is structurally invalid."""


@dataclass(frozen=True)
class MembershipState:
    date: pd.Timestamp
    active_keys: frozenset[str]
    value_by_key: dict[str, str]


def normalize_membership_events(
    membership: pd.DataFrame,
    *,
    key_column: str,
    value_column: str | None = None,
) -> pd.DataFrame:
    required = {"date", key_column, "active"}
    missing = sorted(required - set(membership.columns))
    if missing:
        raise MembershipContractError(f"PIT membership is missing columns: {missing}")
    columns = ["date", key_column, "active"]
    if value_column and value_column not in columns:
        if value_column not in membership.columns:
            raise MembershipContractError(
                f"PIT membership is missing value column: {value_column}"
            )
        columns.append(value_column)
    if "snapshot_type" in membership.columns:
        columns.append("snapshot_type")
    frame = membership.loc[:, columns].copy()
    frame["date"] = pd.to_datetime(
        frame["date"], errors="coerce"
    ).dt.normalize()
    frame[key_column] = frame[key_column].fillna("").astype(str).str.strip()
    frame["active"] = frame["active"].fillna(False).astype(bool)
    if value_column:
        frame[value_column] = (
            frame[value_column].fillna("").astype(str).str.strip()
        )
    if "snapshot_type" in frame.columns:
        frame["snapshot_type"] = (
            frame["snapshot_type"].fillna("").astype(str).str.strip().str.upper()
        )
    else:
        frame["snapshot_type"] = ""
    if frame["date"].isna().any() or frame[key_column].eq("").any():
        raise MembershipContractError("PIT membership contains invalid dates or identities")
    if frame.duplicated(["date", key_column]).any():
        raise MembershipContractError("PIT membership contains duplicate date identities")
    return frame.sort_values(["date", key_column]).reset_index(drop=True)


def _group_kind(group: pd.DataFrame) -> str:
    kinds = set(group["snapshot_type"].astype(str)) - {""}
    if kinds and kinds.issubset(REMOVAL_EVENT_TYPES):
        if group["active"].any():
            raise MembershipContractError("PIT removal events must be inactive rows")
        return "REMOVE"
    if kinds & REMOVAL_EVENT_TYPES:
        raise MembershipContractError("PIT date mixes complete snapshots and removals")
    unknown = kinds - COMPLETE_SNAPSHOT_TYPES
    if unknown:
        raise MembershipContractError(
            f"PIT membership has unknown snapshot types: {sorted(unknown)}"
        )
    return "COMPLETE"


def complete_snapshot_dates(membership: pd.DataFrame) -> pd.DatetimeIndex:
    frame = normalize_membership_events(
        membership,
        key_column="security_id" if "security_id" in membership.columns else "ticker",
    )
    values = [
        pd.Timestamp(date)
        for date, group in frame.groupby("date", sort=True)
        if _group_kind(group) == "COMPLETE"
    ]
    return pd.DatetimeIndex(values).normalize()


def replay_membership_states(
    membership: pd.DataFrame,
    output_dates: Iterable[Any],
    *,
    key_column: str,
    value_column: str | None = None,
    require_baseline: bool = True,
) -> Iterator[MembershipState]:
    """Yield as-of states without expanding compact removal events to snapshots."""
    frame = normalize_membership_events(
        membership,
        key_column=key_column,
        value_column=value_column,
    )
    dates = pd.DatetimeIndex(pd.to_datetime(list(output_dates))).normalize()
    if dates.has_duplicates or not dates.is_monotonic_increasing:
        raise MembershipContractError("PIT replay dates must be sorted and unique")
    groups = [
        (pd.Timestamp(date), _group_kind(group), group)
        for date, group in frame.groupby("date", sort=True)
    ]
    complete_dates = [date for date, kind, _ in groups if kind == "COMPLETE"]
    if not complete_dates:
        raise MembershipContractError("PIT membership has no complete snapshots")
    if require_baseline and len(dates) and complete_dates[0] > dates[0]:
        raise MembershipContractError(
            f"PIT membership starts at {complete_dates[0].date()}, after requested "
            f"start {pd.Timestamp(dates[0]).date()}"
        )

    active: dict[str, str] = {}
    group_position = 0
    for output_date in dates:
        while group_position < len(groups) and groups[group_position][0] <= output_date:
            _event_date, kind, group = groups[group_position]
            if kind == "COMPLETE":
                active = {
                    str(row[key_column]): (
                        str(row[value_column]) if value_column else str(row[key_column])
                    )
                    for row in group.loc[group["active"]].to_dict("records")
                }
            else:
                for key in group.loc[~group["active"], key_column].astype(str):
                    active.pop(key, None)
            group_position += 1
        yield MembershipState(
            date=pd.Timestamp(output_date),
            active_keys=frozenset(active),
            value_by_key=dict(active),
        )


def resolve_membership_asof(
    membership: pd.DataFrame,
    asof: str | pd.Timestamp,
) -> pd.DataFrame:
    """Resolve active rows at one date from either legacy or compact membership."""
    required = {"date", "security_id", "active"}
    missing = sorted(required - set(membership.columns))
    if missing:
        raise MembershipContractError(f"PIT membership is missing columns: {missing}")
    frame = membership.copy()
    frame["date"] = pd.to_datetime(
        frame["date"], errors="coerce"
    ).dt.normalize()
    frame["security_id"] = frame["security_id"].fillna("").astype(str)
    frame["active"] = frame["active"].fillna(False).astype(bool)
    frame["snapshot_type"] = (
        frame["snapshot_type"].fillna("").astype(str).str.upper()
        if "snapshot_type" in frame.columns
        else ""
    )
    target = pd.Timestamp(asof).normalize()
    eligible_dates = frame.loc[frame["date"].le(target), "date"].dropna().unique()
    if not len(eligible_dates):
        return frame.iloc[0:0].copy()
    normalized = normalize_membership_events(
        frame,
        key_column="security_id",
        value_column="ticker" if "ticker" in frame.columns else None,
    )
    baseline_dates = [
        pd.Timestamp(date)
        for date, group in normalized.groupby("date", sort=True)
        if pd.Timestamp(date) <= target and _group_kind(group) == "COMPLETE"
    ]
    if not baseline_dates:
        return frame.iloc[0:0].copy()
    baseline = max(baseline_dates)
    base = frame.loc[frame["date"].eq(baseline) & frame["active"]].copy()
    removals = frame.loc[
        frame["date"].gt(baseline)
        & frame["date"].le(target)
        & ~frame["active"]
        & frame["snapshot_type"].isin(REMOVAL_EVENT_TYPES),
        "security_id",
    ].astype(str)
    if not removals.empty:
        base = base.loc[~base["security_id"].isin(set(removals))]
    return base.sort_values("security_id").reset_index(drop=True)


__all__ = [
    "COMPLETE_SNAPSHOT_TYPES",
    "MembershipContractError",
    "MembershipState",
    "REMOVAL_EVENT_TYPES",
    "complete_snapshot_dates",
    "normalize_membership_events",
    "replay_membership_states",
    "resolve_membership_asof",
]
