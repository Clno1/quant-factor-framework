#!/usr/bin/env python3
"""Build, audit and optionally publish the versioned US Security Master.

The default mode is intentionally candidate-only. Provider payloads are
normalized into immutable Parquet artifacts plus a redacted audit report, but
the DuckDB publication pointer moves only when ``--publish`` is explicit and
every source/completeness and Security Master quality gate passes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gc
import hashlib
import json
from pathlib import Path
import resource
import sys
import time
from typing import Any
import uuid

import pandas as pd
import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import CONFIG  # noqa: E402
from src.data.fmp import (  # noqa: E402
    get_company_profiles_bulk,
    get_delisted_companies,
    get_symbol_changes,
    infer_us_security_asset_type,
)
from src.data.security_master_store import (  # noqa: E402
    SecurityMasterCandidate,
    SecurityMasterStore,
    build_security_master_candidate,
)
from src.data.research_history_policy import (  # noqa: E402
    load_research_history_policy,
)
from src.utils.env import load_local_env  # noqa: E402
from src.utils.io import atomic_save_json  # noqa: E402
from src.utils.market_calendar import (  # noqa: E402
    latest_publishable_xnys_session,
)


SCHEMA_VERSION = 1
CORRECTION_SCHEMA_VERSION = 2
DEFAULT_AUDIT_DIR = ROOT / "outputs" / "data_audits"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = CONFIG.data.security_master
    parser = argparse.ArgumentParser(
        description="Build the versioned US Security Master",
    )
    parser.add_argument("--target-session")
    parser.add_argument("--history-start", default=str(settings.history_start))
    parser.add_argument(
        "--profile-part",
        action="append",
        type=int,
        dest="profile_parts",
        help="FMP profile-bulk part; repeat as needed (default: configured parts)",
    )
    parser.add_argument(
        "--symbol-change-limit",
        type=int,
        default=int(settings.symbol_change_limit),
    )
    parser.add_argument(
        "--delisted-page-size",
        type=int,
        default=int(settings.delisted_page_size),
    )
    parser.add_argument(
        "--delisted-max-pages",
        type=int,
        default=int(settings.delisted_max_pages),
    )
    parser.add_argument("--env-file", default=None)
    parser.add_argument(
        "--corrections",
        default=str(settings.corrections),
        help="reviewed Security Master symbol-transition registry",
    )
    parser.add_argument(
        "--history-policy",
        default=str(getattr(
            settings,
            "history_policy",
            "configs/research_history_policy.yaml",
        )),
        help="approved PROSPECTIVE_ONLY and historical-exclusion registry",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_AUDIT_DIR))
    parser.add_argument(
        "--source-dir",
        default=None,
        help="explicit immutable provider_sources directory from a prior run",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="advance the DuckDB pointer after all gates pass",
    )
    parser.add_argument(
        "--force-publish",
        action="store_true",
        help="rebuild an already published target session (manual repair only)",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _rss_mb() -> float:
    raw = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return raw / (1024.0 * 1024.0) if sys.platform == "darwin" else raw / 1024.0


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def load_security_master_corrections(
    path: str | Path,
) -> tuple[dict[str, Any], Path, str]:
    """Load a strict, source-backed symbol-transition registry."""
    resolved = _project_path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(
            f"Security Master correction registry not found: {resolved}"
        )
    payload = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    if payload.get("schema_version") != CORRECTION_SCHEMA_VERSION:
        raise ValueError("Security Master correction schema version mismatch")
    if payload.get("universe") != "US_SECURITY_MASTER":
        raise ValueError("Security Master correction registry universe mismatch")
    transitions = payload.get("reviewed_symbol_transitions") or []
    if not isinstance(transitions, list):
        raise ValueError("reviewed_symbol_transitions must be a list")
    continuity = payload.get("reviewed_provider_identifier_conflicts") or []
    if not isinstance(continuity, list):
        raise ValueError(
            "reviewed_provider_identifier_conflicts must be a list"
        )
    identifiers: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for item in transitions:
        if not isinstance(item, dict):
            raise ValueError("Security Master correction entry must be a mapping")
        correction_id = str(item.get("id") or "").strip()
        old_ticker = str(item.get("old_ticker") or "").strip().upper()
        new_ticker = str(item.get("new_ticker") or "").strip().upper()
        effective = pd.to_datetime(item.get("effective_date"), errors="coerce")
        sources = item.get("sources") or []
        if not correction_id or correction_id in identifiers:
            raise ValueError("Security Master correction IDs must be unique")
        if not old_ticker or not new_ticker or old_ticker == new_ticker:
            raise ValueError(f"{correction_id}: invalid ticker transition")
        if pd.isna(effective):
            raise ValueError(f"{correction_id}: invalid effective_date")
        if (old_ticker, new_ticker) in pairs:
            raise ValueError(f"duplicate reviewed transition: {old_ticker}->{new_ticker}")
        if not str(item.get("reason") or "").strip():
            raise ValueError(f"{correction_id}: reason is required")
        if not sources or any(
            not str(source).startswith("https://www.sec.gov/")
            for source in sources
        ):
            raise ValueError(
                f"{correction_id}: primary SEC source URLs are required"
            )
        for side in ("old_profile", "new_profile"):
            expectation = item.get(side)
            if not isinstance(expectation, dict) or not expectation.get(
                "name_contains"
            ):
                raise ValueError(
                    f"{correction_id}: {side}.name_contains is required"
                )
        lifecycle = item.get("provider_lifecycle")
        if lifecycle is not None:
            required = {
                "inactive_on_or_after",
                "delisted_exchange",
                "delisted_name_contains",
            }
            if not isinstance(lifecycle, dict) or required - set(lifecycle):
                raise ValueError(
                    f"{correction_id}: provider_lifecycle must bind an exact "
                    "provider delisting record"
                )
            inactive_on_or_after = pd.to_datetime(
                lifecycle["inactive_on_or_after"], errors="coerce"
            )
            if pd.isna(inactive_on_or_after) or inactive_on_or_after <= effective:
                raise ValueError(
                    f"{correction_id}: provider_lifecycle date must be after "
                    "the symbol transition"
                )
            if item["new_profile"].get("is_active") is not True:
                raise ValueError(
                    f"{correction_id}: provider_lifecycle requires an active "
                    "new_profile baseline"
                )
            if not all(
                str(lifecycle[field]).strip()
                for field in ("delisted_exchange", "delisted_name_contains")
            ):
                raise ValueError(
                    f"{correction_id}: provider_lifecycle evidence fields "
                    "cannot be empty"
                )
        identifiers.add(correction_id)
        pairs.add((old_ticker, new_ticker))
    continuity_pairs: set[tuple[str, str]] = set()
    for item in continuity:
        if not isinstance(item, dict):
            raise ValueError(
                "Security Master identifier-conflict entry must be a mapping"
            )
        correction_id = str(item.get("id") or "").strip()
        old_ticker = str(item.get("old_ticker") or "").strip().upper()
        new_ticker = str(item.get("new_ticker") or "").strip().upper()
        effective = pd.to_datetime(item.get("effective_date"), errors="coerce")
        sources = item.get("sources") or []
        if not correction_id or correction_id in identifiers:
            raise ValueError("Security Master correction IDs must be unique")
        if not old_ticker or not new_ticker or old_ticker == new_ticker:
            raise ValueError(f"{correction_id}: invalid ticker transition")
        if pd.isna(effective):
            raise ValueError(f"{correction_id}: invalid effective_date")
        if (old_ticker, new_ticker) in pairs:
            raise ValueError(
                f"{correction_id}: transition is already registered as a "
                "reviewed symbol transition"
            )
        if (old_ticker, new_ticker) in continuity_pairs:
            raise ValueError(
                f"duplicate reviewed identifier conflict: "
                f"{old_ticker}->{new_ticker}"
            )
        if item.get("decision") != "SAME_LISTED_ISSUE":
            raise ValueError(
                f"{correction_id}: decision must be SAME_LISTED_ISSUE"
            )
        if not str(item.get("reason") or "").strip():
            raise ValueError(f"{correction_id}: reason is required")
        if not sources or any(
            not str(source).startswith("https://www.sec.gov/")
            for source in sources
        ):
            raise ValueError(
                f"{correction_id}: primary SEC source URLs are required"
            )
        for side in ("old_profile", "new_profile"):
            expectation = item.get(side)
            required = {
                "name_contains", "asset_type", "exchange", "cik", "isin",
                "cusip", "listing_date", "is_active",
            }
            if not isinstance(expectation, dict) or required - set(expectation):
                raise ValueError(
                    f"{correction_id}: {side} must bind exact provider identity"
                )
        old_profile = dict(item["old_profile"])
        new_profile = dict(item["new_profile"])
        if bool(old_profile["is_active"]) or not bool(new_profile["is_active"]):
            raise ValueError(
                f"{correction_id}: continuity must replace one inactive ticker "
                "with one active ticker"
            )
        old_asset_type = str(old_profile["asset_type"]).strip().upper()
        new_asset_type = str(new_profile["asset_type"]).strip().upper()
        if old_asset_type != new_asset_type or old_asset_type not in {
            "STOCK", "ADR",
        }:
            raise ValueError(
                f"{correction_id}: continuity asset_type must be STOCK or ADR"
            )
        old_cik = "".join(
            char for char in str(old_profile["cik"]).upper()
            if char.isalnum()
        )
        new_cik = "".join(
            char for char in str(new_profile["cik"]).upper()
            if char.isalnum()
        )
        if not old_cik or old_cik != new_cik:
            raise ValueError(
                f"{correction_id}: continuity profiles must bind the same CIK"
            )
        event = item.get("provider_event")
        if not isinstance(event, dict) or not event.get("company_name_contains"):
            raise ValueError(
                f"{correction_id}: provider_event.company_name_contains is required"
            )
        identifiers.add(correction_id)
        continuity_pairs.add((old_ticker, new_ticker))
    return payload, resolved, _file_sha256(resolved)


def _profile_value(value: Any) -> str:
    return "" if value is None or pd.isna(value) else str(value).strip()


def _verify_reviewed_profile(
    profiles: pd.DataFrame,
    *,
    ticker: str,
    expectation: dict[str, Any],
    correction_id: str,
) -> pd.Series:
    rows = profiles.loc[
        profiles["ticker"].fillna("").astype(str).str.upper().eq(ticker)
    ]
    if len(rows) != 1:
        raise ValueError(
            f"{correction_id}: expected one provider profile for {ticker}, "
            f"observed {len(rows)}"
        )
    row = rows.iloc[0]
    name_contains = str(expectation.get("name_contains") or "").strip().upper()
    if name_contains not in _profile_value(row.get("name")).upper():
        raise ValueError(f"{correction_id}: {ticker} provider name drifted")
    for field in ("cik", "isin", "cusip"):
        if field not in expectation:
            continue
        expected = "".join(
            char for char in str(expectation.get(field) or "").upper()
            if char.isalnum()
        )
        observed = "".join(
            char for char in _profile_value(row.get(field)).upper()
            if char.isalnum()
        )
        if observed != expected:
            raise ValueError(
                f"{correction_id}: {ticker} provider {field} drifted"
            )
    if "is_active" in expectation and bool(row.get("is_active")) != bool(
        expectation["is_active"]
    ):
        raise ValueError(
            f"{correction_id}: {ticker} provider is_active drifted"
        )
    for field in ("asset_type", "exchange"):
        if field not in expectation:
            continue
        if _profile_value(row.get(field)).upper() != _profile_value(
            expectation[field]
        ).upper():
            raise ValueError(
                f"{correction_id}: {ticker} provider {field} drifted"
            )
    if "listing_date" in expectation:
        observed_date = pd.to_datetime(row.get("listing_date"), errors="coerce")
        expected_date = pd.to_datetime(
            expectation["listing_date"], errors="coerce"
        )
        if (
            pd.isna(observed_date)
            or pd.isna(expected_date)
            or pd.Timestamp(observed_date).normalize()
            != pd.Timestamp(expected_date).normalize()
        ):
            raise ValueError(
                f"{correction_id}: {ticker} provider listing_date drifted"
            )
    return row


def apply_reviewed_provider_identifier_conflicts(
    profiles: pd.DataFrame,
    changes: pd.DataFrame,
    registry: dict[str, Any],
    *,
    target_session: pd.Timestamp,
) -> tuple[set[tuple[str, str, pd.Timestamp]], list[dict[str, Any]]]:
    """Approve exact SEC-backed continuity without rewriting provider IDs."""
    reviewed_edges: set[tuple[str, str, pd.Timestamp]] = set()
    audit: list[dict[str, Any]] = []
    for item in registry.get("reviewed_provider_identifier_conflicts") or []:
        correction_id = str(item["id"])
        effective = pd.Timestamp(item["effective_date"]).normalize()
        old_ticker = str(item["old_ticker"]).strip().upper()
        new_ticker = str(item["new_ticker"]).strip().upper()
        if effective > target_session:
            audit.append({
                "id": correction_id,
                "action": "FUTURE_NOT_APPLIED",
                "effective_date": effective.date().isoformat(),
                "old_ticker": old_ticker,
                "new_ticker": new_ticker,
            })
            continue
        old_row = _verify_reviewed_profile(
            profiles,
            ticker=old_ticker,
            expectation=dict(item["old_profile"]),
            correction_id=correction_id,
        )
        new_row = _verify_reviewed_profile(
            profiles,
            ticker=new_ticker,
            expectation=dict(item["new_profile"]),
            correction_id=correction_id,
        )
        old_cik = _profile_value(old_row.get("cik"))
        new_cik = _profile_value(new_row.get("cik"))
        if not old_cik or old_cik != new_cik:
            raise ValueError(
                f"{correction_id}: reviewed profiles no longer share CIK"
            )
        conflicts = {
            field.upper(): {
                "old": _profile_value(old_row.get(field)).upper(),
                "new": _profile_value(new_row.get(field)).upper(),
            }
            for field in ("cusip", "isin")
            if _profile_value(old_row.get(field))
            and _profile_value(new_row.get(field))
            and _profile_value(old_row.get(field)).upper()
            != _profile_value(new_row.get(field)).upper()
        }
        if not conflicts:
            raise ValueError(
                f"{correction_id}: provider identifier conflict no longer exists"
            )
        pair = changes.loc[
            changes["old_ticker"].fillna("").astype(str).str.upper().eq(
                old_ticker
            )
            & changes["new_ticker"].fillna("").astype(str).str.upper().eq(
                new_ticker
            )
        ].copy()
        exact = pair.loc[
            pd.to_datetime(pair["date"], errors="coerce").dt.normalize().eq(
                effective
            )
        ]
        if len(exact) != 1:
            raise ValueError(
                f"{correction_id}: expected one exact provider transition, "
                f"observed {len(exact)}"
            )
        company_contains = str(
            item["provider_event"]["company_name_contains"]
        ).strip().upper()
        if company_contains not in _profile_value(
            exact.iloc[0].get("company_name")
        ).upper():
            raise ValueError(
                f"{correction_id}: provider event company_name drifted"
            )
        edge = (old_ticker, new_ticker, effective)
        reviewed_edges.add(edge)
        audit.append({
            "id": correction_id,
            "action": "SEC_CONTINUITY_APPROVED",
            "decision": "SAME_LISTED_ISSUE",
            "effective_date": effective.date().isoformat(),
            "old_ticker": old_ticker,
            "new_ticker": new_ticker,
            "shared_cik": old_cik,
            "provider_identifier_conflicts": conflicts,
            "provider_values_preserved": True,
            "reason": str(item["reason"]),
            "sources": [str(source) for source in item["sources"]],
        })
    return reviewed_edges, audit


def apply_reviewed_symbol_transitions(
    profiles: pd.DataFrame,
    changes: pd.DataFrame,
    registry: dict[str, Any],
    *,
    target_session: pd.Timestamp,
    delisted: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Add only exact, reviewed symbol transitions omitted by the provider."""
    target_session = pd.Timestamp(target_session).normalize()
    corrected = changes.copy()
    audit: list[dict[str, Any]] = []
    for item in registry.get("reviewed_symbol_transitions") or []:
        correction_id = str(item["id"])
        effective = pd.Timestamp(item["effective_date"]).normalize()
        old_ticker = str(item["old_ticker"]).strip().upper()
        new_ticker = str(item["new_ticker"]).strip().upper()
        if effective > target_session:
            audit.append({
                "id": correction_id,
                "action": "FUTURE_NOT_APPLIED",
                "effective_date": effective.date().isoformat(),
                "old_ticker": old_ticker,
                "new_ticker": new_ticker,
            })
            continue
        old_row = _verify_reviewed_profile(
            profiles,
            ticker=old_ticker,
            expectation=dict(item["old_profile"]),
            correction_id=correction_id,
        )
        new_expectation = dict(item["new_profile"])
        lifecycle_audit: dict[str, Any] | None = None
        lifecycle = item.get("provider_lifecycle")
        if lifecycle is not None:
            inactive_on_or_after = pd.Timestamp(
                lifecycle["inactive_on_or_after"]
            ).normalize()
            if target_session >= inactive_on_or_after:
                if delisted is None or delisted.empty:
                    raise ValueError(
                        f"{correction_id}: provider delisting evidence is required"
                    )
                required_columns = {
                    "ticker", "name", "exchange", "delisted_date",
                }
                if missing := required_columns - set(delisted.columns):
                    raise ValueError(
                        f"{correction_id}: provider delisting evidence is "
                        f"missing columns {sorted(missing)}"
                    )
                ticker_rows = delisted.loc[
                    delisted["ticker"].fillna("").astype(str).str.upper().eq(
                        new_ticker
                    )
                ].copy()
                dates = pd.to_datetime(
                    ticker_rows["delisted_date"], errors="coerce"
                ).dt.normalize()
                exact = ticker_rows.loc[dates.eq(inactive_on_or_after)].copy()
                expected_exchange = _profile_value(
                    lifecycle["delisted_exchange"]
                ).upper()
                expected_name = _profile_value(
                    lifecycle["delisted_name_contains"]
                ).upper()
                exact = exact.loc[
                    exact["exchange"].fillna("").astype(str).str.upper().eq(
                        expected_exchange
                    )
                    & exact["name"].fillna("").astype(str).str.upper().str.contains(
                        expected_name,
                        regex=False,
                    )
                ]
                if len(exact) != 1:
                    raise ValueError(
                        f"{correction_id}: exact provider delisting record drifted"
                    )
                new_expectation["is_active"] = False
                lifecycle_audit = {
                    "inactive_on_or_after": (
                        inactive_on_or_after.date().isoformat()
                    ),
                    "delisted_exchange": expected_exchange,
                    "delisted_name": _profile_value(exact.iloc[0].get("name")),
                    "provider_status": "INACTIVE",
                }
        new_row = _verify_reviewed_profile(
            profiles,
            ticker=new_ticker,
            expectation=new_expectation,
            correction_id=correction_id,
        )
        shared_keys = [
            key.upper()
            for key in ("cusip", "isin")
            if _profile_value(old_row.get(key))
            and _profile_value(old_row.get(key)).upper()
            == _profile_value(new_row.get(key)).upper()
        ]
        if not shared_keys:
            raise ValueError(
                f"{correction_id}: reviewed pair no longer shares CUSIP or ISIN"
            )
        pair = corrected.loc[
            corrected["old_ticker"].fillna("").astype(str).str.upper().eq(
                old_ticker
            )
            & corrected["new_ticker"].fillna("").astype(str).str.upper().eq(
                new_ticker
            )
        ].copy()
        if not pair.empty:
            dates = pd.to_datetime(pair["date"], errors="coerce").dt.normalize()
            if not dates.eq(effective).any():
                raise ValueError(
                    f"{correction_id}: provider transition date drifted"
                )
            action = "PROVIDER_EVENT_PRESENT"
        else:
            corrected = pd.concat([
                corrected,
                pd.DataFrame([{
                    "date": effective,
                    "old_ticker": old_ticker,
                    "new_ticker": new_ticker,
                    "company_name": _profile_value(new_row.get("name")),
                }]),
            ], ignore_index=True)
            action = "REVIEWED_EVENT_ADDED"
        audit.append({
            "id": correction_id,
            "action": action,
            "effective_date": effective.date().isoformat(),
            "old_ticker": old_ticker,
            "new_ticker": new_ticker,
            "shared_keys": shared_keys,
            "provider_lifecycle": lifecycle_audit,
            "reason": str(item["reason"]),
            "sources": [str(source) for source in item["sources"]],
        })
    corrected["date"] = pd.to_datetime(
        corrected["date"], errors="coerce"
    ).dt.normalize()
    return (
        corrected.drop_duplicates(
            ["date", "old_ticker", "new_ticker"], keep="last"
        ).sort_values(["date", "old_ticker", "new_ticker"]).reset_index(
            drop=True
        ),
        audit,
    )


