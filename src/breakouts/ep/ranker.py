"""Separate observation eligibility from catalyst strength and price triggers."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .classifier import classify
from .models import ALGORITHM_VERSION, PremarketSnapshot


def evaluate(symbol: str, documents: list[dict[str, Any]], identity: dict[str, Any] | None,
             *, identity_status: str, include_etfs: bool) -> dict[str, Any]:
    hints = [classify(doc) for doc in documents]
    reasons = ["FULLTEXT_NOT_VERIFIED", "CATALYST_RELATION_UNVERIFIED", "PREMARKET_DATA_UNVERIFIED"]
    status = "EVIDENCE_PENDING"
    if identity is None:
        reasons.insert(0, identity_status)
    elif identity.get("asset_type") not in ({"STOCK", "ADR", "ETF"} if include_etfs else {"STOCK", "ADR"}):
        status = "EXCLUDED"
        reasons.insert(0, "SECURITY_TYPE_EXCLUDED")
    elif identity.get("exchange") not in {"NASDAQ", "NYSE", "AMEX"}:
        status = "EXCLUDED"
        reasons.insert(0, "EXCHANGE_EXCLUDED")
    elif not identity.get("is_actively_trading"):
        status = "EXCLUDED"
        reasons.insert(0, "INACTIVE_SECURITY")
    if all(hint["event_type_hint"] in {"UNKNOWN", "LEGAL_NOTICE"} for hint in hints):
        reasons.insert(0, "NO_SUPPORTED_CATALYST_HINT")
    financials = [{"document_id": doc["document_id"], "revision_id": doc["revision_id"],
                   **doc["payload"], "surprise_pct": None,
                   "reason": "EPS_BASIS_AND_ESTIMATE_ASOF_UNVERIFIED"}
                  for doc in documents if doc["kind"] == "CALENDAR"]
    return {"ticker": symbol, "algorithm_version": ALGORITHM_VERSION, "mode": "SHADOW",
            "family": "EP_WATCH", "family_semantics": "OBSERVATION_NOT_CONFIRMED_EP",
            "status": status, "reasons": reasons, "identity": identity,
            "identity_status": identity_status,
            "grade": None, "catalyst_quality": "UNASSESSED", "events": hints,
            "financials": financials, "premarket": asdict(PremarketSnapshot()),
            "trigger_status": "NOT_IMPLEMENTED", "delivery": "DISABLED_SHADOW_ONLY"}
