"""Explicit, whole-security recovery for unauthenticated incremental scales.

Never mix a repaired security's old prefix or bulk overlap into its newly
fetched canonical history. Other securities still require scale authentication.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd
import yaml

from src.data.broad_coverage import (
    coverage_alias_intervals, normalize_coverage_bars, split_coverage_bar_quality,
)
from src.data.foundation import DataFoundationError, _rebase_parent_to_fetched_scale
from src.data.semantic_recovery import is_recoverable_semantic_drift
from src.utils.io import atomic_save_json


REPAIR_METHOD = "FULL_SECURITY_CANONICAL_REPLACEMENT_V2"
VALUE_COLUMNS = ["open", "high", "low", "close", "adj_close", "volume", "unadjusted_close"]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_repair_rules(path, *, catalog, market_reader):
    """Authenticate a narrowly reviewed policy and its immutable quarantine source."""
    from src.config import CONFIG
    path = Path(path)
    rules = yaml.safe_load(path.read_text())
    if not isinstance(rules, dict) or rules.get("schema_version") != 1:
        raise DataFoundationError("invalid history repair rules")
    mappings = rules.get("query_mappings", [])
    keys = set()
    for rule in mappings:
        key = (rule["security_id"], rule["historical_ticker"])
        if key in keys or rule["historical_ticker"] == rule["query_ticker"]:
            raise DataFoundationError("ambiguous history query mapping")
        keys.add(key)
        start, end, next_start = [pd.Timestamp(rule[k]) for k in ("start", "end", "next_alias_start")]
        if not (start <= end < next_start) or not rule.get("reason") or not rule.get("sources"):
            raise DataFoundationError("history query mapping requires bounded dates and evidence")
        if not all(str(url).startswith("https://www.sec.gov/Archives/edgar/") for url in rule["sources"]):
            raise DataFoundationError("history query mapping requires SEC sources")
    source = rules["prior_quarantine"]
    version = catalog.get_version(source["version_id"], universe="US_EQUITY_COVERAGE")
    if version is None or version.manifest_checksum_sha256 != source["manifest_sha256"]:
        raise DataFoundationError("prior quarantine version/manifest mismatch")
    # This authenticates excluded raw evidence, not tradable legacy prices.
    # New accepted bars must still satisfy the current canonical price contract.
    manifest = market_reader.verify_version(version, require_price_semantics=False)
    if manifest.get("bar_quarantine_sha256") != source["quarantine_sha256"]:
        raise DataFoundationError("prior quarantine hash mismatch")
    location = CONFIG.abs_path(version.manifest_path).parent / manifest["bar_quarantine_path"]
    ledger = pd.read_parquet(location)
    selected = []
    for row in source["rows"]:
        found = ledger.loc[ledger.security_id.eq(row["security_id"]) & ledger.ticker.eq(row["ticker"])
                           & pd.to_datetime(ledger.date).eq(pd.Timestamp(row["date"]))]
        if len(found) != 1:
            raise DataFoundationError("reviewed quarantine key is missing or ambiguous")
        selected.append(found)
    approved = pd.concat(selected, ignore_index=True)
    if approved.duplicated(["date", "security_id"]).any():
        raise DataFoundationError("duplicate reviewed quarantine keys")
    return mappings, approved, {"rules_sha256": file_sha256(path), "prior_quarantine": source}


def query_segments(aliases, security_id, mappings):
    """Provider query keys may differ from historical symbols only in reviewed windows."""
    rules = [r for r in mappings if r["security_id"] == security_id]
    for rule in rules:
        old = aliases.loc[aliases.ticker.eq(rule["historical_ticker"])]
        new = aliases.loc[aliases.ticker.eq(rule["query_ticker"])]
        if (len(old) != 1 or len(new) != 1
                or old.iloc[0].fetch_end != pd.Timestamp(rule["end"])
                or old.iloc[0].fetch_start > pd.Timestamp(rule["start"])
                or new.iloc[0].fetch_start != pd.Timestamp(rule["next_alias_start"])):
            raise DataFoundationError("reviewed query mapping alias contract drifted")
    segments = []
    for alias in aliases.itertuples(index=False):
        rule = next((r for r in rules if r["historical_ticker"] == alias.ticker), None)
        if rule is None:
            segments.append((alias.ticker, alias.ticker, alias.fetch_start, alias.fetch_end))
        else:
            boundary = pd.Timestamp(rule["start"])
            if alias.fetch_start < boundary:
                segments.append((alias.ticker, alias.ticker, alias.fetch_start, boundary - pd.Timedelta(days=1)))
            segments.append((alias.ticker, rule["query_ticker"], boundary, alias.fetch_end))
    return segments


def inherit_quarantine(frame, *, approved, previous):
    """Keep known invalid rows out, never remove a previously authenticated valid date."""
    clean, bad = split_coverage_bar_quality(frame)
    if bad.empty:
        return clean, bad
    columns = ["date", "security_id", "ticker", *VALUE_COLUMNS[:-1], "quality_reasons"]
    if approved is None or approved.empty:
        raise DataFoundationError(f"replacement has {len(bad)} invalid bars without reviewed quarantine")
    for row in bad.itertuples(index=False):
        if (previous.security_id.eq(row.security_id) & pd.to_datetime(previous.date).eq(row.date)).any():
            raise DataFoundationError("quarantine would remove an authenticated parent date")
        old = approved.loc[approved.security_id.eq(row.security_id) & approved.date.eq(row.date)]
        new = bad.loc[bad.security_id.eq(row.security_id) & bad.date.eq(row.date)]
        try:
            pd.testing.assert_frame_equal(new[columns].reset_index(drop=True), old[columns].reset_index(drop=True),
                                          check_exact=True, check_dtype=False)
        except AssertionError as exc:
            raise DataFoundationError("replacement invalid bar does not exactly match reviewed quarantine") from exc
    return clean, bad


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


def _frozen_raw_inputs(base, binding, segments):
    """Reuse raw bytes, never a validation result, after a reviewed policy change."""
    def source_contract(value):
        return {k: v for k, v in value.items() if k not in {"rules", "reviewed_rules"}}
    expected_queries = [(h, q, str(first.date()), str(last.date())) for h, q, first, last in segments]
    candidates = {}
    records = list(base.parent.glob("*/manifest.json")) + list(base.parent.glob("*/attempt=*/failure.json"))
    for record in sorted(records):
        evidence = json.loads(record.read_text())
        if source_contract(evidence.get("contract", {})) != source_contract(binding):
            continue
        artifacts = evidence.get("raw_artifacts", [])
        queries = [(a.get("historical_ticker"), a.get("query_ticker"), a.get("start"), a.get("end")) for a in artifacts]
        if queries != expected_queries:
            continue  # Partial failed downloads are not a frozen complete source.
        root = record.parent if record.name == "manifest.json" else record.parent.parent
        paths = [root / a["path"] for a in artifacts]
        if any(not p.is_file() or file_sha256(p) != a["sha256"] for p, a in zip(paths, artifacts)):
            raise DataFoundationError("frozen full-history raw input hash mismatch")
        signature = tuple(a["sha256"] for a in artifacts)
        candidates.setdefault(signature, (paths, {"record_path": str(record),
                                                  "record_sha256": file_sha256(record),
                                                  "fetched_at": evidence.get("fetched_at"),
                                                  "raw_artifacts": artifacts}))
    if len(candidates) > 1:
        raise DataFoundationError("multiple different frozen full-history sources; explicit source review required")
    return next(iter(candidates.values())) if candidates else ([], None)


def fetch_replacement(*, cache_dir, contract, security_id, universe, symbols,
                      previous, recent, history_start, target, fetcher,
                      query_mappings=(), approved_quarantine=None, rules_contract=None,
                      selection_scope="ENTIRE_SECURITY_HISTORY_INCLUDING_OVERLAP_AND_NEW_SESSIONS",
                      reuse_frozen_inputs=False):
    """Freeze one complete history; only a hash-bound verified success is reusable."""
    selected = universe.loc[universe.security_id.astype(str).eq(security_id)]
    if len(selected) != 1 or not symbols.security_id.astype(str).eq(security_id).any():
        raise DataFoundationError(f"{security_id}: missing unique approved security/alias")
    aliases = coverage_alias_intervals(
        selected, symbols, history_start=history_start, target_session=target,
    )
    segments = query_segments(aliases, security_id, query_mappings)
    binding = {**contract, "method": REPAIR_METHOD, "security_id": security_id,
               "rules": rules_contract,
               "selection_scope": selection_scope,
               "query_mappings": [r for r in query_mappings if r["security_id"] == security_id],
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
        quarantine_path = base / manifest["quarantine_artifact"]
        if file_sha256(quarantine_path) != manifest["quarantine_sha256"]:
            raise DataFoundationError("replacement quarantine cache hash mismatch")
        _, bad = inherit_quarantine(pd.concat([frame, pd.read_parquet(quarantine_path)], ignore_index=True),
                                    approved=approved_quarantine, previous=previous)
        proof = validate_replacement(frame, security_id=security_id, previous=previous,
                                     recent=recent, aliases=aliases, target=target)
        proof["selection_scope"] = selection_scope
        frozen_proof = manifest.get("proof", {}).get("frozen_source_proof")
        if frozen_proof and file_sha256(Path(frozen_proof["record_path"])) != frozen_proof["record_sha256"]:
            raise DataFoundationError("frozen full-history source record hash mismatch")
        proof["raw_inputs_reused"] = bool(frozen_proof)
        proof["frozen_source_proof"] = frozen_proof
        return path, {**proof, "manifest_path": str(manifest_path),
                      "quarantine_path": str(quarantine_path), "quarantined_rows": len(bad),
                      "quarantine_sha256": file_sha256(quarantine_path),
                      "manifest_sha256": file_sha256(manifest_path), "cache_hit": True}
    attempt = base / f"attempt={uuid4().hex}"
    attempt.mkdir()
    pieces, raw_artifacts = [], []
    try:
        frozen, source_proof = _frozen_raw_inputs(base, binding, segments) if reuse_frozen_inputs else ([], None)
        for i, (historical, query, first, last) in enumerate(segments):
            start, end = str(first.date()), str(last.date())
            raw = pd.read_parquet(frozen[i]) if frozen else fetcher(str(query), start, end)
            if raw is None or raw.empty:
                raise DataFoundationError(f"{security_id}: empty full history for {query} {start}..{end}")
            raw_path = attempt / f"alias-{i}.parquet"
            if frozen:
                shutil.copyfile(frozen[i], raw_path)
            else:
                raw.to_parquet(raw_path)
            raw_artifacts.append({"path": str(raw_path.relative_to(base)), "sha256": file_sha256(raw_path),
                                  "historical_ticker": historical, "query_ticker": query,
                                  "start": start, "end": end})
            work = raw.reset_index()
            if "date" not in work:
                work = work.rename(columns={work.columns[0]: "date"})
            dates = pd.to_datetime(work.date, errors="coerce")
            if dates.isna().any() or not dates.between(first, last).all():
                raise DataFoundationError(f"{security_id}: provider returned dates outside requested alias interval")
            work["security_id"], work["ticker"] = security_id, historical
            pieces.append(work)
        if not pieces:
            raise DataFoundationError(f"{security_id}: no approved historical alias intervals")
        frame = normalize_coverage_bars(pd.concat(pieces, ignore_index=True),
                                       target_session=target, ingestion_run_id=attempt.name,
                                       source="FMP_FULL_SECURITY_REPAIR")
        frame, bad = inherit_quarantine(frame, approved=approved_quarantine, previous=previous)
        quarantine_path = attempt / "quarantine.parquet"
        bad.to_parquet(quarantine_path, index=False)
        proof = validate_replacement(frame, security_id=security_id, previous=previous,
                                     recent=recent, aliases=aliases, target=target)
        proof["selection_scope"] = selection_scope
        proof["raw_inputs_reused"] = bool(frozen)
        proof["frozen_source_proof"] = source_proof
        path = attempt / "validated.parquet"
        frame.to_parquet(path, index=False)
        atomic_save_json({"contract": binding, "artifact": str(path.relative_to(base)),
                          "quarantine_artifact": str(quarantine_path.relative_to(base)),
                          "quarantine_sha256": file_sha256(quarantine_path),
                          "sha256": file_sha256(path), "raw_artifacts": raw_artifacts,
                          "proof": proof,
                          "fetched_at": (source_proof.get("fetched_at") if frozen else datetime.now(timezone.utc).isoformat()),
                          "validated_at": datetime.now(timezone.utc).isoformat()}, manifest_path)
        return path, {**proof, "manifest_path": str(manifest_path),
                      "quarantine_path": str(quarantine_path), "quarantined_rows": len(bad),
                      "quarantine_sha256": file_sha256(quarantine_path),
                      "manifest_sha256": file_sha256(manifest_path), "cache_hit": False}
    except Exception as exc:
        atomic_save_json({"contract": binding, "status": "FAIL", "error": str(exc),
                          "raw_artifacts": raw_artifacts}, attempt / "failure.json")
        if isinstance(exc, ValueError):
            raise DataFoundationError(f"{security_id}: full-history provider contract failed: {exc}") from exc
        raise


def refresh_canonical_sources(*, mapped, previous_overlap, security_ids, cache_dir,
                              contract, universe, symbols, refresh_start, target, fetcher):
    """Do not put bulk precision back into an authenticated full-source history."""
    frames, proofs = [], []
    for sid in sorted(security_ids):
        old = previous_overlap.loc[previous_overlap.security_id.eq(sid)]
        recent = mapped.loc[mapped.security_id.eq(sid)]
        aliases = coverage_alias_intervals(
            universe.loc[universe.security_id.eq(sid)], symbols,
            history_start=str(pd.Timestamp(refresh_start).date()), target_session=target,
        )
        # Keep retired securities' canonical history without requesting dates
        # outside their approved lifecycle on every future daily run.
        if aliases.empty:
            if not old.empty or not recent.empty:
                raise DataFoundationError(f"{sid}: recent prices exist outside approved alias window")
            continue
        path, proof = fetch_replacement(
            cache_dir=cache_dir, contract=contract, security_id=sid,
            universe=universe, symbols=symbols,
            previous=old, recent=recent,
            history_start=str(pd.Timestamp(refresh_start).date()), target=target, fetcher=fetcher,
            selection_scope="AUTHENTICATED_RECENT_WINDOW_SAME_CANONICAL_SOURCE",
        )
        frames.append(pd.read_parquet(path))
        proofs.append({"security_id": sid, **proof})
    if not frames:
        return mapped, proofs
    return pd.concat([mapped.loc[~mapped.security_id.isin(security_ids)], *frames], ignore_index=True), proofs


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
