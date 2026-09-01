"""Partitioned market-data coverage for broad US-equity research.

Coverage answers whether the system can compute formula-level values for a
security.  It deliberately contains no comparison-universe membership.  Large
history is stored as authenticated Parquet partitions behind one immutable
index registered as a normal ``DatasetVersion``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

import numpy as np
import pandas as pd
import exchange_calendars as xcals

from src.config import PROJECT_ROOT
from src.data.foundation import (
    DataFoundationError,
    DatasetVersion,
    MarketDataCatalog,
    MarketDataReader,
    QualityCheck,
)
from src.data.price_semantics import (
    FMP_CANONICAL_SOURCE,
    build_price_semantics_contract,
    validate_price_semantics_contract,
)
from src.data.security_master_store import SecurityMasterGeneration
from src.data.research_history_policy import (
    EXCLUDED_UNVERIFIABLE_HISTORY,
    FULL_HISTORY,
    PROSPECTIVE_ONLY,
    empty_history_policy,
)
from src.data.universe_ids import US_EQUITY_COVERAGE
from src.utils.file_lock import file_lock


PARTITION_INDEX_SCHEMA_VERSION = 1
PARTITION_STORAGE_TYPE = "PARTITIONED_PARQUET_V1"
COVERAGE_PARTITION_FREQUENCY = "MONTH"
COVERAGE_BAR_COLUMNS = [
    "date",
    "security_id",
    "ticker",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
    "source",
    "source_asof",
    "ingestion_run_id",
]
BAR_QUARANTINE_COLUMNS = [
    *COVERAGE_BAR_COLUMNS,
    "quality_reasons",
]


def _xnys_sessions_between(
    start: str | date | pd.Timestamp,
    end: str | date | pd.Timestamp,
) -> pd.DatetimeIndex:
    sessions = xcals.get_calendar("XNYS").sessions_in_range(start, end)
    if sessions.tz is not None:
        sessions = sessions.tz_localize(None)
    return pd.DatetimeIndex(sessions).normalize()


@dataclass(frozen=True)
class CoveragePublication:
    version: DatasetVersion
    partition_count: int
    security_master_generation_id: str
    checks: tuple[QualityCheck, ...]
    statistics: dict[str, Any]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _portable_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(resolved)


def normalize_coverage_bars(
    frame: pd.DataFrame,
    *,
    target_session: str | pd.Timestamp,
    ingestion_run_id: str,
    source: str = "FMP",
) -> pd.DataFrame:
    """Normalize one bounded security batch without widening it in memory."""
    required = {
        "date", "security_id", "ticker", "open", "high", "low", "close",
        "adj_close", "volume",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise DataFoundationError(f"coverage bars missing columns: {missing}")
    target = pd.Timestamp(target_session).normalize()
    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    out["security_id"] = out["security_id"].fillna("").astype(str).str.strip()
    out["ticker"] = (
        out["ticker"].fillna("").astype(str).str.strip().str.upper()
        .str.replace(".", "-", regex=False)
        .str.replace("/", "-", regex=False)
    )
    for column in ("open", "high", "low", "close", "adj_close", "volume"):
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out["source"] = str(source).upper()
    out["source_asof"] = target
    out["ingestion_run_id"] = str(ingestion_run_id)
    out = out.loc[out["date"].le(target), COVERAGE_BAR_COLUMNS]
    if out["date"].isna().any() or out["security_id"].eq("").any():
        raise DataFoundationError("coverage bars contain invalid dates or identities")
    duplicate_count = int(out.duplicated(["date", "security_id"]).sum())
    if duplicate_count:
        raise DataFoundationError(
            f"coverage batch has {duplicate_count} duplicate date/security rows"
        )
    return out.sort_values(["date", "security_id"]).reset_index(drop=True)


def split_coverage_bar_quality(
    frame: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Separate provider-invalid rows without repairing or hiding raw values.

    The accepted frame keeps the production OHLCV schema.  Rejected rows retain
    every provider value plus deterministic reason codes so a later provider or
    policy review can reproduce exactly why a row did not enter research data.
    """
    missing = sorted(set(COVERAGE_BAR_COLUMNS) - set(frame.columns))
    if missing:
        raise DataFoundationError(
            f"coverage quality split missing columns: {missing}"
        )
    work = frame.loc[:, COVERAGE_BAR_COLUMNS].copy()
    numeric_columns = ["open", "high", "low", "close", "adj_close", "volume"]
    numeric = work[numeric_columns]
    null_required = numeric.isna().any(axis=1)
    nonfinite = pd.Series(
        ~np.isfinite(numeric.to_numpy(dtype=float)).all(axis=1),
        index=work.index,
    ) & ~null_required
    nonpositive_price = work[
        ["open", "high", "low", "close", "adj_close"]
    ].le(0).any(axis=1)
    negative_volume = work["volume"].lt(0)
    invalid_ohlc = (
        work["high"].lt(work[["open", "close", "low"]].max(axis=1))
        | work["low"].gt(work[["open", "close", "high"]].min(axis=1))
    )
    dates = pd.DatetimeIndex(work["date"].dropna().unique()).normalize()
    valid_sessions = (
        _xnys_sessions_between(dates.min(), dates.max())
        if not dates.empty
        else pd.DatetimeIndex([])
    )
    non_xnys_session = ~work["date"].isin(valid_sessions)
    reason_masks = (
        ("NON_XNYS_SESSION", non_xnys_session),
        ("NULL_REQUIRED_VALUE", null_required),
        ("NONFINITE_VALUE", nonfinite),
        ("NONPOSITIVE_PRICE", nonpositive_price),
        ("NEGATIVE_VOLUME", negative_volume),
        ("INVALID_OHLC_BOUNDS", invalid_ohlc),
    )
    reasons = pd.Series("", index=work.index, dtype="object")
    for reason, mask in reason_masks:
        reasons.loc[mask] = np.where(
            reasons.loc[mask].eq(""),
            reason,
            reasons.loc[mask] + "," + reason,
        )
    rejected = reasons.ne("")
    accepted = work.loc[~rejected].reset_index(drop=True)
    quarantine = work.loc[rejected].copy()
    quarantine["quality_reasons"] = reasons.loc[rejected]
    quarantine = quarantine.loc[:, BAR_QUARANTINE_COLUMNS].reset_index(drop=True)
    if len(accepted) + len(quarantine) != len(work):
        raise DataFoundationError("coverage quality split lost source rows")
    return accepted, quarantine


