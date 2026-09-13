"""Version-bound whole-security isolation; never manufacture usable price rows."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pandas as pd

from src.data.foundation import DataFoundationError

POLICY = "BOUNDED_SECURITY_ISOLATION_V1"
MAX_RATIO = 0.001
MAX_COUNT = 10
REASON = "AUTHENTICATED_HISTORY_MISSING"


class MissingAuthenticatedHistory(DataFoundationError):
    """Only raised after identity, alias, numeric and price-source validation."""

    def __init__(self, security_id, dates):
        self.security_id = str(security_id)
        self.missing_dates = sorted({str(pd.Timestamp(d).date()) for d in dates})
        self.evidence = None
        super().__init__(f"{security_id}: full replacement loses {len(self.missing_dates)} "
                         f"authenticated dates: {self.missing_dates[:20]}")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def unavailable_ids(contract):
    return {row["security_id"] for row in (contract or {}).get("unavailable", [])}


def validate_availability(contract, universe, *, target_session=None):
    if contract is None:
        return set()
    if not isinstance(contract, dict) or contract.get("policy") != POLICY:
        raise DataFoundationError("unsupported security availability contract")
    ids = universe.security_id.astype(str).tolist()
    if not ids or len(ids) != len(set(ids)):
        raise DataFoundationError("availability requires a nonempty unique expected scope")
    payload = {k: v for k, v in contract.items() if k != "sha256"}
    if (contract.get("sha256") != digest(payload)
            or contract.get("expected_scope_sha256") != digest(sorted(ids))
            or contract.get("expected_count") != len(ids)):
        raise DataFoundationError("security availability hash/scope mismatch")
    if target_session is not None and contract.get("target_session") != str(pd.Timestamp(target_session).date()):
        raise DataFoundationError("security availability target mismatch")
    rows = contract.get("unavailable")
    if not isinstance(rows, list):
        raise DataFoundationError("missing security isolation ledger")
    try:
        isolated = unavailable_ids(contract)
        ratio, count = Decimal(str(contract["max_ratio"])), contract["max_count"]
        if (not ratio.is_finite() or not 0 <= ratio <= Decimal(str(MAX_RATIO))
                or type(count) is not int or not 0 <= count <= MAX_COUNT):
            raise ValueError("policy limits")
        if (len(isolated) != len(rows) or not isolated <= set(ids)
                or len(isolated) > count or Decimal(len(isolated)) > ratio * len(ids)):
            raise ValueError("isolation budget/scope exceeded")
        for row in rows:
            selected = universe.loc[universe.security_id.astype(str).eq(row["security_id"])].iloc[0]
            if (str(selected.get("coverage_role", "")).upper().startswith("BENCHMARK")
                    or str(selected.ticker).upper() in {"SPY", "QQQ", "IWM"}):
                raise ValueError("required benchmark cannot be isolated")
            if row["reason"] != REASON or row["ticker"] != str(selected.ticker):
                raise ValueError("isolation identity/reason mismatch")
            dates = row["missing_dates"]
            if (not dates or dates != sorted(set(dates))
                    or any(str(pd.Timestamp(d).date()) != d for d in dates)
                    or max(dates) > contract["target_session"]):
                raise ValueError("invalid missing-date evidence")
            if not row["last_good_version_id"] or len(row["last_good_manifest_sha256"]) != 64:
                raise ValueError("missing last-good binding")
            if (not row["evidence_path"] or len(row["evidence_sha256"]) != 64
                    or row["first_isolated_session"] > contract["target_session"]):
                raise ValueError("missing isolation evidence")
            evidence = row["evidence_record"]
            if (evidence["error_code"] != REASON or evidence["missing_dates"] != dates
                    or evidence["contract"]["security_id"] != row["security_id"]
                    or not evidence["raw_artifacts"]):
                raise ValueError("isolation evidence binding mismatch")
        if contract["status"] != ("DEGRADED" if rows else "READY"):
            raise ValueError("availability status mismatch")
    except (KeyError, TypeError, ValueError) as exc:
        raise DataFoundationError(f"invalid security availability: {exc}") from exc
    return isolated


def build_availability(universe, *, target_session, errors, previous=None,
                       restored_ids=(), parent_version_id, parent_manifest_sha256,
                       max_ratio=MAX_RATIO, max_count=MAX_COUNT):
    target = str(pd.Timestamp(target_session).date())
    restored = set(restored_ids)
    prior = {r["security_id"]: dict(r) for r in (previous or {}).get("unavailable", [])}
    if not restored <= prior.keys() or len({e["security_id"] for e in errors}) != len(errors):
        raise DataFoundationError("invalid restoration scope or duplicate isolation errors")
    rows = {sid: row for sid, row in prior.items() if sid not in restored}
    for error in errors:
        evidence = error.get("isolation_evidence")
        if error.get("error_code") != REASON or not evidence:
            raise DataFoundationError("non-isolatable history failure: " + error.get("error", "unknown"))
        sid = error["security_id"]
        old = prior.get(sid, {})
        rows[sid] = {"security_id": sid, "ticker": error["ticker"], "reason": REASON,
                     "missing_dates": error["missing_dates"],
                     "first_isolated_session": old.get("first_isolated_session", target),
                     "last_good_version_id": old.get("last_good_version_id", parent_version_id),
                     "last_good_manifest_sha256": old.get("last_good_manifest_sha256", parent_manifest_sha256),
                     "evidence_path": evidence["path"], "evidence_sha256": evidence["sha256"],
                     "evidence_record": evidence["record"]}
    result = {"policy": POLICY, "status": "DEGRADED" if rows else "READY", "target_session": target,
              "expected_count": len(universe), "expected_scope_sha256": digest(sorted(universe.security_id.astype(str))),
              "max_ratio": max_ratio, "max_count": max_count,
              "unavailable": [rows[sid] for sid in sorted(rows)], "restored_security_ids": sorted(restored)}
    result["sha256"] = digest(result)
    validate_availability(result, universe, target_session=target)
    return result


def availability_from_manifest(manifest):
    return (manifest.get("quality_lineage") or {}).get("security_availability")


def verify_consumer_availability(parent_manifest, consumer_manifest):
    expected = availability_from_manifest(parent_manifest)
    if consumer_manifest.get("security_availability") != expected:
        raise DataFoundationError("consumer security availability contract mismatch")