def _frame_sha256(frame: pd.DataFrame) -> str:
    if frame.empty:
        return hashlib.sha256(b"").hexdigest()
    columns = sorted(str(column) for column in frame.columns)
    digest = hashlib.sha256("\x1f".join(columns).encode("utf-8"))
    for offset in range(0, len(frame), 5_000):
        chunk = frame.iloc[offset:offset + 5_000].loc[:, columns]
        payload = pd.util.hash_pandas_object(
            chunk.astype(str),
            index=True,
        ).to_numpy().tobytes()
        digest.update(payload)
    return digest.hexdigest()


def _load_delisted_history(
    *,
    history_start: pd.Timestamp,
    target_session: pd.Timestamp,
    page_size: int,
    max_pages: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load enough descending delisting pages to prove the history boundary."""
    if not 1 <= int(page_size) <= 100:
        raise ValueError("delisted-page-size must be between 1 and 100")
    if int(max_pages) < 1:
        raise ValueError("delisted-max-pages must be positive")

    frames: list[pd.DataFrame] = []
    stop_reason = "max_pages"
    for page in range(int(max_pages)):
        frame = get_delisted_companies(page=page, limit=int(page_size))
        if frame.empty:
            stop_reason = "empty_page"
            break
        frames.append(frame)
        valid_dates = pd.to_datetime(
            frame["delisted_date"], errors="coerce"
        ).dropna()
        if len(frame) < int(page_size):
            stop_reason = "short_page"
            break
        # FMP documents this endpoint as newest-first. Requiring the newest
        # date on an entire page to precede the boundary avoids stopping on a
        # single stray old row in an otherwise recent page.
        if not valid_dates.empty and valid_dates.max().normalize() < history_start:
            stop_reason = "history_start_reached"
            break

    combined = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=[
            "ticker", "name", "exchange", "ipo_date", "delisted_date",
        ])
    )
    combined = combined.drop_duplicates(
        ["ticker", "delisted_date"], keep="last"
    )
    valid_dates = pd.to_datetime(
        combined.get("delisted_date"), errors="coerce"
    ).dropna()
    oldest_loaded = valid_dates.min().normalize() if not valid_dates.empty else None
    endpoint_exhausted = stop_reason in {"empty_page", "short_page"}
    history_boundary_reached = bool(
        endpoint_exhausted
        or (
            oldest_loaded is not None
            and oldest_loaded <= history_start
        )
    )
    retained_dates = pd.to_datetime(
        combined["delisted_date"], errors="coerce"
    )
    retained = combined.loc[
        retained_dates.ge(history_start) & retained_dates.le(target_session)
    ].copy()
    retained = retained.sort_values(
        ["delisted_date", "ticker"], ascending=[False, True]
    ).reset_index(drop=True)
    diagnostics = {
        "pages_requested": int(len(frames) + (1 if stop_reason == "empty_page" else 0)),
        "pages_with_rows": int(len(frames)),
        "page_size": int(page_size),
        "max_pages": int(max_pages),
        "stop_reason": stop_reason,
        "rows_loaded": int(len(combined)),
        "rows_since_history_start": int(len(retained)),
        "oldest_loaded": (
            oldest_loaded.date().isoformat() if oldest_loaded is not None else None
        ),
        "history_boundary_reached": history_boundary_reached,
    }
    return retained, diagnostics


def _prepare_research_scope(
    profiles: pd.DataFrame,
    changes: pd.DataFrame,
    delisted: pd.DataFrame,
    *,
    history_start: pd.Timestamp,
    target_session: pd.Timestamp,
    reviewed_transition_tickers: set[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Limit global FMP directories to the approved US-equity V1 scope."""
    profiles = profiles.copy()
    profiles["asset_type"] = [
        infer_us_security_asset_type(
            ticker=row.ticker,
            name=row.name,
            is_adr=bool(getattr(row, "is_adr", False)),
            is_etf=bool(getattr(row, "is_etf", False)),
            is_fund=bool(getattr(row, "is_fund", False)),
        )
        for row in profiles.itertuples(index=False)
    ]
    scoped_changes = changes.loc[
        pd.to_datetime(changes["date"], errors="coerce").between(
            history_start, target_session
        )
    ].copy()
    reviewed_tickers = {
        str(value).strip().upper()
        for value in (reviewed_transition_tickers or set())
    }
    main_exchange = profiles["exchange"].isin({"NASDAQ", "NYSE", "AMEX"})
    admitted_exchange = main_exchange | profiles["ticker"].isin(
        reviewed_tickers
    )
    event_tickers = set(scoped_changes["old_ticker"].astype(str)) | set(
        scoped_changes["new_ticker"].astype(str)
    )
    delisted_tickers = set(delisted["ticker"].astype(str))
    listing_dates = pd.to_datetime(profiles["listing_date"], errors="coerce")
    active = profiles["is_active"].fillna(False).astype(bool)
    asset_allowed = profiles["asset_type"].isin({"STOCK", "ADR"})
    benchmark = profiles["ticker"].isin({"SPY", "QQQ", "IWM"})
    historical = (
        listing_dates.ge(history_start)
        | profiles["ticker"].isin(event_tickers)
        | profiles["ticker"].isin(delisted_tickers)
    )
    scoped_profiles = profiles.loc[
        admitted_exchange
        & ((asset_allowed & (active | historical)) | benchmark)
    ].copy()

    # Keep only symbol-change ancestry that can be reached from a retained
    # profile. Repeat because a predecessor may itself have a predecessor.
    reachable = set(scoped_profiles["ticker"].astype(str))
    while True:
        relevant = scoped_changes["new_ticker"].isin(reachable)
        predecessors = set(scoped_changes.loc[relevant, "old_ticker"].astype(str))
        expanded = reachable | predecessors
        if expanded == reachable:
            break
        reachable = expanded
    scoped_changes = scoped_changes.loc[
        scoped_changes["new_ticker"].isin(reachable)
    ].copy()
    diagnostics = {
        "profile_rows_before_scope": int(len(profiles)),
        "profile_rows_after_scope": int(len(scoped_profiles)),
        "active_stock_rows_after_scope": int(
            (
                scoped_profiles["is_active"].fillna(False).astype(bool)
                & scoped_profiles["asset_type"].eq("STOCK")
            ).sum()
        ),
        "symbol_change_rows_before_scope": int(len(changes)),
        "symbol_change_rows_after_scope": int(len(scoped_changes)),
        "history_start": history_start.date().isoformat(),
        "target_session": target_session.date().isoformat(),
        "included_asset_types": ["STOCK", "ADR", "BENCHMARK_ETF"],
        "excluded_instrument_rows": int(
            profiles["asset_type"].isin({
                "UNIT", "WARRANT", "RIGHT", "PREFERRED", "NOTE", "TEMPORARY",
            }).sum()
        ),
    }
    return (
        scoped_profiles.reset_index(drop=True),
        scoped_changes.reset_index(drop=True),
        diagnostics,
    )


def _source_summary(frame: pd.DataFrame, *, date_columns: tuple[str, ...] = ()) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "rows": int(len(frame)),
        "columns": sorted(str(column) for column in frame.columns),
        "frame_sha256": _frame_sha256(frame),
    }
    if "ticker" in frame.columns:
        summary["unique_tickers"] = int(frame["ticker"].nunique(dropna=True))
    for column in date_columns:
        if column not in frame.columns:
            continue
        values = pd.to_datetime(frame[column], errors="coerce").dropna()
        if values.empty:
            continue
        summary[f"{column}_min"] = values.min().date().isoformat()
        summary[f"{column}_max"] = values.max().date().isoformat()
    return summary


