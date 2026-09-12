"""Audit exact source revisions without relaxing production scale authentication.

A terminal-date difference is only a candidate. Certification additionally
requires a complete canonical requery with an unchanged historical prefix.
This module never publishes datasets or changes an immutable parent.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

from src.data.broad_coverage import split_coverage_bar_quality
from src.data.foundation import DataFoundationError


REVISION_METHOD = "EXACT_TERMINAL_DATE_REVISION_FULL_PREFIX_V1"
PRICE_FIELDS = ("open", "high", "low", "close", "adj_close", "volume")
KEY_FIELDS = ("date", "security_id", "ticker")


def _frame(frame: pd.DataFrame) -> pd.DataFrame:
    missing = set((*KEY_FIELDS, *PRICE_FIELDS)) - set(frame.columns)
    if missing:
        raise DataFoundationError(f"revision input missing columns: {sorted(missing)}")
    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    if isinstance(out.date.dtype, pd.DatetimeTZDtype):
        raise DataFoundationError("revision daily dates must be timezone-naive")
    if out.date.isna().any() or not out.date.eq(out.date.dt.normalize()).all():
        raise DataFoundationError("revision input has invalid or non-daily dates")
    for key in ("security_id", "ticker"):
        if out[key].isna().any() or out[key].astype(str).str.strip().eq("").any():
            raise DataFoundationError(f"revision input has empty {key}")
        out[key] = out[key].astype(str)
    if out.duplicated(["date", "security_id"]).any():
        raise DataFoundationError("revision input has duplicate date/security keys")
    for field in PRICE_FIELDS:
        out[field] = pd.to_numeric(out[field], errors="coerce")
    return out.sort_values(["security_id", "date"]).reset_index(drop=True)


def _number(value):
    return None if pd.isna(value) else float(value)


def frame_fingerprint(frame: pd.DataFrame) -> str:
    work = _frame(frame)
    records = [
        {"date": row.date.date().isoformat(), "security_id": row.security_id,
         "ticker": row.ticker, **{field: _number(getattr(row, field)) for field in PRICE_FIELDS}}
        for row in work.itertuples(index=False)
    ]
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _invalid(frame: pd.DataFrame) -> bool:
    values = frame.loc[:, list(PRICE_FIELDS)].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        return True
    return not split_coverage_bar_quality(frame)[1].empty


def classify_revisions(previous: pd.DataFrame, fresh: pd.DataFrame, *,
                       parent_target, window_start, security_ids) -> list[dict]:
    """Keep missing/extra keys and historical aliases visible in the audit."""
    previous, fresh = _frame(previous), _frame(fresh)
    end, start = pd.Timestamp(parent_target), pd.Timestamp(window_start)
    if start > end:
        raise DataFoundationError("revision window starts after the parent target")
    ids = [str(value) for value in security_ids]
    if len(set(ids)) != len(ids):
        raise DataFoundationError("revision scope contains duplicate securities")
    old_window = previous.loc[previous.date.between(start, end)]
    new_window = fresh.loc[fresh.date.between(start, end)]
    old_groups = {str(sid): rows for sid, rows in old_window.groupby("security_id")}
    new_groups = {str(sid): rows for sid, rows in new_window.groupby("security_id")}
    invalid_ids = set(split_coverage_bar_quality(old_window)[1].security_id)
    invalid_ids.update(split_coverage_bar_quality(new_window)[1].security_id)
    empty = previous.iloc[:0]
    results = []
    for sid in ids:
        old, new = old_groups.get(sid, empty), new_groups.get(sid, empty)
        result = {"security_id": sid, "window_start": start.date().isoformat(),
                  "parent_target": end.date().isoformat(), "status": "BLOCKED",
                  "reasons": [], "old_rows": len(old), "fresh_rows": len(new),
                  "missing_dates": [], "extra_dates": [], "changes": []}
        if old.empty or new.empty:
            result["reasons"].append("MISSING_SECURITY_WINDOW")
        if sid in invalid_ids:
            result["reasons"].append("INVALID_SOURCE_BAR")
        if result["reasons"]:
            results.append(result)
            continue
        old, new = old.set_index("date"), new.set_index("date")
        result["ticker"] = str(new.iloc[-1].ticker)
        result["missing_dates"] = [d.date().isoformat() for d in old.index.difference(new.index)]
        result["extra_dates"] = [d.date().isoformat() for d in new.index.difference(old.index)]
        if result["missing_dates"] or result["extra_dates"]:
            result["reasons"].append("HISTORICAL_DATE_COVERAGE_CHANGED")
        common = old.index.intersection(new.index)
        if not old.loc[common, "ticker"].eq(new.loc[common, "ticker"]).all():
            result["reasons"].append("HISTORICAL_SYMBOL_CHANGED")
        if end not in common:
            result["reasons"].append("PARENT_TARGET_NOT_COVERED")
        for date in common:
            fields = {field: {"old": float(old.loc[date, field]), "new": float(new.loc[date, field])}
                      for field in PRICE_FIELDS if old.loc[date, field] != new.loc[date, field]}
            if fields:
                result["changes"].append({"date": date.date().isoformat(), "fields": fields})
        result["old_window_sha256"] = frame_fingerprint(old.reset_index())
        result["fresh_window_sha256"] = frame_fingerprint(new.reset_index())
        if not result["reasons"]:
            dates = {row["date"] for row in result["changes"]}
            if not dates:
                result["status"] = "UNCHANGED"
            elif dates != {end.date().isoformat()}:
                result["status"] = "FULL_HISTORY_REQUIRED"
                result["reasons"].append("EARLIER_OVERLAP_REVISION")
            elif len(common) < 2:
                result["reasons"].append("NO_UNCHANGED_ANCHOR")
            else:
                result["status"] = "LOCAL_REVISION_CANDIDATE"
        results.append(result)
    return results


def certify_local_revision(plan: dict, previous_full: pd.DataFrame,
                           fresh_window: pd.DataFrame, canonical_full: pd.DataFrame,
                           *, parent_target, window_start) -> dict:
    """Verify all retained dates, not just overlap, before certifying one correction."""
    old, fresh, canonical = [_frame(f) for f in (previous_full, fresh_window, canonical_full)]
    sid = str(plan["security_id"])
    for frame in (old, fresh, canonical):
        if frame.empty or set(frame.security_id) != {sid}:
            raise DataFoundationError("revision certificate has empty or foreign identity")
    observed = classify_revisions(old, fresh, parent_target=parent_target,
                                  window_start=window_start, security_ids=[sid])[0]
    if observed != plan or plan["status"] != "LOCAL_REVISION_CANDIDATE":
        raise DataFoundationError("revision plan differs from bound input rows or is not a candidate")
    if _invalid(canonical) or "unadjusted_close" not in canonical:
        raise DataFoundationError("canonical revision evidence has invalid bars or lacks nominal prices")
    nominal = pd.to_numeric(canonical.unadjusted_close, errors="coerce")
    if not np.isfinite(nominal).all() or nominal.le(0).any():
        raise DataFoundationError("canonical revision evidence lacks positive nominal prices")
    old, fresh, canonical = [frame.set_index("date") for frame in (old, fresh, canonical)]
    target = pd.Timestamp(parent_target)
    if old.index.max() > target or canonical.index.max() > fresh.index.max():
        raise DataFoundationError("revision evidence extends beyond the bound target")
    required = old.index.union(fresh.index)
    missing = required.difference(canonical.index)
    historical_extra = canonical.index[canonical.index <= target].difference(old.index)
    new_extra = canonical.index[canonical.index > target].difference(fresh.index)
    result = {"method": REVISION_METHOD, "security_id": sid, "status": "BLOCKED",
              "publishable": False, "reasons": [], "retained_rows_checked": 0,
              "missing_dates": [d.date().isoformat() for d in missing],
              "extra_historical_dates": [d.date().isoformat() for d in historical_extra],
              "extra_new_dates": [d.date().isoformat() for d in new_extra],
              "outside_revision_mismatches": [], "source_disagreements": []}
    if len(missing) or len(historical_extra) or len(new_extra):
        result["reasons"].append("FULL_HISTORY_DATE_COVERAGE_CHANGED")
    for date in old.index.intersection(canonical.index):
        if old.loc[date, "ticker"] != canonical.loc[date, "ticker"]:
            result["reasons"].append("HISTORICAL_SYMBOL_CHANGED")
            break
    prefix = old.index[old.index != target].intersection(canonical.index)
    result["retained_rows_checked"] = len(prefix)
    for date in prefix:
        fields = [field for field in PRICE_FIELDS if old.loc[date, field] != canonical.loc[date, field]]
        if "unadjusted_close" in old and pd.notna(old.loc[date, "unadjusted_close"]):
            if old.loc[date, "unadjusted_close"] != canonical.loc[date, "unadjusted_close"]:
                fields.append("unadjusted_close")
        if fields:
            result["outside_revision_mismatches"].append({"date": date.date().isoformat(), "fields": fields})
    if result["outside_revision_mismatches"]:
        result["reasons"].append("FULL_PREFIX_NOT_IDENTICAL")
    for date in fresh.index.intersection(canonical.index):
        if fresh.loc[date, "ticker"] != canonical.loc[date, "ticker"]:
            result["reasons"].append("SOURCE_SYMBOL_DISAGREEMENT")
        fields = [field for field in PRICE_FIELDS if fresh.loc[date, field] != canonical.loc[date, field]]
        if fields:
            result["source_disagreements"].append({"date": date.date().isoformat(), "fields": fields})
    if result["source_disagreements"]:
        result["reasons"].append("CANONICAL_AND_FROZEN_SOURCE_DISAGREE")
    result["reasons"] = sorted(set(result["reasons"]))
    if not result["reasons"]:
        result["status"] = "VERIFIED_LOCAL_REVISION"
    return result
