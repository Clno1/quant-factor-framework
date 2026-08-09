"""Build auditable security metadata for one immutable universe version.

Industry history is not assumed to be point-in-time unless a dated source says
so.  Current/provider profile classifications are explicitly labelled as
latest-known backfills.  Unknown values remain in the cross-section as the
``UNKNOWN`` bucket instead of silently removing historical securities.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date
from typing import Any

import pandas as pd


UNKNOWN_CLASSIFICATION = "UNKNOWN"
CLASSIFICATION_POLICY = "LATEST_KNOWN_BACKFILL_NOT_PIT"

SecurityProfileFetcher = Callable[[str], Mapping[str, Any] | None]


def _text(value: Any, *, fallback: str | None = None) -> str | None:
    if value is None or pd.isna(value):
        return fallback
    normalized = str(value).strip()
    return normalized or fallback


def _normalized_lookup(frame: pd.DataFrame | None) -> dict[str, dict[str, Any]]:
    if frame is None or frame.empty or "ticker" not in frame.columns:
        return {}
    work = frame.copy()
    work["ticker"] = (
        work["ticker"].astype(str).str.strip().str.upper().str.replace(
            ".", "-", regex=False
        )
    )
    work = work.loc[work["ticker"].ne("")].drop_duplicates("ticker", keep="last")
    return {
        str(row["ticker"]): row.to_dict()
        for _, row in work.iterrows()
    }


def _membership_ranges(
    membership: pd.DataFrame | None,
    *,
    target_session: date,
) -> dict[str, tuple[pd.Timestamp | None, pd.Timestamp | None]]:
    if membership is None or membership.empty:
        return {}
    work = membership.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.normalize()
    work["ticker"] = work["ticker"].astype(str).str.upper()
    work["active"] = work["active"].astype(bool)
    work = work.dropna(subset=["date"]).sort_values(["ticker", "date"])
    result: dict[str, tuple[pd.Timestamp | None, pd.Timestamp | None]] = {}
    for ticker, rows in work.groupby("ticker", sort=False):
        active = rows.loc[rows["active"], "date"]
        if active.empty:
            result[str(ticker)] = (None, None)
            continue
        first = pd.Timestamp(active.min()).normalize()
        later_inactive = rows.loc[
            rows["date"].gt(active.max()) & ~rows["active"], "date"
        ]
        effective_to = (
            pd.Timestamp(later_inactive.min()).normalize()
            if not later_inactive.empty
            else pd.Timestamp(target_session).normalize()
        )
        result[str(ticker)] = (first, effective_to)
    return result


def build_version_security_master(
    current: pd.DataFrame,
    *,
    tickers: Iterable[str],
    target_session: date,
    membership: pd.DataFrame | None = None,
    previous: pd.DataFrame | None = None,
    profile_fetcher: SecurityProfileFetcher | None = None,
) -> pd.DataFrame:
    """Return one metadata row for every current or historical version member."""
    current_lookup = _normalized_lookup(current)
    previous_lookup = _normalized_lookup(previous)
    ranges = _membership_ranges(membership, target_session=target_session)
    normalized_tickers = sorted(
        {
            str(ticker).strip().upper().replace(".", "-")
            for ticker in tickers
            if str(ticker).strip()
        }
    )
    rows: list[dict[str, Any]] = []
    for ticker in normalized_tickers:
        payload = dict(current_lookup.get(ticker) or {})
        source = "provider_current_snapshot"
        if not payload:
            payload = dict(previous_lookup.get(ticker) or {})
            source = "previous_version_backfill"
        needs_profile = not _text(payload.get("sector"))
        if needs_profile and profile_fetcher is not None:
            try:
                profile = profile_fetcher(ticker)
            except Exception:  # Provider gaps remain explicit UNKNOWN metadata.
                profile = None
            if profile:
                for key in (
                    "name",
                    "issuer_id",
                    "sector",
                    "sub_industry",
                ):
                    if not _text(payload.get(key)) and _text(profile.get(key)):
                        payload[key] = profile.get(key)
                source = "provider_profile_backfill"

        sector = _text(payload.get("sector"), fallback=UNKNOWN_CLASSIFICATION)
        sub_industry = _text(
            payload.get("sub_industry"),
            fallback=UNKNOWN_CLASSIFICATION,
        )
        known = sector != UNKNOWN_CLASSIFICATION
        effective_from, effective_to = ranges.get(ticker, (None, None))
        rows.append(
            {
                **payload,
                "ticker": ticker,
                "name": _text(payload.get("name"), fallback=ticker),
                "issuer_id": _text(payload.get("issuer_id")),
                "sector": sector,
                "sub_industry": sub_industry,
                "effective_from": effective_from,
                "effective_to": effective_to,
                "source": source if known else "explicit_unknown",
                "source_asof": pd.Timestamp(target_session),
                "classification_policy": CLASSIFICATION_POLICY,
                "classification_known": bool(known),
                "is_current_member": ticker in current_lookup,
                "snapshot_date": pd.Timestamp(target_session),
            }
        )
    return pd.DataFrame(rows).reset_index(drop=True)


def classification_coverage(frame: pd.DataFrame) -> dict[str, Any]:
    """Summarize known industry metadata without treating UNKNOWN as coverage."""
    if frame.empty:
        return {"total": 0, "known": 0, "coverage": 0.0, "unknown_tickers": []}
    known = frame.get(
        "classification_known",
        frame["sector"].fillna("").ne(UNKNOWN_CLASSIFICATION),
    ).fillna(False).astype(bool)
    unknown = sorted(frame.loc[~known, "ticker"].astype(str).tolist())
    return {
        "total": int(len(frame)),
        "known": int(known.sum()),
        "coverage": float(known.mean()),
        "unknown_tickers": unknown,
    }


__all__ = [
    "CLASSIFICATION_POLICY",
    "UNKNOWN_CLASSIFICATION",
    "build_version_security_master",
    "classification_coverage",
]