def _source_failures(
    *,
    profiles: pd.DataFrame,
    changes: pd.DataFrame,
    delisted_diagnostics: dict[str, Any],
    history_start: pd.Timestamp,
) -> list[str]:
    failures: list[str] = []
    if profiles.empty:
        failures.append("profile bulk is empty")
    if changes.empty:
        failures.append("symbol-change history is empty")
    else:
        oldest_change = pd.to_datetime(changes["date"], errors="coerce").min()
        if pd.isna(oldest_change) or oldest_change.normalize() > history_start:
            failures.append("symbol-change history does not reach history_start")
    if not bool(delisted_diagnostics.get("history_boundary_reached")):
        failures.append("delisted history does not reach history_start")
    return failures


def _candidate_dir(output_dir: Path, target: pd.Timestamp) -> Path:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return output_dir / "security_master_candidates" / f"asof={target.date()}" / f"run={run_id}_{uuid.uuid4().hex[:8]}"


def _write_provider_sources(
    *,
    directory: Path,
    profiles: pd.DataFrame,
    changes: pd.DataFrame,
    delisted: pd.DataFrame,
    delisted_diagnostics: dict[str, Any],
    target_session: pd.Timestamp,
    history_start: pd.Timestamp,
) -> dict[str, Any]:
    source_root = directory / "provider_sources"
    source_root.mkdir(parents=True, exist_ok=False)
    frames = {
        "profiles": profiles,
        "symbol_changes": changes,
        "delisted": delisted,
    }
    artifacts: dict[str, dict[str, Any]] = {}
    for name, frame in frames.items():
        path = source_root / f"{name}.parquet"
        frame.to_parquet(path, index=False)
        artifacts[name] = {
            "file": path.name,
            "rows": int(len(frame)),
            "sha256": _file_sha256(path),
        }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source": "FMP_SECURITY_MASTER_INPUTS",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target_session": target_session.date().isoformat(),
        "history_start": history_start.date().isoformat(),
        "artifacts": artifacts,
        "delisted_diagnostics": delisted_diagnostics,
        "api_key_value_recorded": False,
    }
    manifest_path = source_root / "manifest.json"
    atomic_save_json(manifest, manifest_path)
    return {
        "path": str(source_root),
        "manifest_path": str(manifest_path),
        "manifest_sha256": _file_sha256(manifest_path),
        "artifacts": artifacts,
    }


