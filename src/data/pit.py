"""
Point-in-time universe membership support.

Expected membership files:
  data/pit_universes/<UNIVERSE>.parquet
  data/pit_universes/<UNIVERSE>.csv
  data/processed/<UNIVERSE>/membership.parquet
  data/processed/<UNIVERSE>/membership.csv

Required columns: date, ticker
Optional active column: active / in_universe / is_member. Missing means active=True.
Each date is treated as a membership snapshot. For a backtest date, the latest
snapshot on or before that date is used.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import CONFIG, PROJECT_ROOT


@dataclass
class PITDiagnostics:
    applied: bool
    required: bool
    source: str | None
    snapshots: int
    first_snapshot: str | None
    last_snapshot: str | None
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _membership_dir() -> Path:
    try:
        raw = str(CONFIG.universe.point_in_time.membership_dir)
    except Exception:
        raw = "data/pit_universes"
    p = Path(raw)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _candidate_paths(universe: str) -> list[Path]:
    u = universe.upper()
    return [
        _membership_dir() / f"{u}.parquet",
        _membership_dir() / f"{u}.csv",
        PROJECT_ROOT / "data" / "processed" / u / "membership.parquet",
        PROJECT_ROOT / "data" / "processed" / u / "membership.csv",
    ]


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
    out["date"] = pd.to_datetime(out["date"])
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    if active_col:
        out["active"] = _parse_bool_series(out[active_col])
        out = out.drop(columns=[active_col])
    else:
        out["active"] = True
    out = out.dropna(subset=["date", "ticker"]).drop_duplicates(
        subset=["date", "ticker"],
        keep="last",
    )
    return out.sort_values(["date", "ticker"]).reset_index(drop=True), path


def build_membership_mask(
    index: pd.Index,
    columns: pd.Index,
    universe: str,
    *,
    required: bool = False,
) -> tuple[pd.DataFrame | None, PITDiagnostics]:
    membership, path = load_point_in_time_membership(universe)
    if membership is None or path is None:
        warning = (
            f"No point-in-time membership file found for {universe}; "
            "using current/static universe constituents."
        )
        if required:
            raise FileNotFoundError(warning)
        return None, PITDiagnostics(
            applied=False,
            required=required,
            source=None,
            snapshots=0,
            first_snapshot=None,
            last_snapshot=None,
            warning=warning,
        )

    cols = pd.Index([str(c).upper() for c in columns], name="ticker")
    snapshots = pd.DatetimeIndex(sorted(membership["date"].dropna().unique()))
    if snapshots.empty:
        raise ValueError(f"PIT membership file {path} contains no snapshots")

    active_by_date: dict[pd.Timestamp, set[str]] = {}
    for dt, sub in membership.groupby("date"):
        active_by_date[pd.Timestamp(dt)] = set(sub.loc[sub["active"], "ticker"])

    mask = pd.DataFrame(False, index=pd.DatetimeIndex(index), columns=columns)
    for dt in mask.index:
        pos = snapshots.searchsorted(pd.Timestamp(dt), side="right") - 1
        if pos < 0:
            continue
        active = active_by_date.get(pd.Timestamp(snapshots[pos]), set())
        active_cols = [c for c, upper in zip(columns, cols) if upper in active]
        if active_cols:
            mask.loc[dt, active_cols] = True

    return mask, PITDiagnostics(
        applied=True,
        required=required,
        source=str(path),
        snapshots=len(snapshots),
        first_snapshot=pd.Timestamp(snapshots.min()).strftime("%Y-%m-%d"),
        last_snapshot=pd.Timestamp(snapshots.max()).strftime("%Y-%m-%d"),
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
    "find_membership_file",
    "load_point_in_time_membership",
    "build_membership_mask",
    "apply_point_in_time_mask",
]
