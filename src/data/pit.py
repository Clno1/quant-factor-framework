"""
Point-in-time universe membership support.

Expected membership files:
  data/pit_universes/<UNIVERSE>.parquet
  data/pit_universes/<UNIVERSE>.csv

Required columns: date, ticker
Optional active column: active / in_universe / is_member. Missing means active=True.
Each date is treated as a membership snapshot. For a backtest date, the latest
snapshot on or before that date is used.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import CONFIG, PROJECT_ROOT
from src.data.membership_state import (
    MembershipContractError,
    complete_snapshot_dates,
    replay_membership_states,
)
from src.data.universe_ids import LEGACY_US_ACTIVE, US_LIQUID_5M
from src.utils.identifiers import (
    InvalidResourceId,
    canonical_ticker,
    safe_path_component,
)

_MEMBERSHIP_FROM_CONFIG = object()


@dataclass
class PITDiagnostics:
    applied: bool
    required: bool
    source: str | None
    snapshots: int
    first_snapshot: str | None
    last_snapshot: str | None
    warning: str | None = None
    source_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _membership_dir() -> Path:
    try:
        raw = str(CONFIG.universe.point_in_time.membership_dir)
    except Exception:
        raw = "data/pit_universes"
    p = Path(raw)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _candidate_paths(universe: str) -> list[Path]:
    u = safe_path_component(
        str(universe).upper(),
        label="universe",
    )
    return [
        _membership_dir() / f"{u}.parquet",
        _membership_dir() / f"{u}.csv",
    ]


def point_in_time_required(
    universe: str,
    *,
    strict: bool | None = None,
) -> bool:
    """Return whether a named universe must have historical membership."""
    name = str(universe).strip().upper()
    if name == LEGACY_US_ACTIVE:
        name = US_LIQUID_5M
    try:
        settings = CONFIG.universe.point_in_time
        static_universes = {
            str(value).strip().upper()
            for value in getattr(settings, "static_universes", [])
        }
        required_universes = {
            str(value).strip().upper()
            for value in getattr(settings, "required_universes", [])
        }
    except Exception:
        static_universes = {"MAG7"}
        required_universes = {"SP500", "US_LIQUID_5M"}
    if name.startswith("WATCHLIST:") or name in static_universes:
        return False
    if name in required_universes:
        return True
    # With an explicit allowlist, unknown/custom universes are treated as
    # intentional static sets. Without one, strict retains its old meaning.
    if required_universes:
        return False
    return bool(strict)


def find_membership_file(universe: str) -> Path | None:
    for p in _candidate_paths(universe):
        if p.exists():
            return p
    return None


def _parse_bool_series(s: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(s):
        return s.fillna(False).astype(bool)
    if pd.api.types.is_numeric_dtype(s):
        return s.fillna(0).astype(float) != 0
    return s.astype(str).str.strip().str.lower().isin({"1", "true", "t", "yes", "y"})


def load_point_in_time_membership(universe: str) -> tuple[pd.DataFrame | None, Path | None]:
    """Load and normalize a PIT membership table if one exists."""
    path = find_membership_file(universe)
    if path is None:
        return None, None
    if path.suffix.lower() == ".parquet":
        df = pd.read_parquet(path)
    elif path.suffix.lower() == ".csv":
        df = pd.read_csv(path)
    else:
        raise ValueError(f"Unsupported PIT membership file type: {path}")

    rename = {c: c.strip().lower() for c in df.columns}
    df = df.rename(columns=rename)
    if "date" not in df.columns or "ticker" not in df.columns:
        raise ValueError(
            f"PIT membership file {path} must contain columns: date, ticker"
        )

    active_col = None
    for c in ("active", "in_universe", "is_member"):
        if c in df.columns:
            active_col = c
            break
    out = df[["date", "ticker"] + ([active_col] if active_col else [])].copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce", utc=True)
    if out["date"].isna().any() or out["ticker"].isna().any():
        raise ValueError(
            f"PIT membership file {path} contains empty/invalid date or ticker"
        )
    out["date"] = out["date"].dt.tz_convert(None).dt.normalize()
    try:
        out["ticker"] = out["ticker"].map(canonical_ticker)
    except InvalidResourceId as exc:
        raise ValueError(
            f"PIT membership file {path} contains an invalid ticker"
        ) from exc
    if active_col:
        out["active"] = _parse_bool_series(out[active_col])
        if active_col != "active":
            out = out.drop(columns=[active_col])
    else:
        out["active"] = True
    duplicates = out.duplicated(subset=["date", "ticker"], keep=False)
    if duplicates.any():
        conflicting = (
            out.loc[duplicates]
            .groupby(["date", "ticker"])["active"]
            .nunique()
            .gt(1)
        )
        if conflicting.any():
            sample = [
                f"{pd.Timestamp(date).date()}:{ticker}"
                for date, ticker in conflicting[conflicting].index[:10]
            ]
            raise ValueError(
                f"PIT membership file {path} has conflicting duplicate rows: "
                f"{sample}"
            )
        out = out.drop_duplicates(subset=["date", "ticker"], keep="last")
    return out.sort_values(["date", "ticker"]).reset_index(drop=True), path


def build_membership_mask(
    index: pd.Index,
    columns: pd.Index,
    universe: str,
    *,
    required: bool = False,
    membership_override: pd.DataFrame | None | object = _MEMBERSHIP_FROM_CONFIG,
    membership_source: str | None = None,
    membership_source_sha256: str | None = None,
) -> tuple[pd.DataFrame | None, PITDiagnostics]:
    if membership_override is _MEMBERSHIP_FROM_CONFIG:
        membership, path = load_point_in_time_membership(universe)
        source = str(path) if path is not None else None
        source_sha256 = (
            _file_sha256(path)
            if path is not None and path.exists()
            else None
        )
    else:
        membership = (
            membership_override.copy()
            if isinstance(membership_override, pd.DataFrame)
            else None
        )
        source = membership_source
        source_sha256 = membership_source_sha256
        if membership is not None:
            missing = {"date", "ticker", "active"} - set(membership.columns)
            if missing:
                raise ValueError(
                    f"PIT membership override is missing columns: {sorted(missing)}"
                )
            membership["date"] = pd.to_datetime(
                membership["date"], errors="coerce"
            ).dt.normalize()
            membership["ticker"] = membership["ticker"].map(canonical_ticker)
            membership["active"] = _parse_bool_series(membership["active"])
            if membership[["date", "ticker"]].isna().any().any():
                raise ValueError(
                    "PIT membership override contains invalid date or ticker"
                )
            membership = (
                membership.drop_duplicates(
                    ["date", "ticker"], keep="last"
                )
                .sort_values(["date", "ticker"])
                .reset_index(drop=True)
            )

    if membership is None:
        warning = (
            f"No point-in-time membership snapshot found for {universe}; "
            "using current/static universe constituents."
        )
        if required:
            raise FileNotFoundError(warning)
        return None, PITDiagnostics(
            applied=False,
            required=required,
            source=source,
            snapshots=0,
            first_snapshot=None,
            last_snapshot=None,
            warning=warning,
        )

    cols = pd.Index([str(c).upper() for c in columns], name="ticker")
    if cols.has_duplicates:
        duplicates = sorted(set(cols[cols.duplicated()].tolist()))
        raise ValueError(
            f"Factor/price matrix has duplicate tickers after normalization: "
            f"{duplicates[:10]}"
        )
    matrix_index = pd.DatetimeIndex(pd.to_datetime(index)).sort_values()
    if matrix_index.empty:
        raise ValueError("Cannot build PIT mask for an empty date index")
    if matrix_index.has_duplicates:
        raise ValueError("Cannot build PIT mask for duplicate dates")
    try:
        snapshots = complete_snapshot_dates(membership)
    except MembershipContractError as exc:
        raise ValueError(str(exc)) from exc
    if snapshots.empty:
        raise ValueError(
            f"PIT membership {source or universe} contains no snapshots"
        )
    first_date = pd.Timestamp(matrix_index.min())
    last_date = pd.Timestamp(matrix_index.max())
    baseline_position = snapshots.searchsorted(first_date, side="right") - 1
    if baseline_position < 0:
        raise ValueError(
            f"PIT membership {source or universe} starts at "
            f"{pd.Timestamp(snapshots.min()).date()}, after backtest start "
            f"{first_date.date()}; historical membership is unknown"
        )

    mask = pd.DataFrame(False, index=matrix_index, columns=columns)
    active_tickers: set[str] = set()
    try:
        states = replay_membership_states(
            membership,
            matrix_index,
            key_column="ticker",
        )
        for state in states:
            active_tickers.update(state.active_keys)
            active_cols = [
                column
                for column, upper in zip(columns, cols)
                if upper in state.active_keys
            ]
            if active_cols:
                mask.loc[state.date, active_cols] = True
    except MembershipContractError as exc:
        raise ValueError(str(exc)) from exc
    missing_tickers = sorted(active_tickers - set(cols))
    if missing_tickers:
        raise ValueError(
            f"PIT membership for {universe} contains "
            f"{len(missing_tickers)} historically active tickers absent from "
            "the factor/price matrix. Rebuild data for the union of historical "
            f"constituents. Sample: {missing_tickers[:20]}"
        )

    return mask, PITDiagnostics(
        applied=True,
        required=required,
        source=source,
        snapshots=len(snapshots),
        first_snapshot=pd.Timestamp(snapshots.min()).strftime("%Y-%m-%d"),
        last_snapshot=pd.Timestamp(snapshots.max()).strftime("%Y-%m-%d"),
        source_sha256=source_sha256,
    )


def apply_point_in_time_mask(
    values: pd.DataFrame,
    universe: str,
    *,
    required: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Apply a PIT membership mask to a date x ticker matrix when available."""
    mask, diag = build_membership_mask(
        values.index,
        values.columns,
        universe,
        required=required,
    )
    if mask is None:
        return values, diag.to_dict()
    masked = values.where(mask)
    masked = masked.dropna(how="all")
    if masked.empty:
        raise ValueError(
            f"PIT membership mask for {universe} removed all factor observations."
        )
    return masked, diag.to_dict()


__all__ = [
    "PITDiagnostics",
    "point_in_time_required",
    "find_membership_file",
    "load_point_in_time_membership",
    "build_membership_mask",
    "apply_point_in_time_mask",
]
