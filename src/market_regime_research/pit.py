"""
Reconstruct complete S&P 500 point-in-time snapshots from FMP change events.

FMP exposes additions/removals, not snapshots.  Reconstruction starts from a
current constituent set and walks events backward.  Every inconsistency is
recorded; strict mode refuses publication because an undocumented removal or
symbol transition creates an interval whose true membership is unknowable.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

import pandas as pd

from src.market_regime_research import ALGORITHM_VERSION, SCHEMA_VERSION
from src.market_regime_research.artifacts import file_sha256, write_strict_json
from src.market_regime_research.models import (
    DataContractError,
    PITReconstructionResult,
    PointInTimeReconstructionError,
)
from src.market_regime_research.settings import PITSettings
from src.utils.identifiers import InvalidResourceId, canonical_ticker


def _text(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip()


def _pit_ticker(value: Any) -> str:
    """Normalize FMP's dotted symbols to the project's dash convention."""
    return canonical_ticker(str(value).strip().upper().replace(".", "-"))


def _reason_code_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DataContractError(
                "Normalized PIT reason_codes contains invalid JSON"
            ) from exc
        value = decoded
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if hasattr(value, "tolist"):
        converted = value.tolist()
        if isinstance(converted, list):
            return [str(item) for item in converted]
    raise DataContractError("Normalized PIT reason_codes must be a list")


def normalize_fmp_sp500_changes(payload: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize FMP's mixed replacement/addition/removal row shapes.

    ``symbol`` is an addition only when ``addedSecurity`` is populated.  On
    removal-only rows FMP repeats the removed symbol in ``symbol``; treating it
    as an addition would silently corrupt every older snapshot.
    """
    if payload is None or payload.empty:
        raise DataContractError("FMP historical S&P 500 change payload is empty")
    required = {
        "date",
        "symbol",
        "addedSecurity",
        "removedTicker",
        "removedSecurity",
    }
    missing = required - set(payload.columns)
    if missing:
        raise DataContractError(
            f"FMP historical constituent changes missing fields: {sorted(missing)}"
        )

    source = payload.copy().reset_index(drop=True)
    dates = pd.to_datetime(source["date"], errors="coerce", utc=True)
    if dates.isna().any():
        raise DataContractError("FMP constituent changes contain invalid dates")
    source["effective_date"] = dates.dt.tz_convert(None).dt.normalize()

    added_names = _text(source["addedSecurity"])
    removed_names = _text(source["removedSecurity"])
    symbols = _text(source["symbol"])
    removed_symbols = _text(source["removedTicker"])

    rows: list[dict[str, Any]] = []
    for position, row in source.iterrows():
        added_name = added_names.iloc[position]
        removed_name = removed_names.iloc[position]
        symbol = symbols.iloc[position]
        removed_symbol = removed_symbols.iloc[position]
        reasons: list[str] = []
        warnings: list[str] = []

        added_ticker = ""
        if added_name:
            if not symbol:
                reasons.append("ADDITION_NAME_WITHOUT_SYMBOL")
            else:
                try:
                    added_ticker = _pit_ticker(symbol)
                except InvalidResourceId:
                    reasons.append("INVALID_ADDED_TICKER")

        removal_ticker = ""
        if removed_symbol:
            try:
                removal_ticker = _pit_ticker(removed_symbol)
            except InvalidResourceId:
                reasons.append("INVALID_REMOVED_TICKER")
        elif removed_name:
            # FMP normally supplies removedTicker.  Inferring from ``symbol`` is
            # unsafe because that field is also the addition on paired rows.
            reasons.append("REMOVAL_NAME_WITHOUT_TICKER")

        if not added_ticker and symbol and removal_ticker:
            try:
                possible_addition = _pit_ticker(symbol)
            except InvalidResourceId:
                reasons.append("INVALID_EVENT_SYMBOL")
            else:
                if possible_addition != removal_ticker:
                    # A handful of FMP rows omit addedSecurity while still
                    # carrying a clear replacement pair (symbol != removed).
                    # Preserve the candidate state and keep a warning for
                    # provenance.  The event remains deterministic because the
                    # added and removed symbols are distinct.
                    added_ticker = possible_addition
                    warnings.append("ADDITION_INFERRED_WITHOUT_SECURITY_NAME")

        if not added_ticker and not removal_ticker:
            reasons.append("UNCLASSIFIED_EVENT")
        if added_ticker and removal_ticker and added_ticker == removal_ticker:
            reasons.append("SAME_TICKER_ADDED_AND_REMOVED")

        rows.append(
            {
                "effective_date": row["effective_date"],
                "added_ticker": added_ticker or None,
                "removed_ticker": removal_ticker or None,
                "added_security": added_name or None,
                "removed_security": removed_name or None,
                "reason": str(row.get("reason") or "").strip() or None,
                "source_row": int(position),
                "quality_status": (
                    "ERROR" if reasons else "WARNING" if warnings else "OK"
                ),
                "reason_codes": reasons + warnings,
            }
        )

    output = pd.DataFrame(rows).sort_values(
        ["effective_date", "source_row"],
        ascending=[False, True],
    )
    return output.reset_index(drop=True)


