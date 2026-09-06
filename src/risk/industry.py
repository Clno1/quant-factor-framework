"""Current industry exposure on total NAV, with explicit data comparability.

Current profile observations are suitable for current monitoring only. They
never become historical PIT exposures merely by being saved in a dated file.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import CONFIG
from src.research_universes import research_universe_registry


TAXONOMY = "FMP_SECTOR_V1"
UNKNOWN = "UNKNOWN"
CASH = "CASH"
METHOD = "industry_nav_exposure_v1"


def snapshot_root() -> Path:
    return CONFIG.abs_path("data/lake/industry_risk")


def load_snapshot(root: Path | None = None) -> dict[str, Any]:
    root = root or snapshot_root()
    pointer = root / "latest.json"
    if not pointer.exists():
        return {}
    binding = json.loads(pointer.read_text())
    name = str(binding["file"])
    if Path(name).name != name:
        raise ValueError("Invalid industry snapshot path")
    raw = (root / name).read_bytes()
    if hashlib.sha256(raw).hexdigest() != binding["sha256"]:
        raise ValueError("Industry snapshot checksum mismatch")
    data = json.loads(raw)
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported industry snapshot version")
    return data


def benchmark_for(account: dict) -> str | None:
    explicit = str(account.get("industry_benchmark") or "").strip().upper()
    if explicit:
        return explicit
    universe = str(account.get("universe") or "").upper()
    if universe.startswith("WATCHLIST:"):
        return None
    try:
        return research_universe_registry().get(universe).benchmark or None
    except (KeyError, ValueError):
        return None


def _number(value: Any, label: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return result


def _sector(value: Any) -> str:
    text = "" if value is None or pd.isna(value) else str(value).strip()
    return UNKNOWN if text.upper() in {"", "UNKNOWN", "N/A", "NONE", "UNCLASSIFIED", "CASH & OTHERS"} else text


def build_industry_report(
    account: dict,
    positions: pd.DataFrame,
    snapshot: dict,
    *,
    benchmark: str | None = None,
    now: datetime | None = None,
    max_age_days: int = 7,
) -> dict[str, Any]:
    """Calculate an observation-time report; do not reconstruct historical risk."""
    today = pd.Timestamp(now or datetime.now(timezone.utc)).date()
    issues: list[str] = []
    nav = _number(account.get("last_equity", account.get("initial_cash")), "NAV")
    if nav <= 0:
        raise ValueError("NAV must be positive")
    cash = _number(account.get("cash"), "cash")
    mark = account.get("last_mark_date")
    if not mark:
        issues.append("NO_VALUATION_DATE")
    elif not 0 <= (today - pd.Timestamp(mark).date()).days <= max_age_days:
        issues.append("STALE_VALUATION")
    observed = snapshot.get("observed_at")
    if not observed:
        issues.append("CLASSIFICATION_SNAPSHOT_MISSING")
    else:
        age = (today - pd.Timestamp(observed).date()).days
        if not 0 <= age <= max_age_days:
            issues.append("STALE_CLASSIFICATION")
        if mark and abs((pd.Timestamp(observed).date() - pd.Timestamp(mark).date()).days) > max_age_days:
            issues.append("CLASSIFICATION_VALUATION_DATE_GAP")
    if snapshot and snapshot.get("taxonomy") != TAXONOMY:
        issues.append("CLASSIFICATION_TAXONOMY_MISMATCH")

    classifications = snapshot.get("classifications") or {}
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    details = []
    seen: set[str] = set()
    for row in positions.to_dict("records"):
        ticker = str(row["ticker"]).strip().upper()
        if not ticker or ticker in seen:
            raise ValueError("Positions require unique non-empty tickers")
        seen.add(ticker)
        value = _number(row["market_value"], "position market value")
        if value == 0:
            continue
        profile = classifications.get(ticker) or {}
        sector = _sector(profile.get("sector")) if snapshot.get("taxonomy") == TAXONOMY else UNKNOWN
        totals[sector] = totals.get(sector, 0.0) + value
        counts[sector] = counts.get(sector, 0) + 1
        details.append({"ticker": ticker, "sector": sector, "market_value": value, "nav_weight": value / nav})
    invested = sum(totals.values())
    if not math.isclose(invested + cash, nav, rel_tol=1e-6, abs_tol=0.02):
        raise ValueError("Positions plus cash do not reconcile to NAV")
    unknown = totals.get(UNKNOWN, 0.0) / nav
    if unknown > 0:
        issues.append("UNKNOWN_PORTFOLIO_CLASSIFICATION")

    symbol = benchmark or benchmark_for(account)
    b = (snapshot.get("benchmarks") or {}).get(symbol) or {}
    weights: dict[str, float] = {}
    if not symbol:
        issues.append("BENCHMARK_NOT_CONFIGURED")
    elif not b:
        issues.append("BENCHMARK_SNAPSHOT_MISSING")
    else:
        if b.get("taxonomy") != snapshot.get("taxonomy"):
            issues.append("BENCHMARK_TAXONOMY_MISMATCH")
        if not b.get("source") or not b.get("observed_at"):
            issues.append("BENCHMARK_PROVENANCE_MISSING")
        else:
            age = (today - pd.Timestamp(b["observed_at"]).date()).days
            if not 0 <= age <= max_age_days:
                issues.append("STALE_BENCHMARK")
        if b.get("asof"):
            reference = pd.Timestamp(b["asof"]).date()
            if reference > today or (today - reference).days > max_age_days:
                issues.append("STALE_BENCHMARK_EFFECTIVE_DATE")
        for sector, value in (b.get("weights") or {}).items():
            key = _sector(sector)
            weights[key] = weights.get(key, 0.0) + _number(value, "benchmark weight")
        total = sum(weights.values())
        # No renormalization: incomplete provider coverage remains visible.
        if total > 1.001 or total < 0.999:
            issues.append("BENCHMARK_WEIGHT_TOTAL_INVALID")
        residual = max(0.0, 1.0 - total)
        if residual > 1e-12:
            weights[UNKNOWN] = weights.get(UNKNOWN, 0.0) + residual
        if weights.get(UNKNOWN, 0.0) > 0.001:
            issues.append("UNKNOWN_BENCHMARK_CLASSIFICATION")
    comparable = not issues
    weights = {key: value for key, value in weights.items() if value > 1e-12}
    sectors = set(totals) | set(weights) | {CASH}
    rows = []
    for sector in sorted(sectors):
        value = cash if sector == CASH else totals.get(sector, 0.0)
        weight = value / nav
        rows.append({
            "sector": sector, "count": counts.get(sector, 0), "market_value": value,
            "nav_weight": weight,
            "equity_weight": value / invested if invested > 0 and sector != CASH else None,
            "benchmark_weight": weights.get(sector, 0.0) if b else None,
            "active_weight_pp": (weight - weights.get(sector, 0.0)) * 100 if comparable else None,
        })
    rows.sort(key=lambda row: (-row["nav_weight"], row["sector"]))
    known_weights = [value / nav for sector, value in totals.items() if sector != UNKNOWN]
    stale = any("STALE" in issue or "DATE_GAP" in issue for issue in issues)
    return {
        "schema_version": 1, "methodology": METHOD,
        "status": "STALE" if stale else "READY" if comparable else "PARTIAL",
        "issues": issues, "account_id": account.get("id"), "account_name": account.get("name"),
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(), "valuation_date": mark,
        "nav": nav, "cash_weight": cash / nav, "equity_weight": invested / nav,
        "unknown_weight": unknown, "largest_known_sector_weight": max(known_weights, default=0.0),
        "classification_observed_at": observed, "classification_policy": "CURRENT_OBSERVATION_ONLY",
        "taxonomy": snapshot.get("taxonomy"), "benchmark": symbol,
        "benchmark_observed_at": b.get("observed_at"), "benchmark_asof": b.get("asof"),
        "benchmark_date_basis": b.get("date_basis"), "benchmark_source": b.get("source"),
        "comparable": comparable, "rows": rows, "positions": details,
    }


def paper_industry_report(account: dict, positions: pd.DataFrame) -> dict:
    """UI reads immutable observations only; never calls a provider."""
    try:
        return build_industry_report(account, positions, load_snapshot())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "UNAVAILABLE", "issues": [str(exc)], "rows": [], "comparable": False}