def coverage_bar_quarantine_checks(
    quarantine: pd.DataFrame,
    *,
    source_row_count: int,
    security_universe: pd.DataFrame,
    target_session: str | pd.Timestamp,
    max_ratio: float,
    max_target_ratio: float,
) -> list[QualityCheck]:
    """Build fail-closed gates for an explicit provider-bar quarantine."""
    if source_row_count < len(quarantine):
        raise DataFoundationError("quarantine rows exceed source rows")
    target = pd.Timestamp(target_session).normalize()
    dates = pd.to_datetime(
        quarantine.get("date", pd.Series(dtype="datetime64[ns]")),
        errors="coerce",
    ).dt.normalize()
    current_ids = set(
        security_universe.loc[
            security_universe["is_current_coverage"].astype(bool), "security_id"
        ].astype(str)
    )
    target_quarantined_ids = set(
        quarantine.loc[dates.eq(target), "security_id"].astype(str)
    ) & current_ids
    ratio = len(quarantine) / source_row_count if source_row_count else 0.0
    target_ratio = (
        len(target_quarantined_ids) / len(current_ids) if current_ids else 0.0
    )
    reason_counts: dict[str, int] = {}
    if not quarantine.empty:
        reason_counts = {
            str(reason): int(count)
            for reason, count in (
                quarantine["quality_reasons"].astype(str).str.split(",").explode()
                .value_counts().sort_index().items()
            )
        }
    return [
        QualityCheck(
            "provider_bar_quarantine_ratio",
            ratio <= float(max_ratio),
            {
                "source_rows": int(source_row_count),
                "quarantined_rows": len(quarantine),
                "ratio": ratio,
                "reason_counts": reason_counts,
            },
            {"maximum_ratio": float(max_ratio)},
            f"provider bad-bar quarantine ratio {ratio:.4%}",
        ),
        QualityCheck(
            "target_session_bar_quarantine_ratio",
            bool(current_ids) and target_ratio <= float(max_target_ratio),
            {
                "current_securities": len(current_ids),
                "quarantined_current_securities": len(target_quarantined_ids),
                "ratio": target_ratio,
                "security_sample": sorted(target_quarantined_ids)[:20],
            },
            {"maximum_ratio": float(max_target_ratio)},
            f"target-session bad-bar quarantine ratio {target_ratio:.4%}",
        ),
    ]


