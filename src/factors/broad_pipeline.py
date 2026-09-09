"""Memory-bounded factor calculation for ``US_LIQUID_5M``.

The full coverage history is never pivoted at once.  One factor and one output
month are processed at a time, with only that factor's required warm-up window
loaded from partitioned coverage Parquet.  Columns use stable ``security_id``;
ticker remains display metadata and may change through time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from src.config import CONFIG, PROJECT_ROOT
from src.data.broad_coverage import BroadCoverageReader
from src.data.foundation import DataFoundationError, DatasetVersion
from src.data.membership_state import (
    complete_snapshot_dates,
    replay_membership_states,
)
from src.data.security_master import UNKNOWN_CLASSIFICATION
from src.factors import get_factor
from src.preprocessing.pipeline import PreprocessingAudit, preprocess_factor


@dataclass(frozen=True)
class FactorBlockResult:
    factor_id: str
    output_start: date
    output_end: date
    observations: pd.DataFrame
    preprocessing_audit: PreprocessingAudit
    diagnostics: dict[str, Any]


INPUT_FINGERPRINT_METHOD = "BROAD_FACTOR_INPUT_V3_EXACT_WARMUP_XNYS"


def _calendar() -> Any:
    import exchange_calendars as xcals

    return xcals.get_calendar("XNYS")


def _sessions(start: str | date | pd.Timestamp, end: str | date | pd.Timestamp) -> pd.DatetimeIndex:
    values = _calendar().sessions_in_range(start, end)
    if values.tz is not None:
        values = values.tz_localize(None)
    return pd.DatetimeIndex(values).normalize()


def factor_history_sessions(factor_id: str) -> int:
    """Return prior sessions needed to reproduce the registered formula."""
    factor = get_factor(factor_id)
    if hasattr(factor, "lookback"):
        return int(getattr(factor, "lookback")) + int(getattr(factor, "skip", 0))
    if hasattr(factor, "window"):
        # Return-based rolling factors need one prior price to create the first
        # return.  Loading ``window`` prior sessions plus t is sufficient.
        return int(getattr(factor, "window"))
    raise DataFoundationError(
        f"[{factor_id}] broad factor pipeline has no warm-up rule"
    )


def factor_input_columns(factor_id: str) -> tuple[str, ...]:
    factor = get_factor(factor_id)
    inputs = set(factor.inputs)
    if "returns" in inputs:
        inputs.remove("returns")
        inputs.add("adj_close")
    return tuple(sorted(inputs))


def output_months(
    start: str | date | pd.Timestamp,
    end: str | date | pd.Timestamp,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    sessions = _sessions(start, end)
    if sessions.empty:
        return []
    values: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    frame = pd.DataFrame({"date": sessions})
    frame["period"] = frame["date"].dt.to_period("M")
    for _, rows in frame.groupby("period", sort=True):
        values.append(
            (
                pd.Timestamp(rows["date"].min()).normalize(),
                pd.Timestamp(rows["date"].max()).normalize(),
            )
        )
    return values


def _lookback_start(first_output: pd.Timestamp, prior_sessions: int) -> pd.Timestamp:
    """Return the first session after loading exactly ``prior_sessions`` before t.

    ``exchange_calendars.sessions_window`` includes the anchor session.  Asking
    it for ``-N`` therefore returns only ``N - 1`` sessions before the output
    date, which is one observation short for exact momentum and return windows.
    """
    calendar = _calendar()
    first = calendar.date_to_session(first_output, direction="none")
    values = calendar.sessions_window(first, -(int(prior_sessions) + 1))
    start = values[0]
    if getattr(start, "tzinfo", None) is not None:
        start = start.tz_localize(None)
    return pd.Timestamp(start).normalize()


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _stable_frame_hash(frame: pd.DataFrame, columns: list[str]) -> str:
    work = frame.reindex(columns=columns).copy()
    for column in work.columns:
        if pd.api.types.is_datetime64_any_dtype(work[column]):
            work[column] = pd.to_datetime(work[column], errors="coerce").dt.strftime(
                "%Y-%m-%d"
            )
        elif work[column].dtype == "object":
            work[column] = work[column].fillna("").astype(str)
    payload = work.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def factor_input_fingerprint(
    *,
    factor_id: str,
    parent_version: DatasetVersion,
    membership: pd.DataFrame,
    classifications: pd.DataFrame,
    output_start: str | date | pd.Timestamp,
    output_end: str | date | pd.Timestamp,
) -> tuple[str, dict[str, Any]]:
    """Fingerprint every immutable input that can change one output month."""
    factor = get_factor(factor_id)
    block_start = pd.Timestamp(output_start).normalize()
    block_end = pd.Timestamp(output_end).normalize()
    history_start = _lookback_start(
        block_start, factor_history_sessions(factor_id)
    )
    index_path = _resolve_path(parent_version.bars_path)
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataFoundationError(
            "factor input fingerprint requires a readable coverage partition index"
        ) from exc
    if index.get("storage_type") != "PARTITIONED_PARQUET_V1":
        raise DataFoundationError(
            "factor input fingerprint requires partitioned broad coverage"
        )
    coverage_parts = [
        {
            "sha256": str(entry["sha256"]),
            "rows": int(entry["rows"]),
            "min_date": str(entry["min_date"]),
            "max_date": str(entry["max_date"]),
        }
        for entry in index.get("partitions") or []
        if pd.Timestamp(entry["max_date"]) >= history_start
        and pd.Timestamp(entry["min_date"]) <= block_end
    ]
    coverage_parts.sort(
        key=lambda value: (
            value["min_date"], value["max_date"], value["sha256"]
        )
    )
    if not coverage_parts:
        raise DataFoundationError(f"[{factor_id}] no coverage input partitions")

    snapshots = membership.copy()
    snapshots["date"] = pd.to_datetime(
        snapshots["date"], errors="coerce"
    ).dt.normalize()
    complete_dates = complete_snapshot_dates(snapshots)
    before = complete_dates[complete_dates <= block_start]
    if not len(before):
        raise DataFoundationError(
            f"[{factor_id}] no PIT baseline exists before {block_start.date()}"
        )
    baseline = pd.Timestamp(before.max()).normalize()
    relevant_membership = snapshots.loc[
        snapshots["date"].between(baseline, block_end)
    ].copy()
    membership_columns = [
        column
        for column in (
            "date", "security_id", "ticker", "active", "snapshot_type"
        )
        if column in relevant_membership.columns
    ]
    relevant_membership = relevant_membership.sort_values(
        ["date", "security_id"]
    )
    membership_sha = _stable_frame_hash(
        relevant_membership, membership_columns
    )

    member_ids = set(relevant_membership["security_id"].astype(str))
    classified = classifications.copy()
    if not classified.empty:
        classified["security_id"] = classified["security_id"].astype(str)
        sort_columns = [
            column
            for column in ("knowledge_date", "effective_from", "source_asof")
            if column in classified.columns
        ]
        if sort_columns:
            classified = classified.sort_values(sort_columns)
        classified = classified.loc[
            classified["security_id"].isin(member_ids)
        ].drop_duplicates("security_id", keep="last")
    classification_columns = [
        column
        for column in ("security_id", "sector", "classification_policy")
        if column in classified.columns
    ]
    classified = classified.sort_values("security_id") if not classified.empty else classified
    classification_sha = _stable_frame_hash(
        classified, classification_columns
    )
    proof = {
        "method": INPUT_FINGERPRINT_METHOD,
        "factor_id": factor_id,
        "factor_module": factor.__class__.__module__,
        "factor_class": factor.__class__.__qualname__,
        "factor_parameters": dict(vars(factor)),
        "factor_direction": int(factor.direction),
        "factor_inputs": list(factor.inputs),
        "output_start": block_start.date().isoformat(),
        "output_end": block_end.date().isoformat(),
        "history_start": history_start.date().isoformat(),
        "coverage_partitions": coverage_parts,
        "membership_baseline": baseline.date().isoformat(),
        "membership_sha256": membership_sha,
        "classification_sha256": classification_sha,
        "preprocessing": dict(CONFIG.preprocessing),
        "calendar_policy": "XNYS_ONLY",
    }
    digest = hashlib.sha256(
        json.dumps(
            proof, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    ).hexdigest()
    return digest, proof


def _normalize_bars(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"date", "security_id", "ticker", "adj_close", "volume"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise DataFoundationError(f"coverage bars are missing factor fields: {missing}")
    # The reader returns a private frame for this bounded calculation.  Mutate
    # it in place so replacing repeated strings actually frees the old arrays;
    # even a shallow copy kept both representations alive on 2 GB hosts.
    out = frame
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    out["security_id"] = out["security_id"].fillna("").astype(str).str.strip()
    out["ticker"] = (
        out["ticker"].fillna("").astype(str).str.strip().str.upper()
        .str.replace(".", "-", regex=False)
    )
    if out["date"].isna().any() or out["security_id"].eq("").any():
        raise DataFoundationError("coverage bars contain invalid factor identities")
    observed_dates = pd.DatetimeIndex(out["date"].unique()).normalize()
    valid_sessions = (
        _sessions(observed_dates.min(), observed_dates.max())
        if not observed_dates.empty
        else pd.DatetimeIndex([])
    )
    off_session = observed_dates.difference(valid_sessions)
    if not off_session.empty:
        sample = [value.date().isoformat() for value in off_session[:10]]
        raise DataFoundationError(
            "coverage factor input contains non-XNYS sessions: " + ", ".join(sample)
        )
    duplicate_count = int(out.duplicated(["date", "security_id"]).sum())
    if duplicate_count:
        raise DataFoundationError(
            f"coverage factor block has {duplicate_count} duplicate date/security rows"
        )
    for column in ("adj_close", "volume"):
        out[column] = pd.to_numeric(out[column], errors="coerce").astype(float)
        out[column] = out[column].where(np.isfinite(out[column]))
    out = out.sort_values(["date", "security_id"]).reset_index(drop=True)
    # These values repeat millions of times in a long momentum window.  Their
    # categorical representation is lossless and materially lowers peak RSS.
    out["security_id"] = out["security_id"].astype("category")
    out["ticker"] = out["ticker"].astype("category")
    return out


def _metadata_maps(
    master: pd.DataFrame,
    classifications: pd.DataFrame,
) -> tuple[dict[str, str], pd.Series]:
    required_master = {"security_id", "current_ticker"}
    if not required_master.issubset(master.columns):
        raise DataFoundationError("Security Master is missing identity columns")
    current = master.copy()
    current["security_id"] = current["security_id"].astype(str)
    current["current_ticker"] = (
        current["current_ticker"].fillna("").astype(str).str.upper()
        .str.replace(".", "-", regex=False)
    )
    current_tickers = (
        current.drop_duplicates("security_id", keep="last")
        .set_index("security_id")["current_ticker"]
        .to_dict()
    )
    if classifications.empty or not {"security_id", "sector"}.issubset(
        classifications.columns
    ):
        sector = pd.Series(dtype="object", name="sector")
    else:
        classified = classifications.copy()
        classified["security_id"] = classified["security_id"].astype(str)
        sort_columns = [
            column
            for column in ("knowledge_date", "effective_from", "source_asof")
            if column in classified.columns
        ]
        if sort_columns:
            classified = classified.sort_values(sort_columns)
        sector = (
            classified.drop_duplicates("security_id", keep="last")
            .set_index("security_id")["sector"]
            .fillna(UNKNOWN_CLASSIFICATION)
            .astype(str)
            .replace("", UNKNOWN_CLASSIFICATION)
            .rename("sector")
        )
    return current_tickers, sector


def _membership_by_date(
    membership: pd.DataFrame,
    output_dates: pd.DatetimeIndex,
    security_ids: Iterable[str],
) -> tuple[pd.DataFrame, dict[pd.Timestamp, dict[str, str]]]:
    required = {"date", "security_id", "ticker", "active"}
    missing = sorted(required - set(membership.columns))
    if missing:
        raise DataFoundationError(f"PIT membership is missing columns: {missing}")
    snapshots = membership.copy()
    snapshots["date"] = pd.to_datetime(
        snapshots["date"], errors="coerce"
    ).dt.normalize()
    snapshots["security_id"] = snapshots["security_id"].astype(str)
    snapshots["ticker"] = (
        snapshots["ticker"].astype(str).str.upper()
        .str.replace(".", "-", regex=False)
    )
    snapshots["active"] = snapshots["active"].fillna(False).astype(bool)
    if snapshots.empty or snapshots["date"].isna().any():
        raise DataFoundationError("PIT membership has no valid rows")
    if snapshots.duplicated(["date", "security_id"]).any():
        raise DataFoundationError("PIT membership contains duplicate identities")

    snapshot_types = (
        snapshots["snapshot_type"].fillna("").astype(str).str.upper()
        if "snapshot_type" in snapshots.columns
        else pd.Series("", index=snapshots.index)
    )
    complete_rows = snapshots.loc[
        snapshots["active"] & snapshot_types.ne("FORCED_EXIT")
    ]
    baselines = complete_rows.loc[
        complete_rows["date"].le(output_dates.min()), "date"
    ]
    if baselines.empty:
        raise DataFoundationError("PIT membership has no baseline for factor dates")
    baseline = pd.Timestamp(baselines.max()).normalize()
    snapshots = snapshots.loc[
        snapshots["date"].between(baseline, output_dates.max())
    ].copy()

    columns = pd.Index(sorted({str(value) for value in security_ids}), name="security_id")
    mask = pd.DataFrame(False, index=output_dates, columns=columns, dtype=bool)
    ticker_maps: dict[pd.Timestamp, dict[str, str]] = {}
    for state in replay_membership_states(
        snapshots,
        output_dates,
        key_column="security_id",
        value_column="ticker",
    ):
        present = columns.intersection(state.active_keys)
        mask.loc[state.date, present] = True
        ticker_maps[state.date] = state.value_by_key
    return mask, ticker_maps


def _factor_wide(
    bars: pd.DataFrame,
    factor_id: str,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    factor = get_factor(factor_id)
    columns = pd.Index(sorted(bars["security_id"].unique()), name="security_id")
    wide: dict[str, pd.DataFrame] = {}
    for field in factor_input_columns(factor_id):
        matrix = bars.pivot(index="date", columns="security_id", values=field)
        wide[field] = matrix.sort_index().reindex(columns=columns)
    if "returns" in factor.inputs:
        wide["returns"] = wide["adj_close"].pct_change(fill_method=None)
    raw = factor.compute_from_wide(wide)
    raw.index = pd.DatetimeIndex(raw.index).normalize()
    raw.columns = columns
    return wide, raw


def _window_ready(
    factor_id: str,
    wide: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    factor = get_factor(factor_id)
    if hasattr(factor, "lookback"):
        prices = wide["adj_close"]
        requirement = int(getattr(factor, "lookback")) + int(
            getattr(factor, "skip", 0)
        ) + 1
        return prices.notna().cumsum().ge(requirement)
    if factor.inputs == ("volume",):
        requirement = int(getattr(factor, "window"))
        return wide["volume"].notna().cumsum().ge(requirement)
    if factor.inputs == ("returns",):
        window = int(getattr(factor, "window"))
        minimum_returns = (
            int(window * 0.8)
            if factor_id.startswith("VOL_")
            else window
        )
        return wide["adj_close"].notna().cumsum().ge(minimum_returns + 1)
    raise DataFoundationError(f"[{factor_id}] unsupported broad warm-up semantics")


def compute_factor_block(
    *,
    factor_id: str,
    bars: pd.DataFrame,
    membership: pd.DataFrame,
    master: pd.DataFrame,
    classifications: pd.DataFrame,
    output_dates: Iterable[str | date | pd.Timestamp],
) -> FactorBlockResult:
    """Compute one factor for a bounded set of output sessions."""
    factor = get_factor(factor_id)
    if int(factor.direction) not in {-1, 1}:
        raise DataFoundationError(f"[{factor_id}] has no fixed research direction")
    normalized_bars = _normalize_bars(bars)
    dates = pd.DatetimeIndex(pd.to_datetime(list(output_dates))).normalize().unique()
    dates = pd.DatetimeIndex(sorted(dates))
    if dates.empty:
        raise DataFoundationError(f"[{factor_id}] output block has no sessions")
    if normalized_bars["date"].max() < dates.max():
        raise DataFoundationError(f"[{factor_id}] coverage bars do not reach output end")

    wide, full_raw = _factor_wide(normalized_bars, factor_id)
    membership_security_ids = set(membership["security_id"].astype(str))
    security_ids = pd.Index(
        sorted(set(full_raw.columns.astype(str)) | membership_security_ids),
        name="security_id",
    )
    raw = full_raw.reindex(index=dates, columns=security_ids)
    ready = (
        _window_ready(factor_id, wide)
        .reindex(index=dates, columns=security_ids)
        .fillna(False)
    )
    membership_mask, membership_tickers = _membership_by_date(
        membership, dates, security_ids
    )
    member_raw = raw.where(membership_mask)
    current_tickers, sector_map = _metadata_maps(master, classifications)
    clean, preprocessing_audit = preprocess_factor(
        member_raw,
        sector_map=sector_map.reindex(security_ids),
        mcap_df=None,
        membership_mask=membership_mask,
        return_audit=True,
    )
    clean = clean.where(membership_mask)

    output_bars = normalized_bars.loc[normalized_bars["date"].isin(dates)]
    bars_by_date = {
        pd.Timestamp(observation_date).normalize(): rows
        for observation_date, rows in output_bars.groupby("date", sort=True)
    }
    unknown_sector = sector_map.reindex(security_ids).fillna(
        UNKNOWN_CLASSIFICATION
    ).astype(str).eq(UNKNOWN_CLASSIFICATION)
    rows: list[pd.DataFrame] = []
    for observation_date in dates:
        dated_bars = bars_by_date.get(pd.Timestamp(observation_date))
        bar_tickers = (
            dict(zip(dated_bars["security_id"], dated_bars["ticker"]))
            if dated_bars is not None
            else {}
        )
        member_ids = set(
            membership_mask.columns[membership_mask.loc[observation_date]]
        )
        observed_ids = set(bar_tickers)
        selected_ids = sorted(member_ids | observed_ids)
        if not selected_ids:
            continue
        raw_values = raw.loc[observation_date].reindex(selected_ids)
        clean_values = clean.loc[observation_date].reindex(selected_ids)
        members = membership_mask.loc[observation_date].reindex(
            selected_ids, fill_value=False
        )
        ready_values = ready.loc[observation_date].reindex(
            selected_ids, fill_value=False
        )
        missing_classification = unknown_sector.reindex(
            selected_ids, fill_value=True
        )
        status = pd.Series("NOT_PIT_MEMBER", index=selected_ids, dtype="object")
        status.loc[members & raw_values.isna() & ~ready_values] = (
            "CALCULATION_WINDOW_INSUFFICIENT"
        )
        status.loc[members & raw_values.isna() & ready_values] = "RAW_MISSING"
        status.loc[
            members & raw_values.notna() & clean_values.isna()
        ] = "CLEAN_MISSING"
        valid = members & raw_values.notna() & clean_values.notna()
        status.loc[valid] = "VALID"
        status.loc[valid & missing_classification] = "CLASSIFICATION_MISSING"
        member_ticker_map = membership_tickers.get(
            pd.Timestamp(observation_date), {}
        )
        ticker_values = [
            bar_tickers.get(security_id)
            or member_ticker_map.get(security_id)
            or current_tickers.get(security_id)
            or ""
            for security_id in selected_ids
        ]
        if any(not value for value in ticker_values):
            missing = [
                security_id
                for security_id, ticker in zip(selected_ids, ticker_values)
                if not ticker
            ]
            raise DataFoundationError(
                f"[{factor_id}] missing ticker metadata for {missing[:20]}"
            )
        rows.append(pd.DataFrame({
            "date": observation_date,
            "security_id": selected_ids,
            "ticker": ticker_values,
            "factor_id": factor_id,
            "raw_value": raw_values.to_numpy(dtype=float, na_value=np.nan),
            "clean_value": clean_values.to_numpy(dtype=float, na_value=np.nan),
            "pit_member": members.to_numpy(dtype=bool),
            "status": status.to_numpy(),
        }))
    observations = (
        pd.concat(rows, ignore_index=True)
        if rows
        else pd.DataFrame(columns=[
            "date", "security_id", "ticker", "factor_id", "raw_value",
            "clean_value", "pit_member", "status",
        ])
    )
    latest = observations.loc[
        observations["date"].eq(pd.Timestamp(dates.max()))
    ]
    latest_ready_ids = set(
        security_ids[
            membership_mask.loc[dates.max()]
            & ready.loc[dates.max()]
        ]
    )
    latest_members = latest.loc[
        latest["pit_member"]
        & latest["security_id"].isin(latest_ready_ids)
    ]
    denominator = len(latest_members)
    latest_raw_coverage = (
        float(latest_members["raw_value"].notna().mean()) if denominator else 0.0
    )
    latest_clean_coverage = (
        float(latest_members["clean_value"].notna().mean()) if denominator else 0.0
    )
    zero_std_days = 0
    eligible_days = 0
    for _, daily in observations.loc[
        observations["pit_member"] & observations["clean_value"].notna()
    ].groupby("date"):
        if len(daily) < 2:
            continue
        eligible_days += 1
        zero_std_days += int(float(daily["clean_value"].std()) == 0.0)
    diagnostics = {
        "factor_id": factor_id,
        "direction": int(factor.direction),
        "output_start": dates.min().date().isoformat(),
        "output_end": dates.max().date().isoformat(),
        "output_sessions": len(dates),
        "observation_rows": len(observations),
        "latest_member_count": int(latest["pit_member"].sum()),
        "latest_warmup_eligible_count": denominator,
        "latest_raw_coverage": latest_raw_coverage,
        "latest_clean_coverage": latest_clean_coverage,
        "zero_std_cross_sections": zero_std_days,
        "eligible_cross_sections": eligible_days,
        "zero_std_cross_section_ratio": (
            zero_std_days / eligible_days if eligible_days else 1.0
        ),
        "status_counts": {
            str(key): int(value)
            for key, value in observations["status"].value_counts().items()
        },
    }
    return FactorBlockResult(
        factor_id=factor_id,
        output_start=dates.min().date(),
        output_end=dates.max().date(),
        observations=observations,
        preprocessing_audit=preprocessing_audit,
        diagnostics=diagnostics,
    )


class BroadFactorCalculator:
    """Load only one factor/month window from an authenticated coverage version."""

    def __init__(
        self,
        *,
        coverage_reader: BroadCoverageReader,
        parent_version: DatasetVersion,
        membership: pd.DataFrame,
        master: pd.DataFrame,
        classifications: pd.DataFrame,
    ):
        self.coverage_reader = coverage_reader
        self.parent_version = parent_version
        self.membership = membership
        self.master = master
        self.classifications = classifications

    def compute_month(
        self,
        factor_id: str,
        *,
        output_start: str | date | pd.Timestamp,
        output_end: str | date | pd.Timestamp,
    ) -> FactorBlockResult:
        dates = _sessions(output_start, output_end)
        if dates.empty:
            raise DataFoundationError(f"[{factor_id}] output month has no XNYS sessions")
        history_start = _lookback_start(
            pd.Timestamp(dates.min()), factor_history_sessions(factor_id)
        )
        columns = [
            "date", "security_id", "ticker", *factor_input_columns(factor_id)
        ]
        bars = self.coverage_reader.load_bars(
            start=history_start,
            end=dates.max(),
            version=self.parent_version,
            columns=columns,
            ordered=False,
        )
        # The pure calculator expects both fields so its schema remains stable.
        for column in ("adj_close", "volume"):
            if column not in bars.columns:
                bars[column] = np.nan
        return compute_factor_block(
            factor_id=factor_id,
            bars=bars,
            membership=self.membership,
            master=self.master,
            classifications=self.classifications,
            output_dates=dates,
        )


__all__ = [
    "BroadFactorCalculator",
    "FactorBlockResult",
    "compute_factor_block",
    "factor_history_sessions",
    "factor_input_fingerprint",
    "factor_input_columns",
    "INPUT_FINGERPRINT_METHOD",
    "output_months",
]