def _load_provider_sources(
    source_dir: Path,
    *,
    target_session: pd.Timestamp,
    history_start: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any], dict[str, Any]]:
    source_root = source_dir.resolve()
    if (source_root / "provider_sources").is_dir():
        source_root = source_root / "provider_sources"
    manifest_path = source_root / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"provider source manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("provider source schema version mismatch")
    if manifest.get("target_session") != target_session.date().isoformat():
        raise RuntimeError("provider source target_session mismatch")
    if manifest.get("history_start") != history_start.date().isoformat():
        raise RuntimeError("provider source history_start mismatch")
    frames: dict[str, pd.DataFrame] = {}
    for name in ("profiles", "symbol_changes", "delisted"):
        artifact = (manifest.get("artifacts") or {}).get(name) or {}
        path = source_root / str(artifact.get("file") or "")
        if not path.is_file() or _file_sha256(path) != artifact.get("sha256"):
            raise RuntimeError(f"provider source {name} hash verification failed")
        frame = pd.read_parquet(path)
        if len(frame) != int(artifact.get("rows") or -1):
            raise RuntimeError(f"provider source {name} row-count mismatch")
        frames[name] = frame
    source_record = {
        "path": str(source_root),
        "manifest_path": str(manifest_path),
        "manifest_sha256": _file_sha256(manifest_path),
        "artifacts": manifest["artifacts"],
    }
    return (
        frames["profiles"],
        frames["symbol_changes"],
        frames["delisted"],
        dict(manifest.get("delisted_diagnostics") or {}),
        source_record,
    )