def _normalize_current_constituents(current: pd.DataFrame | pd.Series | list[str]) -> set[str]:
    if isinstance(current, pd.DataFrame):
        if "ticker" not in current.columns:
            raise DataContractError("Current constituents must contain ticker")
        raw = current["ticker"].tolist()
    elif isinstance(current, pd.Series):
        raw = current.tolist()
    else:
        raw = list(current)
    try:
        normalized = {_pit_ticker(value) for value in raw if str(value).strip()}
    except InvalidResourceId as exc:
        raise DataContractError("Current constituents contain an invalid ticker") from exc
    if not normalized:
        raise DataContractError("Current constituent set is empty")
    return normalized


def reconstruct_sp500_snapshots(
    current_constituents: pd.DataFrame | pd.Series | list[str],
    changes: pd.DataFrame,
    *,
    asof: str | pd.Timestamp,
    start: str | pd.Timestamp,
    min_snapshot_members: int = 450,
    max_snapshot_members: int = 550,
    strict: bool = True,
) -> PITReconstructionResult:
    """
    Walk normalized events backward and emit complete active-member snapshots.

    Snapshot dates have effective-close semantics: the row set on an event date
    is the membership *after* that event.  This matches ``src.data.pit``, which
    uses the latest snapshot on or before a research date.
    """
    normalized = (
        normalize_fmp_sp500_changes(changes)
        if "effective_date" not in changes.columns
        else changes.copy()
    )
    required = {
        "effective_date",
        "added_ticker",
        "removed_ticker",
        "quality_status",
        "reason_codes",
        "source_row",
    }
    missing = required - set(normalized.columns)
    if missing:
        raise DataContractError(
            f"Normalized PIT events missing fields: {sorted(missing)}"
        )

    asof_date = pd.Timestamp(asof).tz_localize(None).normalize()
    start_date = pd.Timestamp(start).tz_localize(None).normalize()
    if pd.isna(asof_date) or pd.isna(start_date) or start_date > asof_date:
        raise ValueError("PIT start/asof must define a valid date range")

    normalized["effective_date"] = pd.to_datetime(
        normalized["effective_date"],
        errors="coerce",
    ).dt.tz_localize(None).dt.normalize()
    if normalized["effective_date"].isna().any():
        raise DataContractError("Normalized PIT events contain invalid dates")
    allowed_quality = {"OK", "WARNING", "ERROR"}
    if not set(normalized["quality_status"].astype(str)).issubset(allowed_quality):
        raise DataContractError("Normalized PIT events contain invalid quality_status")
    normalized["reason_codes"] = normalized["reason_codes"].map(_reason_code_list)
    normalized["source_row"] = pd.to_numeric(
        normalized["source_row"],
        errors="coerce",
    )
    if normalized["source_row"].isna().any():
        raise DataContractError("Normalized PIT events contain invalid source_row")
    normalized["source_row"] = normalized["source_row"].astype(int)
    future_events = normalized[normalized["effective_date"] > asof_date]
    events = normalized[
        normalized["effective_date"].between(start_date, asof_date)
    ].copy()
    events = events.sort_values(
        ["effective_date", "source_row"],
        ascending=[False, True],
    )

    state = _normalize_current_constituents(current_constituents)
    snapshots: dict[pd.Timestamp, set[str]] = {asof_date: set(state)}
    inconsistencies: list[dict[str, Any]] = []
    if not future_events.empty:
        inconsistencies.append(
            {
                "effective_date": None,
                "type": "EVENTS_AFTER_SNAPSHOT_ASOF",
                "tickers": [],
                "reason_codes": ["CURRENT_SET_EFFECTIVE_DATE_IS_AMBIGUOUS"],
                "samples": [
                    pd.Timestamp(value).date().isoformat()
                    for value in future_events["effective_date"].drop_duplicates().head(20)
                ],
            }
        )

    source_errors = events[events["quality_status"] == "ERROR"]
    source_warnings = events[events["quality_status"] == "WARNING"]
    for row in source_errors.itertuples(index=False):
        inconsistencies.append(
            {
                "effective_date": pd.Timestamp(row.effective_date).date().isoformat(),
                "type": "SOURCE_EVENT_UNRESOLVED",
                "tickers": [],
                "reason_codes": list(row.reason_codes),
            }
        )

    for effective_date, group in events.groupby("effective_date", sort=False):
        date = pd.Timestamp(effective_date).normalize()
        additions = {
            str(value)
            for value in group["added_ticker"].dropna()
            if str(value).strip()
        }
        removals = {
            str(value)
            for value in group["removed_ticker"].dropna()
            if str(value).strip()
        }
        same_day_overlap = sorted(additions & removals)
        if same_day_overlap:
            inconsistencies.append(
                {
                    "effective_date": date.date().isoformat(),
                    "type": "SAME_TICKER_ADDED_AND_REMOVED_ON_DATE",
                    "tickers": same_day_overlap,
                    "reason_codes": ["AGGREGATE_EVENT_IDENTITY_AMBIGUOUS"],
                }
            )
        missing_additions = sorted(additions - state)
        still_active_removals = sorted(removals & state)
        if missing_additions:
            inconsistencies.append(
                {
                    "effective_date": date.date().isoformat(),
                    "type": "ADDITION_ABSENT_FROM_LATER_STATE",
                    "tickers": missing_additions,
                    "reason_codes": ["UNDOCUMENTED_LATER_REMOVAL_OR_SYMBOL_CHANGE"],
                }
            )
        if still_active_removals:
            inconsistencies.append(
                {
                    "effective_date": date.date().isoformat(),
                    "type": "REMOVAL_PRESENT_IN_LATER_STATE",
                    "tickers": still_active_removals,
                    "reason_codes": ["UNDOCUMENTED_LATER_ADDITION_OR_SYMBOL_REUSE"],
                }
            )

        # Enforce the event on its own effective date for a useful diagnostic
        # candidate.  Strict mode still refuses to publish if state repair was
        # needed because the unknown interval cannot be recovered.
        post_event_state = (state | additions) - removals
        snapshots[date] = post_event_state
        state = (post_event_state - additions) | removals

    if start_date not in snapshots:
        snapshots[start_date] = set(state)

    sizes = {date: len(members) for date, members in snapshots.items()}
    size_violations = [
        {
            "effective_date": date.date().isoformat(),
            "members": size,
        }
        for date, size in sorted(sizes.items())
        if size < int(min_snapshot_members) or size > int(max_snapshot_members)
    ]
    if size_violations:
        inconsistencies.append(
            {
                "effective_date": None,
                "type": "SNAPSHOT_SIZE_OUT_OF_RANGE",
                "tickers": [],
                "reason_codes": ["MEMBERSHIP_COUNT_GATE_FAILED"],
                "samples": size_violations[:20],
            }
        )

    records = [
        {"date": date, "ticker": ticker, "active": True}
        for date, members in sorted(snapshots.items())
        for ticker in sorted(members)
    ]
    membership = pd.DataFrame(records)
    if membership.empty or membership.duplicated(["date", "ticker"]).any():
        raise DataContractError("Reconstructed PIT membership is empty or duplicated")

    diagnostics = {
        "quality_status": "PASS" if not inconsistencies else "FAIL",
        "strict": bool(strict),
        "asof": asof_date.date().isoformat(),
        "start": start_date.date().isoformat(),
        "current_members": len(_normalize_current_constituents(current_constituents)),
        "snapshots": len(snapshots),
        "first_snapshot": min(snapshots).date().isoformat(),
        "last_snapshot": max(snapshots).date().isoformat(),
        "minimum_members": min(sizes.values()),
        "maximum_members": max(sizes.values()),
        "events_in_range": len(events),
        "future_events_ignored": len(future_events),
        "source_warning_events": len(source_warnings),
        "source_warning_samples": [
            {
                "effective_date": pd.Timestamp(row.effective_date).date().isoformat(),
                "reason_codes": list(row.reason_codes),
            }
            for row in source_warnings.head(20).itertuples(index=False)
        ],
        "inconsistency_count": len(inconsistencies),
        "inconsistencies": inconsistencies,
    }
    result = PITReconstructionResult(
        membership=membership.sort_values(["date", "ticker"]).reset_index(drop=True),
        normalized_events=normalized.reset_index(drop=True),
        diagnostics=diagnostics,
    )
    if strict and inconsistencies:
        sample = [
            f"{item['effective_date']}:{item['type']}"
            for item in inconsistencies[:10]
        ]
        raise PointInTimeReconstructionError(
            "FMP change history cannot produce a clean PIT universe; "
            f"{len(inconsistencies)} inconsistency groups found. Sample: {sample}"
        )
    return result


