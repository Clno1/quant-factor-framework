"""Audited historical-research policy for provider-limited securities."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


POLICY_SCHEMA_VERSION = 1
FULL_HISTORY = "FULL_HISTORY"
PROSPECTIVE_ONLY = "PROSPECTIVE_ONLY"
EXCLUDED_UNVERIFIABLE_HISTORY = "EXCLUDED_UNVERIFIABLE_HISTORY"
POLICY_COLUMNS = [
    "security_id",
    "current_ticker",
    "name",
    "trading_status",
    "policy",
    "effective_from",
    "reason_codes",
    "decision_source",
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def empty_history_policy() -> pd.DataFrame:
    """Return a typed empty frame with the immutable policy schema."""
    return pd.DataFrame(columns=POLICY_COLUMNS)


def load_research_history_policy(
    path: str | Path,
) -> tuple[dict[str, Any], Path, str]:
    """Load and strictly validate an explicitly approved policy registry."""
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(
            f"research history policy registry not found: {resolved}"
        )
    payload = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    if payload.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ValueError("research history policy schema version mismatch")
    if payload.get("universe") != "US_SECURITY_MASTER":
        raise ValueError("research history policy universe mismatch")
    activation = pd.to_datetime(
        payload.get("activation_session"), errors="coerce"
    )
    if pd.isna(activation):
        raise ValueError("research history policy activation_session is invalid")
    decision = payload.get("decision") or {}
    if not str(decision.get("approved_at") or "").strip():
        raise ValueError("research history policy decision.approved_at is required")
    if not str(decision.get("approved_by") or "").strip():
        raise ValueError("research history policy decision.approved_by is required")
    if not str(decision.get("basis") or "").strip():
        raise ValueError("research history policy decision.basis is required")

    entries = payload.get("entries") or []
    if not isinstance(entries, list):
        raise ValueError("research history policy entries must be a list")
    observed_ids: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("research history policy entry must be a mapping")
        security_id = str(item.get("security_id") or "").strip()
        ticker = str(item.get("current_ticker") or "").strip().upper()
        name = str(item.get("name") or "").strip()
        status = str(item.get("trading_status") or "").strip().upper()
        policy = str(item.get("policy") or "").strip().upper()
        effective = pd.to_datetime(item.get("effective_from"), errors="coerce")
        reasons = item.get("reason_codes") or []
        if not security_id.startswith("sec_") or security_id in observed_ids:
            raise ValueError("research history policy security_ids must be unique")
        if not ticker or not name:
            raise ValueError(f"{security_id}: ticker and name are required")
        if status not in {"ACTIVE", "INACTIVE", "DELISTED"}:
            raise ValueError(f"{security_id}: invalid trading_status")
        if policy not in {PROSPECTIVE_ONLY, EXCLUDED_UNVERIFIABLE_HISTORY}:
            raise ValueError(f"{security_id}: invalid history policy")
        if policy == PROSPECTIVE_ONLY and status != "ACTIVE":
            raise ValueError(f"{security_id}: PROSPECTIVE_ONLY must be ACTIVE")
        if policy == EXCLUDED_UNVERIFIABLE_HISTORY and status == "ACTIVE":
            raise ValueError(
                f"{security_id}: active security cannot use historical exclusion"
            )
        if pd.isna(effective) or effective.normalize() < activation.normalize():
            raise ValueError(f"{security_id}: invalid effective_from")
        if not isinstance(reasons, list) or not reasons or any(
            not str(value).strip() for value in reasons
        ):
            raise ValueError(f"{security_id}: reason_codes are required")
        observed_ids.add(security_id)
    return payload, resolved, _sha256(resolved)


def apply_research_history_policy(
    master: pd.DataFrame,
    symbols: pd.DataFrame,
    registry: dict[str, Any] | None,
    *,
    target_session: str | pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply exact policy entries and return sanitized symbols plus the ledger."""
    if not registry:
        return symbols.copy(), empty_history_policy()
    target = pd.Timestamp(target_session).normalize()
    decision = registry.get("decision") or {}
    decision_source = str(decision.get("basis") or "").strip()
    result = symbols.copy()
    ledger_rows: list[dict[str, Any]] = []

    for item in registry.get("entries") or []:
        security_id = str(item["security_id"])
        rows = master.loc[master["security_id"].astype(str).eq(security_id)]
        if len(rows) != 1:
            raise ValueError(
                f"{security_id}: expected one Security Master row, observed {len(rows)}"
            )
        row = rows.iloc[0]
        expected = {
            "current_ticker": str(item["current_ticker"]).strip().upper(),
            "name": str(item["name"]).strip(),
            "trading_status": str(item["trading_status"]).strip().upper(),
        }
        observed = {
            "current_ticker": str(row["current_ticker"]).strip().upper(),
            "name": str(row["name"]).strip(),
            "trading_status": str(row["trading_status"]).strip().upper(),
        }
        if observed != expected:
            raise ValueError(
                f"{security_id}: policy selector drifted: "
                f"expected={expected}, observed={observed}"
            )
        policy = str(item["policy"]).upper()
        effective = pd.Timestamp(item["effective_from"]).normalize()
        if effective > target:
            raise ValueError(
                f"{security_id}: policy effective_from exceeds target_session"
            )

        result = result.loc[
            ~result["security_id"].astype(str).eq(security_id)
        ].copy()
        if policy == PROSPECTIVE_ONLY:
            result = pd.concat([
                result,
                pd.DataFrame([{
                    "security_id": security_id,
                    "ticker": observed["current_ticker"],
                    "exchange": str(row.get("primary_exchange") or ""),
                    "effective_from": effective,
                    "effective_to": pd.NaT,
                    "is_primary": True,
                    "event_type": "PROSPECTIVE_ONLY_START",
                    "source": "RESEARCH_HISTORY_POLICY",
                    "source_asof": target,
                }]),
            ], ignore_index=True)
        ledger_rows.append({
            "security_id": security_id,
            "current_ticker": observed["current_ticker"],
            "name": observed["name"],
            "trading_status": observed["trading_status"],
            "policy": policy,
            "effective_from": effective,
            "reason_codes": ",".join(
                sorted({str(value).strip().upper() for value in item["reason_codes"]})
            ),
            "decision_source": decision_source,
        })
    ledger = pd.DataFrame(ledger_rows, columns=POLICY_COLUMNS)
    return result.reset_index(drop=True), ledger.sort_values(
        ["policy", "security_id"]
    ).reset_index(drop=True)


__all__ = [
    "EXCLUDED_UNVERIFIABLE_HISTORY",
    "FULL_HISTORY",
    "POLICY_COLUMNS",
    "PROSPECTIVE_ONLY",
    "apply_research_history_policy",
    "empty_history_policy",
    "load_research_history_policy",
]