def _write_candidate(
    candidate: SecurityMasterCandidate,
    *,
    directory: Path,
) -> dict[str, dict[str, Any]]:
    directory.mkdir(parents=True, exist_ok=True)
    frames = {
        "master": candidate.master,
        "symbols": candidate.symbols,
        "classifications": candidate.classifications,
        "identity_keys": candidate.identity_keys,
        "history_policy": candidate.history_policy,
    }
    artifacts: dict[str, dict[str, Any]] = {}
    for name, frame in frames.items():
        path = directory / f"{name}.parquet"
        frame.to_parquet(path, index=False)
        artifacts[name] = {
            "path": str(path),
            "rows": int(len(frame)),
            "sha256": _file_sha256(path),
        }
    return artifacts


def run_build(
    *,
    target_session: pd.Timestamp,
    history_start: pd.Timestamp,
    profile_parts: list[int],
    symbol_change_limit: int,
    delisted_page_size: int,
    delisted_max_pages: int,
    output_dir: Path,
    publish: bool,
    corrections_path: Path,
    history_policy_path: Path,
    source_dir: Path | None = None,
) -> tuple[dict[str, Any], int]:
    started = time.perf_counter()
    settings = CONFIG.data.security_master
    store = SecurityMasterStore(
        CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)),
        CONFIG.abs_path(str(settings.snapshot_dir)),
    )
    try:
        previous_generation, previous_frames = store.load_published()
    except FileNotFoundError:
        previous_generation, previous_frames = None, {}

    candidate_path = _candidate_dir(output_dir, target_session)
    candidate_path.mkdir(parents=True, exist_ok=False)
    source_started = time.perf_counter()
    if source_dir is not None:
        (
            profiles,
            changes,
            delisted,
            delisted_diagnostics,
            provider_sources,
        ) = _load_provider_sources(
            source_dir,
            target_session=target_session,
            history_start=history_start,
        )
        source_mode = "REUSED_IMMUTABLE"
    else:
        profiles = get_company_profiles_bulk(parts=profile_parts)
        changes = get_symbol_changes(limit=int(symbol_change_limit))
        delisted, delisted_diagnostics = _load_delisted_history(
            history_start=history_start,
            target_session=target_session,
            page_size=int(delisted_page_size),
            max_pages=int(delisted_max_pages),
        )
        provider_sources = _write_provider_sources(
            directory=candidate_path,
            profiles=profiles,
            changes=changes,
            delisted=delisted,
            delisted_diagnostics=delisted_diagnostics,
            target_session=target_session,
            history_start=history_start,
        )
        source_mode = "FETCHED_AND_FROZEN"
    profile_source = _source_summary(
        profiles, date_columns=("listing_date",)
    )
    change_source = _source_summary(changes, date_columns=("date",))
    source_failures = _source_failures(
        profiles=profiles,
        changes=changes,
        delisted_diagnostics=delisted_diagnostics,
        history_start=history_start,
    )
    correction_registry, registry_path, registry_sha256 = (
        load_security_master_corrections(corrections_path)
    )
    (
        history_policy_registry,
        resolved_history_policy_path,
        history_policy_registry_sha256,
    ) = load_research_history_policy(history_policy_path)
    changes, correction_audit = apply_reviewed_symbol_transitions(
        profiles,
        changes,
        correction_registry,
        target_session=target_session,
        delisted=delisted,
    )
    reviewed_symbol_continuity = {
        (
            str(item["old_ticker"]).upper(),
            str(item["new_ticker"]).upper(),
            pd.Timestamp(item["effective_date"]).normalize(),
        )
        for item in correction_audit
    }
    (
        reviewed_identity_continuity,
        identifier_conflict_audit,
    ) = apply_reviewed_provider_identifier_conflicts(
        profiles,
        changes,
        correction_registry,
        target_session=target_session,
    )
    profiles, changes, scope_diagnostics = _prepare_research_scope(
        profiles,
        changes,
        delisted,
        history_start=history_start,
        target_session=target_session,
        reviewed_transition_tickers={
            ticker
            for old_ticker, new_ticker, _event_date in reviewed_symbol_continuity
            for ticker in (old_ticker, new_ticker)
        },
    )
    gc.collect()
    source_duration = time.perf_counter() - source_started

    build_started = time.perf_counter()
    candidate = build_security_master_candidate(
        profiles,
        symbol_changes=changes,
        delisted_companies=delisted,
        target_session=target_session,
        previous_identity_keys=previous_frames.get("identity_keys"),
        previous_master=previous_frames.get("master"),
        previous_symbols=previous_frames.get("symbols"),
        previous_classifications=previous_frames.get("classifications"),
        minimum_active_stocks=int(settings.minimum_active_stocks),
        minimum_name_coverage=float(settings.minimum_name_coverage),
        minimum_classification_coverage=float(
            settings.minimum_classification_coverage
        ),
        research_history_policy=history_policy_registry,
        reviewed_identity_continuity=(
            reviewed_identity_continuity | reviewed_symbol_continuity
        ),
    )
    build_duration = time.perf_counter() - build_started
    if source_failures:
        candidate.quality["source_failures"] = source_failures
        candidate.quality["failures"] = [
            *candidate.quality.get("failures", []),
            *source_failures,
        ]
        candidate.quality["status"] = "FAIL"
    candidate.quality["reviewed_symbol_transitions"] = correction_audit
    candidate.quality["reviewed_provider_identifier_conflicts"] = (
        identifier_conflict_audit
    )
    candidate.quality["correction_registry_sha256"] = registry_sha256
    candidate.quality["history_policy_registry_sha256"] = (
        history_policy_registry_sha256
    )

    artifacts = _write_candidate(candidate, directory=candidate_path)
    generation = None
    publication_withheld = bool(
        publish and candidate.quality.get("status") != "PASS"
    )
    if publish and not publication_withheld:
        generation = store.publish(candidate)

    report = {
        "schema_version": SCHEMA_VERSION,
        "audit": "US_SECURITY_MASTER_BUILD",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "PUBLISH" if publish else "CANDIDATE_ONLY",
        "target_session": target_session.date().isoformat(),
        "history_start": history_start.date().isoformat(),
        "provider": "FMP",
        "provider_source_mode": source_mode,
        "provider_sources": provider_sources,
        "corrections": {
            "registry_path": str(registry_path),
            "registry_sha256": registry_sha256,
            "reviewed_symbol_transitions": correction_audit,
            "reviewed_provider_identifier_conflicts": (
                identifier_conflict_audit
            ),
            "applied": [*correction_audit, *identifier_conflict_audit],
        },
        "research_history_policy": {
            "registry_path": str(resolved_history_policy_path),
            "registry_sha256": history_policy_registry_sha256,
            "activation_session": history_policy_registry[
                "activation_session"
            ],
            "decision": history_policy_registry["decision"],
            "rows": int(len(candidate.history_policy)),
            "prospective_only_count": int(
                candidate.history_policy["policy"].eq(
                    "PROSPECTIVE_ONLY"
                ).sum()
            ),
            "excluded_unverifiable_history_count": int(
                candidate.history_policy["policy"].eq(
                    "EXCLUDED_UNVERIFIABLE_HISTORY"
                ).sum()
            ),
        },
        "api_key_value_recorded": False,
        "parameters": {
            "profile_parts": profile_parts,
            "symbol_change_limit": int(symbol_change_limit),
            "delisted_page_size": int(delisted_page_size),
            "delisted_max_pages": int(delisted_max_pages),
        },
        "sources": {
            "profiles": _source_summary(
                profiles, date_columns=("listing_date",)
            ) | {"provider_bulk": profile_source},
            "symbol_changes": _source_summary(
                changes, date_columns=("date",)
            ) | {"provider_full": change_source},
            "delisted": {
                **_source_summary(
                    delisted, date_columns=("ipo_date", "delisted_date")
                ),
                **delisted_diagnostics,
            },
            "failures": source_failures,
            "scope": scope_diagnostics,
            "duration_seconds": round(source_duration, 3),
        },
        "quality": candidate.quality,
        "previous_publication": (
            {
                "generation_id": previous_generation.generation_id,
                "target_session": previous_generation.target_session.isoformat(),
                "manifest_sha256": previous_generation.manifest_sha256,
            }
            if previous_generation is not None
            else None
        ),
        "candidate_dir": str(candidate_path),
        "candidate_artifacts": artifacts,
        "publication": (
            {
                "generation_id": generation.generation_id,
                "manifest_path": generation.manifest_path,
                "manifest_sha256": generation.manifest_sha256,
            }
            if generation is not None else None
        ),
        "publication_requested": bool(publish),
        "publication_withheld": publication_withheld,
        "timings": {
            "source_seconds": round(source_duration, 3),
            "build_seconds": round(build_duration, 3),
            "total_seconds": round(time.perf_counter() - started, 3),
        },
        "peak_rss_mb": round(_rss_mb(), 3),
    }
    report_path = candidate_path / "audit.json"
    atomic_save_json(report, report_path)
    digest = _file_sha256(report_path)
    digest_path = report_path.with_suffix(".json.sha256")
    digest_path.write_text(f"{digest}  {report_path.name}\n", encoding="ascii")
    report["report_path"] = str(report_path)
    report["report_sha256"] = digest
    report["report_sha256_path"] = str(digest_path)
    return report, 0 if candidate.quality.get("status") == "PASS" else 2


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    load_local_env(args.env_file)
    delay = int(getattr(CONFIG.data.foundation, "close_delay_minutes", 120))
    target = (
        pd.Timestamp(args.target_session).normalize()
        if args.target_session
        else pd.Timestamp(
            latest_publishable_xnys_session(delay_minutes=delay)
        ).normalize()
    )
    history_start = pd.Timestamp(args.history_start).normalize()
    if pd.isna(target) or pd.isna(history_start) or history_start > target:
        raise ValueError("history-start must be on or before target-session")
    if args.publish and not args.force_publish:
        settings = CONFIG.data.security_master
        store = SecurityMasterStore(
            CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)),
            CONFIG.abs_path(str(settings.snapshot_dir)),
        )
        try:
            published, _frames = store.load_published()
        except FileNotFoundError:
            published = None
        if published is not None and published.target_session > target.date():
            raise RuntimeError(
                "requested target predates the published Security Master: "
                f"{target.date()} < {published.target_session}"
            )
        if published is not None and published.target_session == target.date():
            summary = {
                "status": "NOOP",
                "mode": "PUBLISH",
                "target_session": target.date().isoformat(),
                "publication": {
                    "generation_id": published.generation_id,
                    "manifest_path": published.manifest_path,
                    "manifest_sha256": published.manifest_sha256,
                },
                "message": "Security Master is already published for this target session",
            }
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                print(
                    "status=NOOP mode=PUBLISH "
                    f"target={summary['target_session']} "
                    f"generation={published.generation_id}"
                )
            return 0
    profile_parts = (
        list(dict.fromkeys(args.profile_parts))
        if args.profile_parts
        else [int(value) for value in CONFIG.data.security_master.profile_parts]
    )
    report, exit_code = run_build(
        target_session=target,
        history_start=history_start,
        profile_parts=profile_parts,
        symbol_change_limit=int(args.symbol_change_limit),
        delisted_page_size=int(args.delisted_page_size),
        delisted_max_pages=int(args.delisted_max_pages),
        output_dir=Path(args.output_dir).resolve(),
        publish=bool(args.publish),
        corrections_path=_project_path(args.corrections),
        history_policy_path=_project_path(args.history_policy),
        source_dir=Path(args.source_dir) if args.source_dir else None,
    )
    summary = {
        "status": report["quality"]["status"],
        "mode": report["mode"],
        "target_session": report["target_session"],
        "security_count": report["quality"]["security_count"],
        "active_stock_count": report["quality"]["active_stock_count"],
        "failures": report["quality"]["failures"],
        "publication": report["publication"],
        "report_path": report["report_path"],
        "report_sha256": report["report_sha256"],
        "peak_rss_mb": report["peak_rss_mb"],
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            "status={status} mode={mode} target={target_session} "
            "securities={security_count} active_stocks={active_stock_count} "
            "report={report_path}".format(**summary)
        )
        if summary["failures"]:
            print("failures=" + "; ".join(summary["failures"]))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