def select_coverage_securities(
    master: pd.DataFrame,
    *,
    history_start: str | pd.Timestamp,
    target_session: str | pd.Timestamp,
    allowed_asset_types: Iterable[str] = ("STOCK", "ADR"),
    benchmark_tickers: Iterable[str] = ("SPY", "QQQ", "IWM"),
    history_policy: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Select securities to ingest without treating them as rank members."""
    required = {
        "security_id", "current_ticker", "name", "asset_type",
        "primary_exchange", "listing_date", "delisting_date", "trading_status",
    }
    missing = sorted(required - set(master.columns))
    if missing:
        raise DataFoundationError(f"Security Master missing columns: {missing}")
    start = pd.Timestamp(history_start).normalize()
    target = pd.Timestamp(target_session).normalize()
    out = master.copy()
    out["asset_type"] = out["asset_type"].fillna("").astype(str).str.upper()
    out["current_ticker"] = (
        out["current_ticker"].fillna("").astype(str).str.upper()
        .str.replace(".", "-", regex=False)
        .str.replace("/", "-", regex=False)
    )
    out["listing_date"] = pd.to_datetime(
        out["listing_date"], errors="coerce"
    ).dt.normalize()
    out["delisting_date"] = pd.to_datetime(
        out["delisting_date"], errors="coerce"
    ).dt.normalize()
    allowed = {str(value).upper() for value in allowed_asset_types}
    benchmarks = {str(value).upper() for value in benchmark_tickers}
    lifetime_overlap = (
        (out["listing_date"].isna() | out["listing_date"].le(target))
        & (out["delisting_date"].isna() | out["delisting_date"].ge(start))
    )
    equity = out["asset_type"].isin(allowed)
    benchmark = out["current_ticker"].isin(benchmarks) & out["asset_type"].eq("ETF")
    lifetime_is_auditable = (
        out["trading_status"].astype(str).str.upper().eq("ACTIVE")
        | out["delisting_date"].notna()
    )
    selected = out.loc[
        lifetime_overlap & lifetime_is_auditable & (equity | benchmark)
    ].copy()
    policy = (
        history_policy.copy()
        if history_policy is not None
        else empty_history_policy()
    )
    if not policy.empty:
        required_policy = {"security_id", "policy", "effective_from"}
        missing_policy = sorted(required_policy - set(policy.columns))
        if missing_policy:
            raise DataFoundationError(
                f"research history policy missing columns: {missing_policy}"
            )
        if policy["security_id"].astype(str).duplicated().any():
            raise DataFoundationError(
                "research history policy contains duplicate security_id"
            )
        policy = policy[["security_id", "policy", "effective_from"]].copy()
        policy["security_id"] = policy["security_id"].astype(str)
        policy["policy"] = policy["policy"].astype(str).str.upper()
        policy["effective_from"] = pd.to_datetime(
            policy["effective_from"], errors="coerce"
        ).dt.normalize()
        selected = selected.merge(
            policy,
            on="security_id",
            how="left",
            validate="one_to_one",
        )
    else:
        selected["policy"] = FULL_HISTORY
        selected["effective_from"] = pd.NaT
    selected["research_history_policy"] = selected["policy"].fillna(
        FULL_HISTORY
    )
    selected["research_eligible_from"] = pd.to_datetime(
        selected["effective_from"], errors="coerce"
    ).dt.normalize()
    selected = selected.loc[
        ~selected["research_history_policy"].eq(
            EXCLUDED_UNVERIFIABLE_HISTORY
        )
    ].copy()
    prospective = selected["research_history_policy"].eq(PROSPECTIVE_ONLY)
    if selected.loc[prospective, "research_eligible_from"].isna().any():
        raise DataFoundationError(
            "PROSPECTIVE_ONLY security is missing research_eligible_from"
        )
    selected = selected.loc[
        ~prospective
        | selected["research_eligible_from"].le(target)
    ].copy()
    selected["coverage_start"] = start
    prospective = selected["research_history_policy"].eq(PROSPECTIVE_ONLY)
    selected.loc[prospective, "coverage_start"] = selected.loc[
        prospective, "research_eligible_from"
    ].clip(lower=start)
    selected = selected.drop(columns=["policy", "effective_from"])
    selected["coverage_role"] = np.where(
        selected["current_ticker"].isin(benchmarks)
        & selected["asset_type"].eq("ETF"),
        "BENCHMARK_ONLY",
        np.where(selected["asset_type"].eq("ADR"), "ADR", "EQUITY"),
    )
    selected["is_current_coverage"] = (
        selected["delisting_date"].isna()
        & selected["trading_status"].astype(str).str.upper().eq("ACTIVE")
    ) | selected["coverage_role"].eq("BENCHMARK_ONLY")
    selected["ticker"] = selected["current_ticker"]
    if selected["security_id"].duplicated().any():
        raise DataFoundationError("coverage selection contains duplicate security_id")
    return selected.sort_values("security_id").reset_index(drop=True)


def coverage_alias_intervals(
    security_universe: pd.DataFrame,
    symbol_history: pd.DataFrame,
    *,
    history_start: str | pd.Timestamp,
    target_session: str | pd.Timestamp,
) -> pd.DataFrame:
    """Return clipped ticker intervals needed to fetch the selected identities."""
    start = pd.Timestamp(history_start).normalize()
    target = pd.Timestamp(target_session).normalize()
    required = {"security_id", "ticker", "effective_from", "effective_to"}
    missing = sorted(required - set(symbol_history.columns))
    if missing:
        raise DataFoundationError(f"symbol history missing columns: {missing}")
    selected_ids = set(security_universe["security_id"].astype(str))
    aliases = symbol_history.loc[
        symbol_history["security_id"].astype(str).isin(selected_ids)
    ].copy()
    aliases["ticker"] = (
        aliases["ticker"].astype(str).str.upper().str.replace(".", "-", regex=False)
        .str.replace("/", "-", regex=False)
    )
    aliases["effective_from"] = pd.to_datetime(
        aliases["effective_from"], errors="coerce"
    ).dt.normalize()
    aliases["effective_to"] = pd.to_datetime(
        aliases["effective_to"], errors="coerce"
    ).dt.normalize()
    aliases["fetch_start"] = aliases["effective_from"].fillna(start).clip(lower=start)
    aliases["fetch_end"] = aliases["effective_to"].fillna(target).clip(upper=target)
    aliases = aliases.loc[aliases["fetch_start"].le(aliases["fetch_end"])].copy()
    if "coverage_start" in security_universe.columns:
        starts = security_universe[["security_id", "coverage_start"]].copy()
        starts["security_id"] = starts["security_id"].astype(str)
        starts["coverage_start"] = pd.to_datetime(
            starts["coverage_start"], errors="coerce"
        ).dt.normalize()
        aliases = aliases.merge(
            starts,
            on="security_id",
            how="left",
            validate="many_to_one",
        )
        aliases["fetch_start"] = aliases[[
            "fetch_start", "coverage_start"
        ]].max(axis=1)
        aliases = aliases.drop(columns=["coverage_start"])
        aliases = aliases.loc[
            aliases["fetch_start"].le(aliases["fetch_end"])
        ].copy()

    observed = set(aliases["security_id"].astype(str))
    missing_ids = selected_ids - observed
    if missing_ids:
        fallback = security_universe.loc[
            security_universe["security_id"].astype(str).isin(missing_ids)
        ].copy()
        fallback["ticker"] = fallback["current_ticker"]
        fallback_start = fallback.get(
            "coverage_start",
            pd.Series(start, index=fallback.index),
        )
        fallback["fetch_start"] = pd.concat(
            [
                pd.to_datetime(
                    fallback["listing_date"], errors="coerce"
                ).fillna(start),
                pd.to_datetime(fallback_start, errors="coerce").fillna(start),
            ],
            axis=1,
        ).max(axis=1)
        fallback["fetch_end"] = fallback["delisting_date"].fillna(target).clip(upper=target)
        aliases = pd.concat(
            [
                aliases,
                fallback[["security_id", "ticker", "fetch_start", "fetch_end"]],
            ],
            ignore_index=True,
        )
    aliases = aliases[["security_id", "ticker", "fetch_start", "fetch_end"]]
    sessions = pd.DatetimeIndex(
        xcals.get_calendar("XNYS").sessions_in_range(
            start.date(), target.date()
        )
    ).tz_localize(None).normalize()
    if sessions.empty:
        raise DataFoundationError(
            f"no XNYS sessions between {start.date()} and {target.date()}"
        )
    session_values = sessions.to_numpy(dtype="datetime64[ns]")
    requested_starts = aliases["fetch_start"].to_numpy(dtype="datetime64[ns]")
    requested_ends = aliases["fetch_end"].to_numpy(dtype="datetime64[ns]")
    left = np.searchsorted(session_values, requested_starts, side="left")
    right = np.searchsorted(session_values, requested_ends, side="right") - 1
    valid = (left < len(session_values)) & (right >= 0) & (left <= right)
    aliases = aliases.loc[valid].copy()
    aliases["fetch_start"] = pd.to_datetime(session_values[left[valid]])
    aliases["fetch_end"] = pd.to_datetime(session_values[right[valid]])
    return (
        aliases
        .drop_duplicates()
        .sort_values(["security_id", "fetch_start", "ticker"])
        .reset_index(drop=True)
    )


def fetch_coverage_history_delta(
    security_universe: pd.DataFrame,
    symbol_history: pd.DataFrame,
    *,
    security_ids: Iterable[str],
    history_start: str | pd.Timestamp,
    target_session: str | pd.Timestamp,
    fetcher: Callable[[str, str, str], pd.DataFrame | None],
) -> tuple[pd.DataFrame, list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch complete dated alias intervals for identities absent from coverage."""
    requested_ids = {str(value) for value in security_ids}
    selected = security_universe.loc[
        security_universe["security_id"].astype(str).isin(requested_ids)
    ].copy()
    if not requested_ids:
        return pd.DataFrame(), [], []
    missing_ids = sorted(requested_ids - set(selected["security_id"].astype(str)))
    if missing_ids:
        raise DataFoundationError(
            f"history delta contains identities outside the selected universe: {missing_ids[:20]}"
        )
    current_tickers = selected.set_index("security_id")["current_ticker"].astype(str)
    aliases = coverage_alias_intervals(
        selected,
        symbol_history,
        history_start=history_start,
        target_session=target_session,
    )
    pieces: list[pd.DataFrame] = []
    failures: list[dict[str, Any]] = []
    fallbacks: list[dict[str, Any]] = []
    for row in aliases.itertuples(index=False):
        security_id = str(row.security_id)
        ticker = str(row.ticker)
        start = pd.Timestamp(row.fetch_start).date().isoformat()
        end = pd.Timestamp(row.fetch_end).date().isoformat()
        provider_ticker = ticker
        frame = fetcher(ticker, start, end)
        if frame is None or frame.empty:
            fallback = str(current_tickers.loc[security_id]).strip().upper()
            if fallback and fallback != ticker:
                frame = fetcher(fallback, start, end)
                if frame is not None and not frame.empty:
                    provider_ticker = fallback
                    fallbacks.append({
                        "security_id": security_id,
                        "requested_ticker": ticker,
                        "provider_ticker": fallback,
                        "start": start,
                        "end": end,
                        "rows": len(frame),
                    })
        if frame is None or frame.empty:
            failures.append({
                "security_id": security_id,
                "ticker": ticker,
                "start": start,
                "end": end,
            })
            continue
        work = frame.reset_index()
        if "date" not in work.columns:
            work = work.rename(columns={work.columns[0]: "date"})
        work["security_id"] = security_id
        work["ticker"] = ticker
        work["provider_ticker"] = provider_ticker
        pieces.append(work)
    if not pieces:
        return pd.DataFrame(), failures, fallbacks
    combined = pd.concat(pieces, ignore_index=True)
    combined["date"] = pd.to_datetime(combined["date"], errors="coerce").dt.normalize()
    if combined["date"].isna().any():
        raise DataFoundationError("history delta provider returned invalid dates")
    numeric = ["open", "high", "low", "close", "adj_close", "volume"]
    duplicates = combined.loc[
        combined.duplicated(["date", "security_id"], keep=False)
    ]
    if not duplicates.empty:
        conflicts = (
            duplicates.groupby(["date", "security_id"])[numeric]
            .nunique(dropna=False)
            .max(axis=1)
            .gt(1)
        )
        if conflicts.any():
            raise DataFoundationError(
                "history delta aliases returned conflicting bars for "
                f"{int(conflicts.sum())} date/security keys"
            )
    return (
        combined.drop(columns=["provider_ticker"])
        .drop_duplicates(["date", "security_id"], keep="last")
        .sort_values(["date", "security_id"])
        .reset_index(drop=True),
        failures,
        fallbacks,
    )


def map_eod_bulk_to_security_ids(
    bulk: pd.DataFrame,
    symbol_history: pd.DataFrame,
    security_universe: pd.DataFrame,
) -> pd.DataFrame:
    """Resolve each dated bulk-EOD ticker to one approved stable identity."""
    required_bars = {"date", "ticker", "open", "high", "low", "close", "adj_close", "volume"}
    missing_bars = sorted(required_bars - set(bulk.columns))
    if missing_bars:
        raise DataFoundationError(f"bulk EOD is missing columns: {missing_bars}")
    required_symbols = {"security_id", "ticker", "effective_from", "effective_to"}
    missing_symbols = sorted(required_symbols - set(symbol_history.columns))
    if missing_symbols:
        raise DataFoundationError(
            f"symbol history is missing columns: {missing_symbols}"
        )
    selected_ids = set(security_universe["security_id"].astype(str))
    symbols = symbol_history.copy()
    symbols["security_id"] = symbols["security_id"].astype(str)
    symbols = symbols.loc[symbols["security_id"].isin(selected_ids)]
    symbols["ticker"] = (
        symbols["ticker"].fillna("").astype(str).str.strip().str.upper()
        .str.replace(".", "-", regex=False)
        .str.replace("/", "-", regex=False)
    )
    symbols["effective_from"] = pd.to_datetime(
        symbols["effective_from"], errors="coerce"
    ).dt.normalize()
    symbols["effective_to"] = pd.to_datetime(
        symbols["effective_to"], errors="coerce"
    ).dt.normalize()

    bars = bulk.copy()
    bars["date"] = pd.to_datetime(bars["date"], errors="coerce").dt.normalize()
    bars["ticker"] = (
        bars["ticker"].fillna("").astype(str).str.strip().str.upper()
        .str.replace(".", "-", regex=False)
        .str.replace("/", "-", regex=False)
    )
    if bars["date"].isna().any() or bars["ticker"].eq("").any():
        raise DataFoundationError("bulk EOD contains invalid dates or tickers")
    numeric = ["open", "high", "low", "close", "adj_close", "volume"]
    duplicates = bars.loc[bars.duplicated(["date", "ticker"], keep=False)]
    if not duplicates.empty:
        conflicts = (
            duplicates.groupby(["date", "ticker"])[numeric]
            .nunique(dropna=False)
            .max(axis=1)
            .gt(1)
        )
        if conflicts.any():
            raise DataFoundationError(
                f"bulk EOD has {int(conflicts.sum())} conflicting date/ticker rows"
            )
        bars = bars.drop_duplicates(["date", "ticker"], keep="last")

    joined = bars.merge(
        symbols[["security_id", "ticker", "effective_from", "effective_to"]],
        on="ticker",
        how="inner",
        validate="many_to_many",
    )
    joined = joined.loc[
        (joined["effective_from"].isna() | joined["date"].ge(joined["effective_from"]))
        & (joined["effective_to"].isna() | joined["date"].le(joined["effective_to"]))
    ].copy()
    ambiguous = joined.loc[
        joined.duplicated(["date", "ticker"], keep=False)
    ].groupby(["date", "ticker"])["security_id"].nunique().gt(1)
    if ambiguous.any():
        sample = [
            {"date": key[0].date().isoformat(), "ticker": key[1]}
            for key in ambiguous[ambiguous].index[:20]
        ]
        raise DataFoundationError(
            f"bulk EOD ticker maps to multiple security_ids: {sample}"
        )
    joined = joined.drop_duplicates(["date", "security_id"], keep="last")
    return (
        joined[["date", "security_id", "ticker", *numeric]]
        .sort_values(["date", "security_id"])
        .reset_index(drop=True)
    )


def _link_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


class BroadCoverageStore:
    """Publish partitioned coverage through the existing dataset catalog."""

    def __init__(
        self,
        *,
        catalog: MarketDataCatalog | None = None,
        lake_dir: str | Path,
    ):
        self.catalog = catalog or MarketDataCatalog()
        self.lake_dir = Path(lake_dir)

    def stage_frames(
        self,
        frames: Iterable[pd.DataFrame],
        *,
        target_session: str | pd.Timestamp,
        ingestion_run_id: str,
        directory: str | Path,
    ) -> list[Path]:
        """Write bounded batches into resumable month partitions."""
        destination = Path(directory)
        destination.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        part_number = 0
        for frame in frames:
            normalized = normalize_coverage_bars(
                frame,
                target_session=target_session,
                ingestion_run_id=ingestion_run_id,
            )
            if normalized.empty:
                continue
            normalized["_period"] = normalized["date"].dt.to_period("M")
            for period, rows in normalized.groupby("_period", sort=True):
                path = (
                    destination
                    / f"year={int(period.year)}"
                    / f"month={int(period.month):02d}"
                    / f"part-{part_number:06d}.parquet"
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                rows.drop(columns="_period").to_parquet(path, index=False)
                paths.append(path)
                part_number += 1
        if not paths:
            raise DataFoundationError("coverage candidate contains no bars")
        return paths

    def _validate_partitions(
        self,
        paths: list[Path],
        *,
        security_universe: pd.DataFrame,
        target_session: pd.Timestamp,
        min_target_coverage: float,
    ) -> tuple[list[QualityCheck], dict[str, Any]]:
        import duckdb

        path_values = [str(path) for path in paths]
        validation_temp_root = self.lake_dir / "tmp"
        validation_temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="coverage-validation-",
            dir=validation_temp_root,
        ) as temporary:
            connection = duckdb.connect()
            try:
                escaped_temp = str(Path(temporary).resolve()).replace("'", "''")
                connection.execute("SET threads = 1")
                connection.execute("SET memory_limit = '420MB'")
                connection.execute("SET max_temp_directory_size = '12GB'")
                connection.execute("SET preserve_insertion_order = false")
                connection.execute(f"SET temp_directory = '{escaped_temp}'")
                connection.read_parquet(path_values).create_view("bars")
                row = connection.execute(
                """
                SELECT count(*) AS rows,
                       min(date) AS min_date,
                       max(date) AS max_date,
                       count(*) FILTER (WHERE date IS NULL OR security_id IS NULL OR security_id = '') AS invalid_keys,
                       count(*) FILTER (
                         WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL
                            OR adj_close IS NULL OR volume IS NULL
                       ) AS null_values,
                       count(*) FILTER (
                         WHERE NOT isfinite(open) OR NOT isfinite(high)
                            OR NOT isfinite(low) OR NOT isfinite(close)
                            OR NOT isfinite(adj_close) OR NOT isfinite(volume)
                            OR open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
                            OR adj_close <= 0 OR volume < 0
                       ) AS invalid_numeric,
                       count(*) FILTER (
                         WHERE high < greatest(open, close, low)
                            OR low > least(open, close, high)
                       ) AS invalid_ohlc,
                       count(*) FILTER (WHERE date > ?) AS future_rows
                FROM bars
                """,
                [target_session.date()],
                ).fetchone()
                off_session_rows = 0
                if row[1] is not None and row[2] is not None:
                    valid_sessions = _xnys_sessions_between(row[1], row[2])
                    connection.register(
                        "valid_xnys_sessions",
                        pd.DataFrame({"date": valid_sessions.date}),
                    )
                    off_session_rows = int(
                        connection.execute(
                            """
                            SELECT count(*)
                            FROM bars AS b
                            LEFT JOIN valid_xnys_sessions AS s USING (date)
                            WHERE s.date IS NULL
                            """
                        ).fetchone()[0]
                    )
                duplicate_count = 0
                if row[1] is not None and row[2] is not None:
                    for period in pd.period_range(row[1], row[2], freq="M"):
                        duplicate_count += int(
                            connection.execute(
                                """
                                SELECT count(*) - count(DISTINCT (date, security_id))
                                FROM bars
                                WHERE date >= ? AND date < ?
                                """,
                                [
                                    period.start_time.date(),
                                    (period + 1).start_time.date(),
                                ],
                            ).fetchone()[0]
                        )
                target_ids = {
                    str(value)
                    for value in connection.execute(
                        "SELECT DISTINCT security_id FROM bars WHERE date = ?",
                        [target_session.date()],
                    ).fetchnumpy()["security_id"]
                }
                bar_ids = {
                    str(value)
                    for value in connection.execute(
                        "SELECT DISTINCT security_id FROM bars"
                    ).fetchnumpy()["security_id"]
                }
            finally:
                connection.close()
        current_ids = set(
            security_universe.loc[
                security_universe["is_current_coverage"].astype(bool), "security_id"
            ].astype(str)
        )
        target_coverage = len(current_ids & target_ids) / len(current_ids) if current_ids else 0.0
        exited = security_universe.loc[
            pd.to_datetime(
                security_universe.get("delisting_date"), errors="coerce"
            ).notna()
        ]
        exited_ids = set(exited["security_id"].astype(str))
        exited_missing = sorted(exited_ids - bar_ids)
        selected_ids = set(security_universe["security_id"].astype(str))
        unexpected_ids = sorted(bar_ids - selected_ids)
        stats = {
            "row_count": int(row[0]),
            "security_count": len(bar_ids),
            "min_date": pd.Timestamp(row[1]).date() if row[1] is not None else None,
            "max_date": pd.Timestamp(row[2]).date() if row[2] is not None else None,
            "duplicate_count": duplicate_count,
            "invalid_keys": int(row[3]),
            "null_values": int(row[4]),
            "invalid_numeric": int(row[5]),
            "invalid_ohlc": int(row[6]),
            "future_rows": int(row[7]),
            "off_xnys_session_rows": off_session_rows,
            "target_coverage": float(target_coverage),
            "current_security_count": len(current_ids),
            "target_covered_count": len(current_ids & target_ids),
            "historical_exit_count": len(exited_ids),
            "historical_exit_missing": exited_missing,
            "unexpected_security_count": len(unexpected_ids),
            "unexpected_security_sample": unexpected_ids[:20],
            "validation_memory_limit_mb": 420,
            "validation_temp_directory_max_gb": 12,
        }
        checks = [
            QualityCheck(
                "unique_date_security",
                stats["duplicate_count"] == 0,
                stats["duplicate_count"],
                0,
                "coverage date/security keys are unique",
            ),
            QualityCheck(
                "required_values",
                stats["invalid_keys"] == 0 and stats["null_values"] == 0,
                {"invalid_keys": stats["invalid_keys"], "null_values": stats["null_values"]},
                0,
                "coverage required values are complete",
            ),
            QualityCheck(
                "valid_ohlcv",
                stats["invalid_numeric"] == 0 and stats["invalid_ohlc"] == 0,
                {"invalid_numeric": stats["invalid_numeric"], "invalid_ohlc": stats["invalid_ohlc"]},
                0,
                "coverage OHLCV values are valid",
            ),
            QualityCheck(
                "no_future_rows",
                stats["future_rows"] == 0,
                stats["future_rows"],
                0,
                "coverage contains no post-target rows",
            ),
            QualityCheck(
                "xnys_session_calendar",
                stats["off_xnys_session_rows"] == 0,
                stats["off_xnys_session_rows"],
                0,
                "every coverage row belongs to an XNYS trading session",
            ),
            QualityCheck(
                "target_session_coverage",
                bool(current_ids) and target_coverage >= float(min_target_coverage),
                {
                    "covered": len(current_ids & target_ids),
                    "current": len(current_ids),
                    "coverage": target_coverage,
                    "missing_sample": sorted(current_ids - target_ids)[:20],
                },
                float(min_target_coverage),
                f"target coverage {target_coverage:.2%}",
            ),
            QualityCheck(
                "historical_exit_bar_presence",
                not exited_missing,
                {"exit_count": len(exited_ids), "missing_sample": exited_missing[:20]},
                {"missing": 0},
                "every selected historical exit has at least one bar",
            ),
            QualityCheck(
                "selected_security_scope",
                not unexpected_ids,
                {
                    "unexpected_count": len(unexpected_ids),
                    "unexpected_sample": unexpected_ids[:20],
                },
                {"unexpected": 0},
                "coverage bars contain only identities in the selected universe",
            ),
        ]
        return checks, stats

    def publish_partitions(
        self,
        partition_paths: Iterable[str | Path],
        *,
        security_universe: pd.DataFrame,
        target_session: str | pd.Timestamp,
        security_master: SecurityMasterGeneration,
        price_semantics: Mapping[str, Any] | None = None,
        price_semantics_parent_version_id: str | None = None,
        min_target_coverage: float = 0.98,
        external_checks: Iterable[QualityCheck] = (),
        run_id: str | None = None,
        bar_quarantine_path: str | Path | None = None,
        quality_lineage: dict[str, Any] | None = None,
    ) -> CoveragePublication:
        """Authenticate, freeze and atomically publish one coverage version."""
        if security_master.status != "PUBLISHED":
            raise DataFoundationError(
                "formal coverage requires a published Security Master generation"
            )
        paths = [Path(path).resolve() for path in partition_paths]
        if not paths or any(not path.is_file() for path in paths):
            raise DataFoundationError("coverage partition list is empty or incomplete")
        target = pd.Timestamp(target_session).normalize()
        run_id = run_id or uuid4().hex
        lineage_parent = str(
            (quality_lineage or {}).get("parent_dataset_version_id") or ""
        ).strip() or None
        semantics_parent = str(
            price_semantics_parent_version_id or ""
        ).strip() or lineage_parent
        if lineage_parent and semantics_parent != lineage_parent:
            raise DataFoundationError(
                "price-semantics parent does not match quality lineage parent"
            )
        resolved_price_semantics = validate_price_semantics_contract(
            price_semantics
            or build_price_semantics_contract(
                source=FMP_CANONICAL_SOURCE,
                history_mode=(
                    "INCREMENTAL_FROM_AUTHENTICATED_PARENT"
                    if semantics_parent
                    else "FULL_REBUILD"
                ),
            )
        )
        semantic_contract = resolved_price_semantics
        history_mode = str(semantic_contract["history_mode"]).upper()
        if history_mode == "INCREMENTAL_FROM_AUTHENTICATED_PARENT" and not semantics_parent:
            raise DataFoundationError(
                "incremental price semantics require an authenticated parent version"
            )
        if history_mode == "FULL_REBUILD" and semantics_parent:
            raise DataFoundationError(
                "full-rebuild price semantics cannot reference a parent version"
            )
        if history_mode == "INCREMENTAL_FROM_AUTHENTICATED_PARENT":
            parent = self.catalog.get_version(
                semantics_parent,
                universe=US_EQUITY_COVERAGE,
            )
            if parent is None:
                raise DataFoundationError(
                    "incremental broad-coverage semantic parent is not a "
                    "published immutable version"
                )
            MarketDataReader(catalog=self.catalog).verify_version(
                parent,
                require_price_semantics=True,
            )
            if pd.Timestamp(parent.target_session).normalize() > target:
                raise DataFoundationError(
                    "incremental broad-coverage target predates its semantic parent"
                )
        checks, stats = self._validate_partitions(
            paths,
            security_universe=security_universe,
            target_session=target,
            min_target_coverage=min_target_coverage,
        )
        checks.extend(list(external_checks))
        failed = [check for check in checks if not check.passed]
        if failed:
            detail = "; ".join(f"{item.name}: {item.message}" for item in failed)
            raise DataFoundationError(
                f"[{US_EQUITY_COVERAGE}] candidate rejected: {detail}"
            )

        version_id = uuid4().hex
        created_at = _utc_now()
        base = self.lake_dir / "curated" / US_EQUITY_COVERAGE
        staging = base / f".staging_{version_id}"
        destination = base / f"version={version_id}"
        staging.mkdir(parents=True)
        try:
            # Candidate backfills are downloaded by security batch and year so
            # they can resume cheaply.  The immutable read model is compacted
            # by calendar month.  Daily overlap updates can then hard-link every
            # unaffected month instead of changing a whole year's hash.
            source_metadata: list[dict[str, Any]] = []
            for source in paths:
                frame = pd.read_parquet(source, columns=["date", "security_id"])
                dates = pd.to_datetime(frame["date"], errors="coerce")
                if frame.empty or dates.isna().any():
                    raise DataFoundationError(
                        f"coverage source partition has invalid dates: {source}"
                    )
                first = pd.Timestamp(dates.min()).normalize()
                last = pd.Timestamp(dates.max()).normalize()
                source_metadata.append({
                    "path": source,
                    "rows": len(frame),
                    "security_count": int(frame["security_id"].nunique()),
                    "min_date": first,
                    "max_date": last,
                    "period": (
                        first.to_period("M")
                        if first.to_period("M") == last.to_period("M")
                        else None
                    ),
                })

            by_period: dict[pd.Period, list[dict[str, Any]]] = {}
            month_pure = True
            for item in source_metadata:
                period = item["period"]
                if period is None:
                    month_pure = False
                    break
                by_period.setdefault(period, []).append(item)
            month_pure = month_pure and all(
                len(items) == 1 for items in by_period.values()
            )

            materialized: list[Path] = []
            if month_pure:
                for period, items in sorted(by_period.items()):
                    destination_path = (
                        staging
                        / "bars"
                        / f"year={int(period.year)}"
                        / f"month={int(period.month):02d}"
                        / "part-000000.parquet"
                    )
                    _link_or_copy(items[0]["path"], destination_path)
                    materialized.append(destination_path)
            else:
                import duckdb

                # A single seven-year ORDER BY can spend an hour reclaiming
                # memory on the 2 GB production host.  Bound both the sort and
                # working set to one calendar month.  Each source partition is
                # immutable and its date range above determines exactly which
                # monthly query may read it.
                minimum_period = min(
                    item["min_date"].to_period("M") for item in source_metadata
                )
                maximum_period = max(
                    item["max_date"].to_period("M") for item in source_metadata
                )
                validation_temp_root = self.lake_dir / "tmp"
                validation_temp_root.mkdir(parents=True, exist_ok=True)
                connection = duckdb.connect()
                try:
                    connection.execute("SET threads = 1")
                    connection.execute("SET memory_limit = '320MB'")
                    connection.execute("SET preserve_insertion_order = false")
                    connection.execute("SET max_temp_directory_size = '12GB'")
                    with tempfile.TemporaryDirectory(
                        prefix="coverage-compaction-",
                        dir=validation_temp_root,
                    ) as temporary:
                        escaped_temp = str(Path(temporary).resolve()).replace(
                            "'", "''"
                        )
                        connection.execute(
                            f"SET temp_directory = '{escaped_temp}'"
                        )
                        for period in pd.period_range(
                            minimum_period, maximum_period, freq="M"
                        ):
                            period_start = period.start_time.normalize()
                            period_end = min(period.end_time.normalize(), target)
                            selected_sources = [
                                item["path"]
                                for item in source_metadata
                                if item["min_date"] <= period_end
                                and item["max_date"] >= period_start
                            ]
                            if not selected_sources:
                                continue
                            destination_path = (
                                staging
                                / "bars"
                                / f"year={int(period.year)}"
                                / f"month={int(period.month):02d}"
                                / "part-000000.parquet"
                            )
                            destination_path.parent.mkdir(
                                parents=True, exist_ok=True
                            )
                            destination_sql = str(destination_path).replace(
                                "'", "''"
                            )
                            connection.execute(
                                f"""
                                COPY (
                                    SELECT *
                                    FROM read_parquet(
                                        ?, hive_partitioning = false
                                    )
                                    WHERE date >= ? AND date <= ?
                                    ORDER BY date, security_id
                                ) TO '{destination_sql}' (
                                    FORMAT PARQUET,
                                    COMPRESSION SNAPPY,
                                    ROW_GROUP_SIZE 100000
                                )
                                """,
                                [
                                    [str(path) for path in selected_sources],
                                    period_start.date(),
                                    period_end.date(),
                                ],
                            )
                            materialized.append(destination_path)
                finally:
                    connection.close()
                if not materialized:
                    raise DataFoundationError(
                        "coverage monthly compaction produced no partitions"
                    )

            entries: list[dict[str, Any]] = []
            for index, target_path in enumerate(sorted(materialized)):
                frame = pd.read_parquet(
                    target_path, columns=["date", "security_id"]
                )
                dates = pd.to_datetime(frame["date"], errors="coerce")
                period_values = dates.dt.to_period("M").dropna().unique()
                if len(period_values) != 1:
                    raise DataFoundationError(
                        "published coverage partition must contain one month"
                    )
                period = period_values[0]
                relative = target_path.relative_to(staging)
                entries.append({
                    "file": relative.as_posix(),
                    "sha256": _file_sha256(target_path),
                    "rows": len(frame),
                    "security_count": int(frame["security_id"].nunique()),
                    "min_date": dates.min().date().isoformat(),
                    "max_date": dates.max().date().isoformat(),
                    "year": int(period.year),
                    "month": int(period.month),
                    "precedence": index,
                })
            index_payload = {
                "schema_version": PARTITION_INDEX_SCHEMA_VERSION,
                "storage_type": PARTITION_STORAGE_TYPE,
                "partition_frequency": COVERAGE_PARTITION_FREQUENCY,
                "version_id": version_id,
                "key_columns": ["date", "security_id"],
                "partitions": entries,
            }
            bars_index_path = staging / "bars_index.json"
            bars_index_path.write_text(
                json.dumps(index_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            bars_index_sha = _file_sha256(bars_index_path)
            universe_path = staging / "security_universe.parquet"
            security_universe.to_parquet(universe_path, index=False)
            universe_sha = _file_sha256(universe_path)
            quarantine_name = None
            quarantine_sha = None
            quarantine_rows = 0
            if bar_quarantine_path is not None:
                quarantine_source = Path(bar_quarantine_path).resolve()
                if not quarantine_source.is_file():
                    raise DataFoundationError(
                        f"bar quarantine artifact is missing: {quarantine_source}"
                    )
                quarantine_target = staging / "bar_quarantine.parquet"
                _link_or_copy(quarantine_source, quarantine_target)
                quarantine = pd.read_parquet(quarantine_target)
                missing_quarantine = sorted(
                    set(BAR_QUARANTINE_COLUMNS) - set(quarantine.columns)
                )
                if missing_quarantine:
                    raise DataFoundationError(
                        "bar quarantine artifact is missing columns: "
                        f"{missing_quarantine}"
                    )
                quarantine_name = quarantine_target.name
                quarantine_sha = _file_sha256(quarantine_target)
                quarantine_rows = len(quarantine)
            manifest = {
                "schema_version": 5,
                "publication_type": "US_EQUITY_COVERAGE",
                "version_id": version_id,
                "run_id": run_id,
                "universe": US_EQUITY_COVERAGE,
                "provider": "fmp",
                "price_semantics": semantic_contract,
                "price_semantics_parent_version_id": semantics_parent,
                "status": "PUBLISHED",
                "target_session": target.date().isoformat(),
                "created_at": created_at.isoformat(),
                "bars_storage_type": PARTITION_STORAGE_TYPE,
                "bars_partition_frequency": COVERAGE_PARTITION_FREQUENCY,
                "bars_sha256": bars_index_sha,
                "universe_sha256": universe_sha,
                "membership_sha256": None,
                "security_master_generation_id": security_master.generation_id,
                "security_master_manifest_sha256": security_master.manifest_sha256,
                "partition_count": len(entries),
                "bar_quarantine_path": quarantine_name,
                "bar_quarantine_sha256": quarantine_sha,
                "bar_quarantine_rows": quarantine_rows,
                "compaction_strategy": (
                    "MONTH_PURE_LINK_V1"
                    if month_pure
                    else "BOUNDED_MONTHLY_SORT_V1"
                ),
                "quality_lineage": quality_lineage or {},
                "statistics": stats,
                "quality_checks": [check.to_dict() for check in checks],
            }
            manifest_path = staging / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            manifest_sha = _file_sha256(manifest_path)
            base.mkdir(parents=True, exist_ok=True)
            os.replace(staging, destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        version = DatasetVersion(
            version_id=version_id,
            run_id=run_id,
            universe=US_EQUITY_COVERAGE,
            provider="fmp",
            status="PUBLISHED",
            target_session=target.date(),
            created_at=created_at,
            row_count=stats["row_count"],
            ticker_count=stats["security_count"],
            min_date=stats["min_date"],
            max_date=stats["max_date"],
            target_coverage=stats["target_coverage"],
            bars_path=_portable_path(destination / bars_index_path.name),
            universe_path=_portable_path(destination / universe_path.name),
            membership_path=None,
            membership_checksum_sha256=None,
            manifest_path=_portable_path(destination / manifest_path.name),
            checksum_sha256=bars_index_sha,
            universe_checksum_sha256=universe_sha,
            manifest_checksum_sha256=manifest_sha,
        )
        self.catalog.initialize()
        with file_lock(self.catalog.writer_lock_path):
            self.catalog.start_run(
                run_id=run_id,
                universe=US_EQUITY_COVERAGE,
                provider="fmp",
                target_session=target.date(),
            )
            try:
                self.catalog.register_version(version, checks, publish=True)
                self.catalog.finish_run(run_id, status="PUBLISHED")
            except Exception as exc:
                self.catalog.finish_run(
                    run_id, status="FAILED", error_message=str(exc)
                )
                raise
        return CoveragePublication(
            version=version,
            partition_count=len(entries),
            security_master_generation_id=security_master.generation_id,
            checks=tuple(checks),
            statistics=stats,
        )

    def publish_frames(
        self,
        frames: Iterable[pd.DataFrame],
        **kwargs: Any,
    ) -> CoveragePublication:
        """Test/pilot convenience wrapper; production backfills pass checkpoints."""
        target = kwargs["target_session"]
        run_id = str(kwargs.get("run_id") or uuid4().hex)
        kwargs["run_id"] = run_id
        with tempfile.TemporaryDirectory(prefix="coverage_candidate_") as temporary:
            paths = self.stage_frames(
                frames,
                target_session=target,
                ingestion_run_id=run_id,
                directory=temporary,
            )
            return self.publish_partitions(paths, **kwargs)


class BroadCoverageReader:
    """Predicate-pushed reader retaining stable ``security_id`` columns."""

    def __init__(self, *, market_reader: MarketDataReader | None = None):
        self.market_reader = market_reader or MarketDataReader()

    def load_bars(
        self,
        *,
        security_ids: Iterable[str] | None = None,
        start: str | date | pd.Timestamp | None = None,
        end: str | date | pd.Timestamp | None = None,
        version: DatasetVersion | str | None = None,
        columns: Iterable[str] | None = None,
        ordered: bool = True,
    ) -> pd.DataFrame:
        selected = (
            version
            if isinstance(version, DatasetVersion)
            else self.market_reader.require_version(
                US_EQUITY_COVERAGE,
                version,
                require_price_semantics=True,
            )
            if isinstance(version, str)
            else self.market_reader.require_latest(
                US_EQUITY_COVERAGE,
                require_price_semantics=True,
            )
        )
        if isinstance(version, DatasetVersion):
            self.market_reader.verify_version(
                selected,
                require_price_semantics=True,
            )
        paths = self.market_reader.partition_paths(selected, start=start, end=end)
        ids = sorted({str(value) for value in security_ids or []})
        selected_columns = (
            list(dict.fromkeys(str(column) for column in columns))
            if columns is not None
            else list(COVERAGE_BAR_COLUMNS)
        )
        invalid_columns = sorted(set(selected_columns) - set(COVERAGE_BAR_COLUMNS))
        if invalid_columns:
            raise DataFoundationError(
                f"unsupported broad-coverage columns: {invalid_columns}"
            )
        if "date" not in selected_columns:
            selected_columns.insert(0, "date")
        if security_ids is not None and not ids:
            return pd.DataFrame(columns=selected_columns)
        conditions: list[str] = []
        parameters: list[Any] = []
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            conditions.append(f"security_id IN ({placeholders})")
            parameters.extend(ids)
        if start is not None:
            conditions.append("date >= ?")
            parameters.append(pd.Timestamp(start).date())
        if end is not None:
            conditions.append("date <= ?")
            parameters.append(pd.Timestamp(end).date())
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        import duckdb

        connection = duckdb.connect()
        try:
            connection.execute("SET threads = 1")
            connection.read_parquet([str(path) for path in paths]).create_view(
                "coverage_bars"
            )
            projection = ", ".join(f'"{column}"' for column in selected_columns)
            order_by = "ORDER BY date, security_id" if ordered else ""
            frame = connection.execute(
                f"SELECT {projection} FROM coverage_bars {where} {order_by}",
                parameters,
            ).df()
        finally:
            connection.close()
        frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
        return frame


__all__ = [
    "BroadCoverageReader",
    "BroadCoverageStore",
    "BAR_QUARANTINE_COLUMNS",
    "COVERAGE_BAR_COLUMNS",
    "COVERAGE_PARTITION_FREQUENCY",
    "CoveragePublication",
    "PARTITION_INDEX_SCHEMA_VERSION",
    "PARTITION_STORAGE_TYPE",
    "coverage_alias_intervals",
    "coverage_bar_quarantine_checks",
    "map_eod_bulk_to_security_ids",
    "normalize_coverage_bars",
    "select_coverage_securities",
    "split_coverage_bar_quality",
]