def reconstruct_with_settings(
    current_constituents: pd.DataFrame | pd.Series | list[str],
    changes: pd.DataFrame,
    *,
    asof: str | pd.Timestamp,
    settings: PITSettings,
    strict: bool | None = None,
) -> PITReconstructionResult:
    return reconstruct_sp500_snapshots(
        current_constituents,
        changes,
        asof=asof,
        start=settings.start,
        min_snapshot_members=settings.min_snapshot_members,
        max_snapshot_members=settings.max_snapshot_members,
        strict=settings.strict if strict is None else bool(strict),
    )


def atomic_write_membership(frame: pd.DataFrame, path: Path) -> Path:
    """Atomically publish a validated membership parquet."""
    required = {"date", "ticker", "active"}
    if frame.empty or not required.issubset(frame.columns):
        raise DataContractError("Membership publication requires date/ticker/active")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp.parquet",
        dir=str(path.parent),
    )
    os.close(descriptor)
    try:
        frame.to_parquet(temporary_name, compression="snappy", index=False)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return path


def membership_metadata_path(path: Path) -> Path:
    """Return the sidecar required for a production PIT membership file."""
    return Path(path).with_suffix(".metadata.json")


def publish_validated_membership(
    result: PITReconstructionResult,
    path: Path,
    *,
    source_metadata: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    """Publish only a PASS reconstruction and bind it to auditable metadata."""
    if result.diagnostics.get("quality_status") != "PASS":
        raise PointInTimeReconstructionError(
            "Only a PIT reconstruction with quality_status=PASS can be published"
        )
    if result.diagnostics.get("strict") is not True:
        raise PointInTimeReconstructionError(
            "Production PIT publication requires a strict reconstruction"
        )
    membership_path = atomic_write_membership(result.membership, Path(path))
    metadata_path = membership_metadata_path(membership_path)
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "quality_status": "PASS",
        "strict": bool(result.diagnostics.get("strict")),
        "asof": result.diagnostics.get("asof"),
        "start": result.diagnostics.get("start"),
        "membership_sha256": file_sha256(membership_path),
        "diagnostics": result.diagnostics,
        "source": dict(source_metadata or {}),
    }
    write_strict_json(metadata_path, metadata)
    return membership_path, metadata_path


__all__ = [
    "atomic_write_membership",
    "membership_metadata_path",
    "normalize_fmp_sp500_changes",
    "publish_validated_membership",
    "reconstruct_sp500_snapshots",
    "reconstruct_with_settings",
]
