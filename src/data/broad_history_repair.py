"""Explicit, whole-security recovery for unauthenticated incremental scales.

Never mix a repaired security's old prefix or bulk overlap into its newly
fetched canonical history. Other securities still require scale authentication.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd

from src.data.broad_coverage import (
    coverage_alias_intervals, normalize_coverage_bars, split_coverage_bar_quality,
)
from src.data.foundation import DataFoundationError, _rebase_parent_to_fetched_scale
from src.data.semantic_recovery import is_recoverable_semantic_drift
from src.utils.io import atomic_save_json


REPAIR_METHOD = "FULL_SECURITY_CANONICAL_REPLACEMENT_V1"
VALUE_COLUMNS = ["open", "high", "low", "close", "adj_close", "volume", "unadjusted_close"]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def audit_overlap(previous: pd.DataFrame, fresh: pd.DataFrame, parent_ids: set[str]):
    """Collect every failure, not just the first failing security in sort order."""
    old_groups = previous.groupby("security_id", sort=False).indices
    audit, failures = [], []
    for sid, rows in fresh.groupby("security_id", sort=True):
        sid = str(sid)
        if sid not in parent_ids:
            continue
        try:
            if sid not in old_groups:
                raise DataFoundationError(
                    f"{sid}: continuing security has no overlap anchor; run a full rebuild"
                )
            old = previous.iloc[old_groups[sid]].copy()
            current = rows.copy()
            old["ticker"] = current["ticker"] = sid
            _, entries = _rebase_parent_to_fetched_scale(old, current)
            for entry in entries:
                entry.pop("older_rows_rebased", None)
                audit.append({**entry, "security_id": sid})
        except DataFoundationError as exc:
            failures.append({
                "security_id": sid, "ticker": str(rows.iloc[-1]["ticker"]),
                "error": str(exc), "recoverable": is_recoverable_semantic_drift(exc),
            })
    return audit, failures


def validate_replacement(frame, *, security_id, previous, recent, aliases, target):
    """Reject truncated/invalid histories; no repair-specific quality exemptions."""
    if frame.empty or set(frame["security_id"].astype(str)) != {security_id}:
        raise DataFoundationError(f"{security_id}: empty or foreign replacement identity")
    dates = pd.to_datetime(frame["date"], errors="coerce")
    if dates.isna().any() or dates.duplicated().any() or dates.gt(target).any():
        raise DataFoundationError(f"{security_id}: invalid, duplicate or future replacement dates")
    _, bad = split_coverage_bar_quality(frame)
    if not bad.empty:
        raise DataFoundationError(f"{security_id}: replacement has {len(bad)} invalid bars")
    nominal = pd.to_numeric(frame["unadjusted_close"], errors="coerce")
    if not np.isfinite(nominal).all() or nominal.le(0).any():
        raise DataFoundationError(f"{security_id}: incomplete independently sourced nominal prices")
    allowed = pd.Series(False, index=frame.index)
    for alias in aliases.itertuples(index=False):
        allowed |= dates.between(alias.fetch_start, alias.fetch_end) & frame.ticker.eq(alias.ticker)
    if not allowed.all():
        raise DataFoundationError(f"{security_id}: replacement violates approved alias intervals")
    required = set(pd.to_datetime(previous["date"])) | set(pd.to_datetime(recent["date"]))
    missing = sorted(required - set(dates))
    if missing:
        raise DataFoundationError(
            f"{security_id}: full replacement loses {len(missing)} authenticated dates: "
            f"{[str(d.date()) for d in missing[:20]]}"
        )
    # Conflicting endpoints are retained as evidence, not silently rounded.
    matched = recent.merge(frame, on=["date", "security_id"], suffixes=("_bulk", "_full"))
    conflicts = {}
    for field in VALUE_COLUMNS[:-1]:
        unequal = matched[f"{field}_bulk"].ne(matched[f"{field}_full"])
        conflicts[field] = int(unequal.sum())
    return {
        "rows": len(frame), "required_dates": len(required), "missing_dates": 0,
        "min_date": str(dates.min().date()), "max_date": str(dates.max().date()),
        "bulk_conflict_counts": conflicts,
        "selected_source": "FMP_FULL_PLUS_DIVIDEND_ADJUSTED_AND_NON_SPLIT_ADJUSTED",
        "selection_scope": "ENTIRE_SECURITY_HISTORY_INCLUDING_OVERLAP_AND_NEW_SESSIONS",
    }


def fetch_replacement(*, cache_dir, contract, security_id, universe, symbols,
                      previous, recent, history_start, target, fetcher):
    """Freeze one complete history; only a hash-bound verified success is reusable."""
    selected = universe.loc[universe.security_id.astype(str).eq(security_id)]
    if len(selected) != 1 or not symbols.security_id.astype(str).eq(security_id).any():
        raise DataFoundationError(f"{security_id}: missing unique approved security/alias")
    aliases = coverage_alias_intervals(
        selected, symbols, history_start=history_start, target_session=target,
    )
    binding = {**contract, "method": REPAIR_METHOD, "security_id": security_id,
               "history_start": str(history_start), "target_session": str(pd.Timestamp(target).date()),
               "aliases": json.loads(aliases.to_json(orient="records", date_format="iso"))}
    key = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    base = Path(cache_dir) / "full_security_repair" / security_id / key
    base.mkdir(parents=True, exist_ok=True)
    manifest_path = base / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        path = base / manifest["artifact"]
        if manifest["contract"] != binding or file_sha256(path) != manifest["sha256"]:
            raise DataFoundationError(f"{security_id}: replacement cache binding/hash mismatch")
        for raw in manifest["raw_artifacts"]:
            if file_sha256(base / raw["path"]) != raw["sha256"]:
                raise DataFoundationError(f"{security_id}: raw replacement cache hash mismatch")
        frame = pd.read_parquet(path)
        proof = validate_replacement(frame, security_id=security_id, previous=previous,
                                     recent=recent, aliases=aliases, target=target)
        return path, {**proof, "manifest_path": str(manifest_path),
                      "manifest_sha256": file_sha256(manifest_path), "cache_hit": True}
    attempt = base / f"attempt={uuid4().hex}"
    attempt.mkdir()
    pieces, raw_artifacts = [], []
    try:
        for i, alias in enumerate(aliases.itertuples(index=False)):
            start, end = str(alias.fetch_start.date()), str(alias.fetch_end.date())
            raw = fetcher(str(alias.ticker), start, end)
            if raw is None or raw.empty:
                raise DataFoundationError(f"{security_id}: empty full history for {alias.ticker} {start}..{end}")
            raw_path = attempt / f"alias-{i}.parquet"
            raw.to_parquet(raw_path)
            raw_artifacts.append({"path": str(raw_path.relative_to(base)), "sha256": file_sha256(raw_path)})
            work = raw.reset_index()
            if "date" not in work:
                work = work.rename(columns={work.columns[0]: "date"})
            dates = pd.to_datetime(work.date, errors="coerce")
            if dates.isna().any() or not dates.between(alias.fetch_start, alias.fetch_end).all():
                raise DataFoundationError(f"{security_id}: provider returned dates outside requested alias interval")
            work["security_id"], work["ticker"] = security_id, alias.ticker
            pieces.append(work)
        if not pieces:
            raise DataFoundationError(f"{security_id}: no approved historical alias intervals")
        frame = normalize_coverage_bars(pd.concat(pieces, ignore_index=True),
                                       target_session=target, ingestion_run_id=attempt.name,
                                       source="FMP_FULL_SECURITY_REPAIR")
        proof = validate_replacement(frame, security_id=security_id, previous=previous,
                                     recent=recent, aliases=aliases, target=target)
        path = attempt / "validated.parquet"
        frame.to_parquet(path, index=False)
        atomic_save_json({"contract": binding, "artifact": str(path.relative_to(base)),
                          "sha256": file_sha256(path), "raw_artifacts": raw_artifacts,
                          "proof": proof, "fetched_at": datetime.now(timezone.utc).isoformat()}, manifest_path)
        return path, {**proof, "manifest_path": str(manifest_path),
                      "manifest_sha256": file_sha256(manifest_path), "cache_hit": False}
    except Exception as exc:
        atomic_save_json({"contract": binding, "status": "FAIL", "error": str(exc),
                          "raw_artifacts": raw_artifacts}, attempt / "failure.json")
        if isinstance(exc, ValueError):
            raise DataFoundationError(f"{security_id}: full-history provider contract failed: {exc}") from exc
        raise


def replace_month(old, delta, replacement, repaired_ids):
    """Erase all old/bulk rows for repaired identities before adding full history."""
    retained = [f.loc[~f.security_id.astype(str).isin(repaired_ids)]
                for f in (old, delta) if not f.empty]
    return pd.concat([*retained, replacement], ignore_index=True)


def verify_repaired_rows(actual, expected, repaired_ids):
    columns = ["date", "security_id", "ticker", *VALUE_COLUMNS]
    observed = actual.loc[actual.security_id.astype(str).isin(repaired_ids), columns]
    expected = expected.loc[:, columns]
    try:
        pd.testing.assert_frame_equal(
            observed.sort_values(["date", "security_id"]).reset_index(drop=True),
            expected.sort_values(["date", "security_id"]).reset_index(drop=True),
            check_exact=True, check_dtype=False,
        )
    except AssertionError as exc:
        raise DataFoundationError("candidate does not exactly equal full-security replacements") from exc
