"""Versioned Security Master for broad US-equity research.

Ticker is a dated alias, never the durable primary key. Provider identifiers
and symbol-change lineage produce a stable ``security_id``; every publication
freezes the master, symbol history, identity keys and classifications together.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from typing import Any, Iterable
import uuid

import duckdb
import pandas as pd

from src.data.security_master import (
    CLASSIFICATION_POLICY,
    UNKNOWN_CLASSIFICATION,
)
from src.data.research_history_policy import (
    apply_research_history_policy,
    empty_history_policy,
)
from src.utils.io import atomic_save_json


SCHEMA_VERSION = 1
MAIN_EXCHANGES = {"NASDAQ", "NYSE", "AMEX"}
SECURITY_NAMESPACE = uuid.UUID("66e4123e-5ce0-4c94-a7b6-f080b8e578b4")


@dataclass(frozen=True)
class SecurityMasterCandidate:
    target_session: date
    master: pd.DataFrame
    symbols: pd.DataFrame
    classifications: pd.DataFrame
    identity_keys: pd.DataFrame
    history_policy: pd.DataFrame
    quality: dict[str, Any]


@dataclass(frozen=True)
class SecurityMasterGeneration:
    generation_id: str
    target_session: date
    created_at: datetime
    status: str
    row_count: int
    active_count: int
    master_path: str
    symbols_path: str
    classifications_path: str
    identity_keys_path: str
    manifest_path: str
    master_sha256: str
    symbols_sha256: str
    classifications_sha256: str
    identity_keys_sha256: str
    manifest_sha256: str


@dataclass(frozen=True)
class SecurityResolution:
    generation_id: str
    asof: date
    security_id: str
    queried_ticker: str
    current_ticker: str
    name: str
    asset_type: str
    trading_status: str
    effective_from: date | None
    effective_to: date | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "asof": self.asof.isoformat(),
            "security_id": self.security_id,
            "queried_ticker": self.queried_ticker,
            "current_ticker": self.current_ticker,
            "name": self.name,
            "asset_type": self.asset_type,
            "trading_status": self.trading_status,
            "effective_from": (
                self.effective_from.isoformat() if self.effective_from else None
            ),
            "effective_to": (
                self.effective_to.isoformat() if self.effective_to else None
            ),
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _ticker(value: Any) -> str:
    return _text(value).upper().replace(".", "-").replace("/", "-")


def _identifier(value: Any) -> str:
    return "".join(character for character in _text(value).upper() if character.isalnum())


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_id(prefix: str, key: str) -> str:
    return f"{prefix}_{uuid.uuid5(SECURITY_NAMESPACE, key).hex}"


def _normalize_changes(frame: pd.DataFrame | None, target: pd.Timestamp) -> pd.DataFrame:
    columns = ["date", "old_ticker", "new_ticker", "company_name"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    work = frame.copy()
    missing = {"date", "old_ticker", "new_ticker"} - set(work.columns)
    if missing:
        raise ValueError(f"symbol changes missing fields: {sorted(missing)}")
    if "company_name" not in work.columns:
        work["company_name"] = ""
    work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.normalize()
    work["old_ticker"] = work["old_ticker"].map(_ticker)
    work["new_ticker"] = work["new_ticker"].map(_ticker)
    work["company_name"] = work["company_name"].map(_text)
    return (
        work.dropna(subset=["date"])
        .loc[lambda value: value["date"].le(target)]
        .loc[lambda value: value["old_ticker"].ne("")]
        .loc[lambda value: value["new_ticker"].ne("")]
        .loc[:, columns]
        .drop_duplicates(["date", "old_ticker", "new_ticker"], keep="last")
        .sort_values(["date", "old_ticker", "new_ticker"])
        .reset_index(drop=True)
    )


def _profile_identity_lookup(
    profile: pd.DataFrame,
) -> dict[str, dict[str, set[str]]]:
    lookup: dict[str, dict[str, set[str]]] = {}
    for ticker, rows in profile.groupby("ticker", sort=False):
        lookup[str(ticker)] = {
            key: {
                value
                for value in rows[key].map(_identifier)
                if value
            }
            for key in ("cik", "cusip", "isin")
        }
    return lookup


def _identity_support_score(
    old_ticker: str,
    new_ticker: str,
    identities: dict[str, dict[str, set[str]]],
) -> tuple[int, str | None]:
    old = identities.get(old_ticker)
    new = identities.get(new_ticker)
    if not old or not new:
        return 0, None
    if old["cusip"] & new["cusip"]:
        return 3, "CUSIP"
    if old["isin"] & new["isin"]:
        return 3, "ISIN"
    # CIK identifies the issuer, not the listed issue. If both profiles expose
    # issue-level IDs and those IDs disagree, a CIK-only match can be two share
    # classes trading at the same time (for example common and preferred).
    conflicting_issue_ids = any(
        old[key] and new[key] and not (old[key] & new[key])
        for key in ("cusip", "isin")
    )
    if conflicting_issue_ids:
        return 0, None
    if old["cik"] & new["cik"]:
        return 2, "CIK"
    return 0, None


def _is_transitive_predecessor(
    ancestor: str,
    descendant: str,
    *,
    before: pd.Timestamp,
    edges: list[tuple[str, str, pd.Timestamp]],
) -> bool:
    graph: dict[str, set[str]] = {}
    for old_ticker, new_ticker, event_date in edges:
        if event_date >= before:
            continue
        graph.setdefault(old_ticker, set()).add(new_ticker)
    frontier = [ancestor]
    seen: set[str] = set()
    while frontier:
        current = frontier.pop()
        if current == descendant:
            return True
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(graph.get(current, ()))
    return False


def _lineage_maps(
    changes: pd.DataFrame,
    profile: pd.DataFrame,
    reviewed_identity_continuity: set[
        tuple[str, str, pd.Timestamp]
    ] | None = None,
) -> tuple[
    dict[str, list[tuple[str, pd.Timestamp]]],
    list[str],
    dict[str, Any],
]:
    identities = _profile_identity_lookup(profile)
    backward: dict[str, list[tuple[str, pd.Timestamp]]] = {}
    conflicts: list[str] = []
    verified = changes.copy()
    reviewed_edges = {
        (
            _ticker(old_ticker),
            _ticker(new_ticker),
            pd.Timestamp(event_date).normalize(),
        )
        for old_ticker, new_ticker, event_date in (
            reviewed_identity_continuity or set()
        )
    }
    source_edges = {
        (
            str(row.old_ticker),
            str(row.new_ticker),
            pd.Timestamp(row.date).normalize(),
        )
        for row in verified.itertuples(index=False)
    }
    missing_reviewed_edges = sorted(reviewed_edges - source_edges)
    if missing_reviewed_edges:
        raise ValueError(
            "reviewed identity-continuity edge missing from provider events: "
            + ",".join(
                f"{old}->{new}@{event.date()}"
                for old, new, event in missing_reviewed_edges
            )
        )

    def support_for(row: pd.Series) -> tuple[int, str | None]:
        edge = (
            str(row["old_ticker"]),
            str(row["new_ticker"]),
            pd.Timestamp(row["date"]).normalize(),
        )
        if edge in reviewed_edges:
            return 4, "REVIEWED_SEC_CONTINUITY"
        return _identity_support_score(
            str(row["old_ticker"]),
            str(row["new_ticker"]),
            identities,
        )

    support = [support_for(row) for _, row in verified.iterrows()]
    verified["_support_score"] = [value[0] for value in support]
    verified["_support_key"] = [value[1] or "" for value in support]
    unverified = verified.loc[verified["_support_score"].eq(0)].copy()
    verified = verified.loc[verified["_support_score"].gt(0)].copy()
    verified_edges = [
        (
            str(row.old_ticker),
            str(row.new_ticker),
            pd.Timestamp(row.date).normalize(),
        )
        for row in verified.itertuples(index=False)
    ]
    resolved: list[dict[str, Any]] = []
    for (event_date, new_ticker), rows in verified.groupby(
        ["date", "new_ticker"], sort=True
    ):
        candidates = rows.sort_values(
            ["_support_score", "old_ticker"], ascending=[False, True]
        )
        old_values = candidates["old_ticker"].astype(str).tolist()
        immediate = [
            candidate
            for candidate in old_values
            if not any(
                other != candidate
                and _is_transitive_predecessor(
                    candidate,
                    other,
                    before=pd.Timestamp(event_date),
                    edges=verified_edges,
                )
                for other in old_values
            )
        ]
        if immediate and len(immediate) < len(old_values):
            candidates = candidates.loc[
                candidates["old_ticker"].isin(immediate)
            ].copy()
            resolved.append({
                "date": pd.Timestamp(event_date).date().isoformat(),
                "new_ticker": str(new_ticker),
                "chosen_old_ticker": immediate[0] if len(immediate) == 1 else None,
                "candidates": sorted(old_values),
                "reason": "TRANSITIVE_ANCESTOR_REMOVED",
            })
        top_score = int(candidates["_support_score"].max())
        top = candidates.loc[candidates["_support_score"].eq(top_score)].copy()
        if len(top) != 1:
            predecessors = sorted(top["old_ticker"].astype(str).unique())
            conflicts.append(
                f"multiple verified predecessors for {new_ticker} on "
                f"{pd.Timestamp(event_date).date()}: {','.join(predecessors)}"
            )
            continue
        chosen = top.iloc[0]
        old_ticker = str(chosen["old_ticker"])
        event_ts = pd.Timestamp(event_date).normalize()
        backward.setdefault(str(new_ticker), []).append((old_ticker, event_ts))
        if len(candidates) > 1 and not any(
            value["date"] == event_ts.date().isoformat()
            and value["new_ticker"] == str(new_ticker)
            for value in resolved
        ):
            resolved.append({
                "date": event_ts.date().isoformat(),
                "new_ticker": str(new_ticker),
                "chosen_old_ticker": old_ticker,
                "candidates": sorted(candidates["old_ticker"].astype(str).unique()),
                "reason": f"STRONGER_{str(chosen['_support_key'])}_MATCH",
            })
    for events in backward.values():
        events.sort(key=lambda value: (value[1], value[0]), reverse=True)
    diagnostics = {
        "source_event_count": int(len(changes)),
        "verified_event_count": int(len(verified)),
        "unverified_event_count": int(len(unverified)),
        "unverified_event_sha256": hashlib.sha256(
            pd.util.hash_pandas_object(
                unverified[["date", "old_ticker", "new_ticker"]].astype(str),
                index=False,
            ).to_numpy().tobytes()
        ).hexdigest(),
        "resolved_multiple_predecessors": resolved,
        "unresolved_multiple_predecessor_count": int(len(conflicts)),
        "reviewed_identity_continuity": [
            {
                "old_ticker": old,
                "new_ticker": new,
                "effective_date": event.date().isoformat(),
            }
            for old, new, event in sorted(reviewed_edges)
        ],
        "policy": (
            "AUTO_LINK_SHARED_ISSUE_ID_OR_NONCONFLICTING_CIK_"
            "PLUS_EXACT_SEC_REVIEW"
        ),
    }
    return backward, conflicts, diagnostics


def _predecessor(
    ticker: str,
    backward: dict[str, list[tuple[str, pd.Timestamp]]],
    *,
    before: pd.Timestamp,
    listing_date: pd.Timestamp | None,
) -> tuple[str, pd.Timestamp] | None:
    for old_ticker, event_date in backward.get(ticker, []):
        if event_date > before:
            continue
        if listing_date is not None and event_date < listing_date:
            continue
        return old_ticker, event_date
    return None


def _lineage_root(
    ticker: str,
    backward: dict[str, list[tuple[str, pd.Timestamp]]],
    *,
    listing_date: pd.Timestamp | None,
    target: pd.Timestamp,
) -> str:
    current = ticker
    before = target
    while True:
        predecessor = _predecessor(
            current,
            backward,
            before=before,
            listing_date=listing_date,
        )
        if predecessor is None:
            break
        current, event_date = predecessor
        before = event_date - pd.Timedelta(days=1)
    return current


def _identity_key(row: pd.Series, lineage_root: str) -> tuple[str, str]:
    cusip = _identifier(row.get("cusip"))
    isin = _identifier(row.get("isin"))
    cik = _identifier(row.get("cik"))
    # CUSIP/ISIN can change during an otherwise continuous ticker rename.
    # CIK alone is issuer-level and would merge share classes, so bind it to
    # the dated symbol lineage before considering issue identifiers.
    if cik:
        return "CIK_LINEAGE", f"{cik}:{lineage_root}"
    if cusip:
        return "CUSIP", cusip
    if isin:
        return "ISIN", isin
    listing = pd.to_datetime(row.get("listing_date"), errors="coerce")
    listing_key = listing.date().isoformat() if not pd.isna(listing) else "UNKNOWN"
    exchange = _text(row.get("exchange")).upper() or "UNKNOWN"
    name = _text(row.get("name")).upper()
    return "LISTING_LINEAGE", f"{exchange}:{lineage_root}:{listing_key}:{name}"


def _normalize_profiles(frame: pd.DataFrame, target: pd.Timestamp) -> pd.DataFrame:
    if frame.empty:
        raise ValueError("profile bulk is empty")
    required = {
        "ticker", "name", "asset_type", "exchange", "country", "currency",
        "cik", "isin", "cusip", "listing_date", "sector", "sub_industry",
        "trading_status", "is_active",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"profile bulk missing fields: {sorted(missing)}")
    work = frame.copy()
    work["ticker"] = work["ticker"].map(_ticker)
    work["exchange"] = work["exchange"].map(lambda value: _text(value).upper())
    work = work.loc[
        work["ticker"].ne("") & work["exchange"].isin(MAIN_EXCHANGES)
    ].copy()
    for column in ("name", "sector", "sub_industry"):
        work[column] = work[column].map(_text)
    for column in ("country", "currency", "cik", "isin", "cusip"):
        work[column] = work[column].map(lambda value: _text(value).upper())
    work["asset_type"] = work["asset_type"].map(lambda value: _text(value).upper())
    work["listing_date"] = pd.to_datetime(
        work["listing_date"], errors="coerce"
    ).dt.normalize()
    work["is_active"] = work["is_active"].fillna(False).astype(bool)
    work["trading_status"] = work["is_active"].map(
        {True: "ACTIVE", False: "INACTIVE"}
    )
    work["source_asof"] = target
    return work.reset_index(drop=True)


def _normalize_delisted(frame: pd.DataFrame | None, target: pd.Timestamp) -> pd.DataFrame:
    columns = ["ticker", "name", "exchange", "ipo_date", "delisted_date"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    work = frame.copy()
    missing = {"ticker", "delisted_date"} - set(work.columns)
    if missing:
        raise ValueError(f"delisted companies missing fields: {sorted(missing)}")
    for column in columns:
        if column not in work.columns:
            work[column] = pd.NaT if column.endswith("_date") else ""
    work["ticker"] = work["ticker"].map(_ticker)
    work["name"] = work["name"].map(_text)
    work["exchange"] = work["exchange"].map(lambda value: _text(value).upper())
    for column in ("ipo_date", "delisted_date"):
        work[column] = pd.to_datetime(work[column], errors="coerce").dt.normalize()
    return (
        work.dropna(subset=["delisted_date"])
        .loc[lambda value: value["ticker"].ne("")]
        .loc[lambda value: value["exchange"].isin(MAIN_EXCHANGES)]
        .loc[lambda value: value["delisted_date"].le(target)]
        .loc[:, columns]
        .drop_duplicates(["ticker", "delisted_date"], keep="last")
        .sort_values(["delisted_date", "ticker"])
        .reset_index(drop=True)
    )


def _previous_identity_map(frame: pd.DataFrame | None) -> tuple[dict[str, str], list[str]]:
    if frame is None or frame.empty:
        return {}, []
    mapping: dict[str, str] = {}
    ambiguous: list[str] = []
    for (key_type, key_value), rows in frame.groupby(
        ["key_type", "key_value"], sort=True
    ):
        security_ids = sorted(rows["security_id"].astype(str).unique())
        key = f"{key_type}:{key_value}"
        if len(security_ids) != 1:
            ambiguous.append(
                f"identity key {key} maps to {','.join(security_ids)}"
            )
            continue
        mapping[key] = security_ids[0]
    return mapping, ambiguous


def _shared_issue_identity_pairs(
    profile: pd.DataFrame,
) -> tuple[dict[Any, tuple[str, str]], list[dict[str, Any]]]:
    """Identify ticker aliases only when issuer and issue IDs all agree."""
    work = profile.copy()
    for column in ("cik", "cusip", "isin"):
        work[f"_{column}"] = work[column].map(_identifier)
    work["_asset_type"] = work["asset_type"].map(
        lambda value: _text(value).upper()
    )
    eligible = work.loc[
        work["_cik"].ne("")
        & work["_cusip"].ne("")
        & work["_isin"].ne("")
        & work["_asset_type"].isin({"STOCK", "ADR"})
    ]
    overrides: dict[Any, tuple[str, str]] = {}
    diagnostics: list[dict[str, Any]] = []
    group_columns = ["_cik", "_cusip", "_isin", "_asset_type"]
    for values, rows in eligible.groupby(group_columns, sort=True):
        tickers = sorted(rows["ticker"].astype(str).unique())
        if len(tickers) < 2:
            continue
        cik, cusip, isin, asset_type = values
        key_value = f"{asset_type}:{cik}:{cusip}:{isin}"
        for index in rows.index:
            overrides[index] = ("ISSUE_IDENTITY", key_value)
        diagnostics.append({
            "key_value": key_value,
            "asset_type": asset_type,
            "cik": cik,
            "cusip": cusip,
            "isin": isin,
            "tickers": tickers,
        })
    return overrides, diagnostics


def _identity_key_conflicts(frame: pd.DataFrame) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    if frame.empty:
        return conflicts
    for (key_type, key_value), rows in frame.groupby(
        ["key_type", "key_value"], sort=True
    ):
        security_ids = sorted(rows["security_id"].astype(str).unique())
        if len(security_ids) > 1:
            conflicts.append({
                "key_type": str(key_type),
                "key_value": str(key_value),
                "security_ids": security_ids,
            })
    return conflicts


def _symbol_chain(
    current_ticker: str,
    *,
    listing_date: pd.Timestamp | None,
    delisting_date: pd.Timestamp | None,
    backward: dict[str, list[tuple[str, pd.Timestamp]]],
    target: pd.Timestamp,
) -> list[dict[str, Any]]:
    reverse_rows: list[dict[str, Any]] = []
    cursor = current_ticker
    effective_to = delisting_date
    before = target
    while True:
        predecessor = _predecessor(
            cursor,
            backward,
            before=before,
            listing_date=listing_date,
        )
        if predecessor is None:
            break
        old_ticker, event_date = predecessor
        if listing_date is not None and event_date <= listing_date:
            break
        reverse_rows.append({
            "ticker": cursor,
            "effective_from": event_date,
            "effective_to": effective_to,
            "event_type": "SYMBOL_CHANGE",
        })
        cursor = old_ticker
        effective_to = event_date - pd.Timedelta(days=1)
        before = effective_to
    if not (
        listing_date is not None
        and effective_to is not None
        and listing_date > effective_to
    ):
        reverse_rows.append({
            "ticker": cursor,
            "effective_from": listing_date,
            "effective_to": effective_to,
            "event_type": "LISTING",
        })
    return list(reversed(reverse_rows))


def _reconcile_event_linked_security_ids(
    profile: pd.DataFrame,
    *,
    backward: dict[str, list[tuple[str, pd.Timestamp]]],
) -> tuple[pd.Series, list[str], list[dict[str, Any]]]:
    """Union initial IDs only across already verified symbol-change edges."""
    ids = sorted(profile["security_id"].astype(str).unique())
    parent = {security_id: security_id for security_id in ids}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        canonical, other = sorted((left_root, right_root))
        parent[other] = canonical

    edge_keys: list[tuple[str, str, str]] = []
    resolutions: list[dict[str, Any]] = []
    for new_ticker, events in backward.items():
        new_rows = profile.loc[profile["ticker"].eq(new_ticker)]
        if new_rows.empty:
            continue
        for old_ticker, event_date in events:
            old_rows = profile.loc[profile["ticker"].eq(old_ticker)]
            if old_rows.empty:
                continue
            for _, old_row in old_rows.iterrows():
                for _, new_row in new_rows.iterrows():
                    matches: list[tuple[str, str]] = []
                    for key in ("cusip", "isin", "cik"):
                        old_value = _identifier(old_row.get(key))
                        new_value = _identifier(new_row.get(key))
                        if old_value and old_value == new_value:
                            matches.append((key.upper(), old_value))
                    if not matches:
                        continue
                    old_id = str(old_row["security_id"])
                    new_id = str(new_row["security_id"])
                    union(old_id, new_id)
                    for key_type, key_value in matches:
                        edge_keys.append((old_id, new_id, f"{key_type}:{key_value}"))
                    if old_id != new_id:
                        resolutions.append({
                            "date": event_date.date().isoformat(),
                            "old_ticker": old_ticker,
                            "new_ticker": new_ticker,
                            "matched_keys": [key for key, _ in matches],
                        })

    components: dict[str, set[str]] = {}
    for security_id in ids:
        components.setdefault(find(security_id), set()).add(security_id)
    mapping: dict[str, str] = {}
    conflicts: list[str] = []
    for members in components.values():
        rows = profile.loc[profile["security_id"].isin(members)]
        previous = sorted(
            {
                str(value)
                for value in rows["_previous_security_id"].dropna()
                if str(value)
            }
        )
        if len(previous) > 1:
            conflicts.append(
                "verified event would merge prior security_ids: "
                + ",".join(previous)
            )
            canonical_id = previous[0]
        elif previous:
            canonical_id = previous[0]
        else:
            component_keys = sorted({
                key
                for left, right, key in edge_keys
                if left in members and right in members
                and key.startswith(("CUSIP:", "ISIN:"))
            })
            canonical_id = (
                _stable_id("sec", component_keys[0])
                if component_keys else sorted(members)[0]
            )
        for security_id in members:
            mapping[security_id] = canonical_id
    return profile["security_id"].astype(str).map(mapping), conflicts, resolutions


def _interval_conflicts(symbols: pd.DataFrame) -> list[str]:
    conflicts: list[str] = []
    if symbols.empty:
        return conflicts
    lower_bound = pd.Timestamp("1900-01-01")
    upper_bound = pd.Timestamp("2262-01-01")
    for ticker, rows in symbols.groupby("ticker", sort=False):
        ordered = rows.assign(
            _start=pd.to_datetime(rows["effective_from"], errors="coerce").fillna(lower_bound),
            _end=pd.to_datetime(rows["effective_to"], errors="coerce").fillna(upper_bound),
        ).sort_values(["_start", "_end", "security_id"])
        previous_end: pd.Timestamp | None = None
        previous_security: str | None = None
        for row in ordered.to_dict("records"):
            start = pd.Timestamp(row["_start"])
            end = pd.Timestamp(row["_end"])
            if start > end:
                conflicts.append(f"invalid interval {ticker} {start.date()}..{end.date()}")
            if (
                previous_end is not None
                and start <= previous_end
                and str(row["security_id"]) != previous_security
            ):
                conflicts.append(
                    f"overlapping ticker {ticker}: {previous_security} and {row['security_id']}"
                )
            if previous_end is None or end > previous_end:
                previous_end = end
                previous_security = str(row["security_id"])
    # One stable security may change ticker over time, but two different
    # tickers for that same security cannot both be valid on the same date.
    # Such overlap usually means a provider omitted a rename or an issuer-level
    # key accidentally merged separate share classes.
    for security_id, rows in symbols.groupby("security_id", sort=False):
        ordered = rows.assign(
            _start=pd.to_datetime(rows["effective_from"], errors="coerce").fillna(lower_bound),
            _end=pd.to_datetime(rows["effective_to"], errors="coerce").fillna(upper_bound),
        ).sort_values(["_start", "_end", "ticker"])
        records = ordered.to_dict("records")
        for index, left in enumerate(records):
            left_end = pd.Timestamp(left["_end"])
            for right in records[index + 1:]:
                right_start = pd.Timestamp(right["_start"])
                if right_start > left_end:
                    break
                if str(left["ticker"]) != str(right["ticker"]):
                    conflicts.append(
                        "overlapping aliases for security "
                        f"{security_id}: {left['ticker']} and {right['ticker']}"
                    )
    return conflicts


def build_security_master_candidate(
    profiles: pd.DataFrame,
    *,
    symbol_changes: pd.DataFrame | None,
    delisted_companies: pd.DataFrame | None,
    target_session: date | str | pd.Timestamp,
    previous_identity_keys: pd.DataFrame | None = None,
    previous_master: pd.DataFrame | None = None,
    previous_symbols: pd.DataFrame | None = None,
    previous_classifications: pd.DataFrame | None = None,
    minimum_active_stocks: int = 1_000,
    minimum_name_coverage: float = 0.99,
    minimum_classification_coverage: float = 0.95,
    research_history_policy: dict[str, Any] | None = None,
    reviewed_identity_continuity: set[
        tuple[str, str, pd.Timestamp]
    ] | None = None,
) -> SecurityMasterCandidate:
    """Build and validate one immutable Security Master candidate."""
    target = pd.Timestamp(target_session).normalize()
    if pd.isna(target):
        raise ValueError("target_session must be a valid date")
    # Immutable business rows must be a pure function of their frozen inputs.
    # The audit manifest records the actual build time separately.
    row_updated_at = target.tz_localize("UTC")
    profile = _normalize_profiles(profiles, target)
    changes = _normalize_changes(symbol_changes, target)
    delisted = _normalize_delisted(delisted_companies, target)
    backward, lineage_conflicts, lineage_diagnostics = _lineage_maps(
        changes,
        profile,
        reviewed_identity_continuity=reviewed_identity_continuity,
    )
    previous_map, previous_ambiguous_keys = _previous_identity_map(
        previous_identity_keys
    )

    profile["lineage_root"] = profile.apply(
        lambda row: _lineage_root(
            str(row["ticker"]),
            backward,
            listing_date=(
                pd.Timestamp(row["listing_date"]).normalize()
                if pd.notna(row["listing_date"]) else None
            ),
            target=target,
        ),
        axis=1,
    )
    identity_pairs = profile.apply(
        lambda row: _identity_key(row, str(row["lineage_root"])),
        axis=1,
    )
    shared_issue_pairs, shared_issue_groups = _shared_issue_identity_pairs(
        profile
    )
    identity_pairs = [
        shared_issue_pairs.get(index, pair)
        for index, pair in zip(profile.index, identity_pairs)
    ]
    profile["primary_key_type"] = [pair[0] for pair in identity_pairs]
    profile["primary_key_value"] = [pair[1] for pair in identity_pairs]
    profile["identity_lookup"] = (
        profile["primary_key_type"] + ":" + profile["primary_key_value"]
    )
    previous_ids = profile["identity_lookup"].map(previous_map)
    generated_ids = profile["identity_lookup"].map(
        lambda key: _stable_id("sec", key)
    )
    profile["security_id"] = previous_ids.where(
        previous_ids.notna(), generated_ids
    ).astype(str)
    profile["_previous_security_id"] = previous_ids
    (
        profile["security_id"],
        event_identity_conflicts,
        event_identity_resolutions,
    ) = _reconcile_event_linked_security_ids(profile, backward=backward)
    lineage_diagnostics["event_identity_resolutions"] = event_identity_resolutions

    delisted_lookup = {
        ticker: rows.sort_values("delisted_date").iloc[-1]
        for ticker, rows in delisted.groupby("ticker", sort=False)
    }
    master_rows: list[dict[str, Any]] = []
    identity_rows: list[dict[str, Any]] = []
    symbol_rows: list[dict[str, Any]] = []
    classification_rows: list[dict[str, Any]] = []
    represented_delistings: set[tuple[str, pd.Timestamp]] = set()
    invalid_delisted_listing_dates: list[dict[str, str]] = []

    # Only verified edges influence which profile is considered the current
    # symbol. Raw provider events may reuse the same ticker for another issuer.
    final_old_symbols = {
        old_ticker
        for events in backward.values()
        for old_ticker, _event_date in events
    }
    for security_id, rows in profile.groupby("security_id", sort=True):
        rows = rows.copy()
        rows["_is_final_symbol"] = ~rows["ticker"].isin(final_old_symbols)
        rows = rows.sort_values(
            ["is_active", "_is_final_symbol", "listing_date", "ticker"],
            ascending=[False, False, False, True],
            na_position="last",
        )
        selected = rows.iloc[0]
        current_ticker = str(selected["ticker"])
        selected_listing = pd.to_datetime(
            selected.get("listing_date"), errors="coerce"
        )
        current_listing_ts = (
            None
            if pd.isna(selected_listing)
            else pd.Timestamp(selected_listing).normalize()
        )
        listing_date = pd.to_datetime(
            rows["listing_date"], errors="coerce"
        ).min()
        listing_ts = None if pd.isna(listing_date) else pd.Timestamp(listing_date).normalize()
        delisted_row = delisted_lookup.get(current_ticker)
        delisting_ts: pd.Timestamp | None = None
        if delisted_row is not None and not bool(selected["is_active"]):
            delisting_ts = pd.Timestamp(delisted_row["delisted_date"]).normalize()
            represented_delistings.add((current_ticker, delisting_ts))
        cik = _identifier(selected.get("cik"))
        issuer_id = (
            f"issuer_cik_{cik.zfill(10)}"
            if cik else _stable_id("issuer", f"NAME:{_text(selected.get('name')).upper()}")
        )
        sector = _text(selected.get("sector")) or UNKNOWN_CLASSIFICATION
        sub_industry = _text(selected.get("sub_industry")) or UNKNOWN_CLASSIFICATION
        is_active = bool(selected["is_active"]) and delisting_ts is None
        master_rows.append({
            "security_id": str(security_id),
            "issuer_id": issuer_id,
            "current_ticker": current_ticker,
            "name": _text(selected.get("name")) or current_ticker,
            "asset_type": _text(selected.get("asset_type")).upper() or "UNKNOWN",
            "primary_exchange": _text(selected.get("exchange")).upper(),
            "country": _text(selected.get("country")).upper(),
            "currency": _text(selected.get("currency")).upper() or "USD",
            "cik": cik,
            "isin": _identifier(selected.get("isin")),
            "cusip": _identifier(selected.get("cusip")),
            "listing_date": listing_ts,
            "delisting_date": delisting_ts,
            "trading_status": "ACTIVE" if is_active else "INACTIVE",
            "source": "FMP_PROFILE_BULK",
            "source_asof": target,
            "updated_at": row_updated_at,
        })
        for row in rows.itertuples(index=False):
            for key_type, raw_value in (
                ("CUSIP", row.cusip),
                ("ISIN", row.isin),
                ("CIK_LINEAGE", f"{_identifier(row.cik)}:{row.lineage_root}" if _identifier(row.cik) else ""),
                (str(row.primary_key_type), row.primary_key_value),
            ):
                value = _text(raw_value)
                if value:
                    identity_rows.append({
                        "security_id": str(security_id),
                        "key_type": key_type,
                        "key_value": value,
                        "source": "FMP_PROFILE_BULK",
                        "source_asof": target,
                    })
        symbol_chain = _symbol_chain(
            current_ticker,
            listing_date=listing_ts,
            delisting_date=delisting_ts,
            backward=backward,
            target=target,
        )
        if (
            len(symbol_chain) == 1
            and symbol_chain[0]["ticker"] == current_ticker
            and current_listing_ts is not None
        ):
            symbol_chain[0]["effective_from"] = current_listing_ts
        represented_tickers = {
            str(symbol["ticker"]) for symbol in symbol_chain
        }
        for alias in rows.itertuples(index=False):
            alias_ticker = str(alias.ticker)
            if alias_ticker in represented_tickers:
                continue
            alias_start = (
                pd.Timestamp(alias.listing_date).normalize()
                if pd.notna(alias.listing_date) else None
            )
            alias_delisted = delisted_lookup.get(alias_ticker)
            alias_end = (
                pd.Timestamp(alias_delisted["delisted_date"]).normalize()
                if alias_delisted is not None else None
            )
            if alias_end is None and alias_ticker != current_ticker:
                if (
                    current_listing_ts is not None
                    and alias_start is not None
                    and current_listing_ts > alias_start
                ):
                    alias_end = current_listing_ts - pd.Timedelta(days=1)
                elif not bool(alias.is_active):
                    alias_end = target
            symbol_chain.append({
                "ticker": alias_ticker,
                "effective_from": alias_start,
                "effective_to": alias_end,
                "event_type": "ISSUE_ID_ALIAS",
            })
            represented_tickers.add(alias_ticker)
        for symbol in symbol_chain:
            symbol_rows.append({
                "security_id": str(security_id),
                **symbol,
                "exchange": _text(selected.get("exchange")).upper(),
                "is_primary": True,
                "source": "FMP_PROFILE_BULK+SYMBOL_CHANGE",
                "source_asof": target,
            })
            alias_delisting = delisted_lookup.get(str(symbol["ticker"]))
            if alias_delisting is not None:
                alias_date = pd.Timestamp(
                    alias_delisting["delisted_date"]
                ).normalize()
                alias_start = symbol["effective_from"]
                alias_end = symbol["effective_to"]
                starts_before = alias_start is None or alias_date >= alias_start
                ends_after = alias_end is None or alias_date <= (
                    pd.Timestamp(alias_end) + pd.Timedelta(days=1)
                )
                if starts_before and ends_after:
                    represented_delistings.add((str(symbol["ticker"]), alias_date))
        classification_rows.append({
            "security_id": str(security_id),
            "sector": sector,
            "sub_industry": sub_industry,
            "effective_from": listing_ts,
            "effective_to": delisting_ts,
            "knowledge_date": target,
            "classification_policy": CLASSIFICATION_POLICY,
            "source": "FMP_PROFILE_BULK",
            "source_asof": target,
        })

    # A reused ticker can have an active profile and a separate older delisting.
    # Keep the old listing under a distinct fallback identity instead of merging.
    for row in delisted.itertuples(index=False):
        delisting_ts = pd.Timestamp(row.delisted_date).normalize()
        key = (str(row.ticker), delisting_ts)
        if key in represented_delistings:
            continue
        current_matches = profile.loc[profile["ticker"].eq(str(row.ticker))]
        if not current_matches.empty:
            oldest_current_listing = pd.to_datetime(
                current_matches["listing_date"], errors="coerce"
            ).min()
            if pd.notna(oldest_current_listing) and delisting_ts >= oldest_current_listing:
                # The inactive profile already owns this history even if provider
                # status metadata is internally inconsistent.
                if not current_matches["is_active"].any():
                    continue
        listing_ts = (
            pd.Timestamp(row.ipo_date).normalize()
            if pd.notna(row.ipo_date) else None
        )
        if listing_ts is not None and listing_ts > delisting_ts:
            invalid_delisted_listing_dates.append({
                "ticker": str(row.ticker),
                "provider_listing_date": listing_ts.date().isoformat(),
                "delisting_date": delisting_ts.date().isoformat(),
                "resolution": "LISTING_DATE_SET_TO_UNKNOWN",
            })
            listing_ts = None
        fallback_key = (
            f"DELISTED:{row.exchange}:{row.ticker}:"
            f"{listing_ts.date().isoformat() if listing_ts is not None else 'UNKNOWN'}:"
            f"{delisting_ts.date().isoformat()}"
        )
        security_id = previous_map.get(
            f"DELISTED_LISTING:{fallback_key}",
            _stable_id("sec", fallback_key),
        )
        master_rows.append({
            "security_id": security_id,
            "issuer_id": _stable_id("issuer", f"DELISTED:{row.name}:{row.ticker}"),
            "current_ticker": str(row.ticker),
            "name": _text(row.name) or str(row.ticker),
            "asset_type": "UNKNOWN",
            "primary_exchange": str(row.exchange),
            "country": "US",
            "currency": "USD",
            "cik": "",
            "isin": "",
            "cusip": "",
            "listing_date": listing_ts,
            "delisting_date": delisting_ts,
            "trading_status": "DELISTED",
            "source": "FMP_DELISTED_COMPANIES",
            "source_asof": target,
            "updated_at": row_updated_at,
        })
        identity_rows.append({
            "security_id": security_id,
            "key_type": "DELISTED_LISTING",
            "key_value": fallback_key,
            "source": "FMP_DELISTED_COMPANIES",
            "source_asof": target,
        })
        symbol_rows.append({
            "security_id": security_id,
            "ticker": str(row.ticker),
            "exchange": str(row.exchange),
            "effective_from": listing_ts,
            "effective_to": delisting_ts,
            "is_primary": True,
            "event_type": "DELISTED_LISTING",
            "source": "FMP_DELISTED_COMPANIES",
            "source_asof": target,
        })
        classification_rows.append({
            "security_id": security_id,
            "sector": UNKNOWN_CLASSIFICATION,
            "sub_industry": UNKNOWN_CLASSIFICATION,
            "effective_from": listing_ts,
            "effective_to": delisting_ts,
            "knowledge_date": target,
            "classification_policy": CLASSIFICATION_POLICY,
            "source": "FMP_DELISTED_COMPANIES",
            "source_asof": target,
        })

    master = pd.DataFrame(master_rows).drop_duplicates("security_id", keep="last")
    symbols = pd.DataFrame(symbol_rows).drop_duplicates(
        ["security_id", "ticker", "effective_from", "effective_to"], keep="last"
    )
    classifications = pd.DataFrame(classification_rows).drop_duplicates(
        ["security_id", "effective_from", "effective_to"], keep="last"
    )
    identity_keys = pd.DataFrame(identity_rows).drop_duplicates(
        ["security_id", "key_type", "key_value"], keep="last"
    )
    approved_excluded_ids = {
        str(item.get("security_id") or "")
        for item in (research_history_policy or {}).get("entries", [])
        if str(item.get("policy") or "").strip().upper()
        == "EXCLUDED_UNVERIFIABLE_HISTORY"
    }
    carried_forward_ids: set[str] = set()
    if previous_master is not None and not previous_master.empty:
        prior_master = previous_master.copy()
        required_prior_columns = {"security_id", "trading_status"}
        missing_prior_columns = required_prior_columns - set(prior_master.columns)
        if missing_prior_columns:
            raise ValueError(
                "previous Security Master is missing columns: "
                f"{sorted(missing_prior_columns)}"
            )
        prior_master["security_id"] = prior_master["security_id"].astype(str)
        prior_master["trading_status"] = (
            prior_master["trading_status"].fillna("").astype(str).str.upper()
        )
        current_ids = set(master["security_id"].astype(str))
        carried = prior_master.loc[
            prior_master["trading_status"].isin({"INACTIVE", "DELISTED"})
            & prior_master["security_id"].isin(approved_excluded_ids)
            & ~prior_master["security_id"].isin(current_ids)
        ].copy()
        carried_forward_ids = set(carried["security_id"])
        if carried_forward_ids:
            master = pd.concat([master, carried], ignore_index=True).drop_duplicates(
                "security_id", keep="first"
            )

            def carry_history(
                current: pd.DataFrame,
                previous: pd.DataFrame | None,
                keys: list[str],
            ) -> pd.DataFrame:
                if previous is None or previous.empty:
                    return current
                if "security_id" not in previous.columns:
                    raise ValueError(
                        "previous Security Master child frame has no security_id"
                    )
                retained = previous.loc[
                    previous["security_id"].astype(str).isin(carried_forward_ids)
                ].copy()
                if retained.empty:
                    return current
                return pd.concat([current, retained], ignore_index=True).drop_duplicates(
                    keys, keep="first"
                )

            symbols = carry_history(
                symbols,
                previous_symbols,
                ["security_id", "ticker", "effective_from", "effective_to"],
            )
            classifications = carry_history(
                classifications,
                previous_classifications,
                ["security_id", "effective_from", "effective_to"],
            )
            identity_keys = carry_history(
                identity_keys,
                previous_identity_keys,
                ["security_id", "key_type", "key_value"],
            )
    symbols, history_policy = apply_research_history_policy(
        master,
        symbols,
        research_history_policy,
        target_session=target,
    )
    candidate_key_conflicts = _identity_key_conflicts(identity_keys)
    quarantined_identity_keys = [
        item for item in candidate_key_conflicts
        if item["key_type"] in {"CUSIP", "ISIN"}
    ]
    quarantined_pairs = {
        (item["key_type"], item["key_value"])
        for item in quarantined_identity_keys
    }
    if quarantined_pairs:
        identity_keys = identity_keys.loc[
            ~identity_keys.apply(
                lambda row: (str(row["key_type"]), str(row["key_value"]))
                in quarantined_pairs,
                axis=1,
            )
        ].copy()
    remaining_identity_conflicts = _identity_key_conflicts(identity_keys)
    master = master[[
        "security_id", "issuer_id", "current_ticker", "name", "asset_type",
        "primary_exchange", "country", "currency", "cik", "isin", "cusip",
        "listing_date", "delisting_date", "trading_status", "source",
        "source_asof", "updated_at",
    ]]
    symbols = symbols[[
        "security_id", "ticker", "exchange", "effective_from", "effective_to",
        "is_primary", "event_type", "source", "source_asof",
    ]]
    classifications = classifications[[
        "security_id", "sector", "sub_industry", "effective_from",
        "effective_to", "knowledge_date", "classification_policy", "source",
        "source_asof",
    ]]
    identity_keys = identity_keys[[
        "security_id", "key_type", "key_value", "source", "source_asof",
    ]]
    interval_conflicts = _interval_conflicts(symbols)
    current_stocks = master.loc[
        master["trading_status"].eq("ACTIVE") & master["asset_type"].eq("STOCK")
    ]
    classification_known = current_stocks["security_id"].isin(
        classifications.loc[
            classifications["sector"].ne(UNKNOWN_CLASSIFICATION), "security_id"
        ]
    )
    name_coverage = float(current_stocks["name"].ne("").mean()) if len(current_stocks) else 0.0
    classification_coverage = float(classification_known.mean()) if len(current_stocks) else 0.0
    identity_security_coverage = float(
        master["security_id"].isin(identity_keys["security_id"]).mean()
    ) if len(master) else 0.0
    quality = {
        "status": "PASS",
        "target_session": target.date().isoformat(),
        "security_count": int(len(master)),
        "active_stock_count": int(len(current_stocks)),
        "symbol_history_rows": int(len(symbols)),
        "identity_key_rows": int(len(identity_keys)),
        "active_stock_name_coverage": name_coverage,
        "active_stock_classification_coverage": classification_coverage,
        "duplicate_security_ids": int(master["security_id"].duplicated().sum()),
        "identity_conflicts": sorted(set(
            lineage_conflicts + event_identity_conflicts + [
                "identity key {key_type}:{key_value} maps to {security_ids}".format(
                    key_type=item["key_type"],
                    key_value=item["key_value"],
                    security_ids=",".join(item["security_ids"]),
                )
                for item in remaining_identity_conflicts
            ]
        )),
        "previous_ambiguous_identity_keys": previous_ambiguous_keys,
        "carried_forward_excluded_security_count": len(carried_forward_ids),
        "carried_forward_excluded_security_ids": sorted(carried_forward_ids),
        "shared_issue_identity_groups": shared_issue_groups,
        "quarantined_identity_keys": quarantined_identity_keys,
        "identity_security_coverage": identity_security_coverage,
        "ticker_interval_conflicts": sorted(set(interval_conflicts)),
        "symbol_change_diagnostics": lineage_diagnostics,
        "classification_policy": CLASSIFICATION_POLICY,
        "invalid_delisted_listing_dates": invalid_delisted_listing_dates,
        "research_history_policy_rows": int(len(history_policy)),
        "prospective_only_count": int(
            history_policy["policy"].eq("PROSPECTIVE_ONLY").sum()
        ) if not history_policy.empty else 0,
        "excluded_unverifiable_history_count": int(
            history_policy["policy"].eq(
                "EXCLUDED_UNVERIFIABLE_HISTORY"
            ).sum()
        ) if not history_policy.empty else 0,
    }
    failures: list[str] = []
    if quality["active_stock_count"] < int(minimum_active_stocks):
        failures.append(f"active_stock_count below {int(minimum_active_stocks)}")
    if quality["duplicate_security_ids"]:
        failures.append("duplicate security_id")
    if quality["identity_conflicts"]:
        failures.append("identity-key conflicts")
    if identity_security_coverage < 1.0:
        failures.append("security identity-key coverage below 100%")
    if quality["ticker_interval_conflicts"]:
        failures.append("overlapping ticker intervals")
    if name_coverage < float(minimum_name_coverage):
        failures.append(
            f"active stock name coverage below {float(minimum_name_coverage):.2%}"
        )
    if classification_coverage < float(minimum_classification_coverage):
        failures.append(
            "active stock classification coverage below "
            f"{float(minimum_classification_coverage):.2%}"
        )
    quality["failures"] = failures
    quality["status"] = "PASS" if not failures else "FAIL"
    return SecurityMasterCandidate(
        target_session=target.date(),
        master=master.sort_values(["trading_status", "current_ticker"]).reset_index(drop=True),
        symbols=symbols.sort_values(["ticker", "effective_from"], na_position="first").reset_index(drop=True),
        classifications=classifications.sort_values(["security_id", "effective_from"], na_position="first").reset_index(drop=True),
        identity_keys=identity_keys.sort_values(["key_type", "key_value", "security_id"]).reset_index(drop=True),
        history_policy=(
            history_policy.reset_index(drop=True)
            if not history_policy.empty else empty_history_policy()
        ),
        quality=quality,
    )


class SecurityMasterStore:
    """Single-writer DuckDB catalog plus immutable Parquet snapshots."""

    def __init__(self, catalog_path: str | Path, snapshot_root: str | Path):
        self.catalog_path = Path(catalog_path).resolve()
        self.snapshot_root = Path(snapshot_root).resolve()

    def _connect(self, *, read_only: bool) -> duckdb.DuckDBPyConnection:
        if read_only and not self.catalog_path.exists():
            raise FileNotFoundError(f"DuckDB catalog does not exist: {self.catalog_path}")
        if not read_only:
            self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        attempts = 5 if read_only else 10
        for attempt in range(attempts):
            try:
                return duckdb.connect(str(self.catalog_path), read_only=read_only)
            except Exception:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(0.2 * min(attempt + 1, 5))
        raise AssertionError("unreachable")

    def initialize(self) -> None:
        connection = self._connect(read_only=False)
        try:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS security_master_generations (
                    generation_id VARCHAR PRIMARY KEY,
                    target_session DATE NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    status VARCHAR NOT NULL,
                    row_count BIGINT NOT NULL,
                    active_count BIGINT NOT NULL,
                    master_path VARCHAR NOT NULL,
                    symbols_path VARCHAR NOT NULL,
                    classifications_path VARCHAR NOT NULL,
                    identity_keys_path VARCHAR NOT NULL,
                    manifest_path VARCHAR NOT NULL,
                    master_sha256 VARCHAR NOT NULL,
                    symbols_sha256 VARCHAR NOT NULL,
                    classifications_sha256 VARCHAR NOT NULL,
                    identity_keys_sha256 VARCHAR NOT NULL,
                    manifest_sha256 VARCHAR NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS published_security_master (
                    singleton BOOLEAN PRIMARY KEY,
                    generation_id VARCHAR NOT NULL,
                    published_at TIMESTAMPTZ NOT NULL,
                    CHECK (singleton = TRUE)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS security_master (
                    security_id VARCHAR PRIMARY KEY,
                    issuer_id VARCHAR,
                    current_ticker VARCHAR NOT NULL,
                    name VARCHAR NOT NULL,
                    asset_type VARCHAR NOT NULL,
                    primary_exchange VARCHAR NOT NULL,
                    country VARCHAR,
                    currency VARCHAR,
                    cik VARCHAR,
                    isin VARCHAR,
                    cusip VARCHAR,
                    listing_date DATE,
                    delisting_date DATE,
                    trading_status VARCHAR NOT NULL,
                    source VARCHAR NOT NULL,
                    source_asof DATE NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS security_symbol_history (
                    security_id VARCHAR NOT NULL,
                    ticker VARCHAR NOT NULL,
                    exchange VARCHAR,
                    effective_from DATE,
                    effective_to DATE,
                    is_primary BOOLEAN NOT NULL,
                    event_type VARCHAR NOT NULL,
                    source VARCHAR NOT NULL,
                    source_asof DATE NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS security_classification_history (
                    security_id VARCHAR NOT NULL,
                    sector VARCHAR NOT NULL,
                    sub_industry VARCHAR NOT NULL,
                    effective_from DATE,
                    effective_to DATE,
                    knowledge_date DATE NOT NULL,
                    classification_policy VARCHAR NOT NULL,
                    source VARCHAR NOT NULL,
                    source_asof DATE NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS security_identity_keys (
                    security_id VARCHAR NOT NULL,
                    key_type VARCHAR NOT NULL,
                    key_value VARCHAR NOT NULL,
                    source VARCHAR NOT NULL,
                    source_asof DATE NOT NULL
                )
            """)
        finally:
            connection.close()

    def load_current_identity_keys(self) -> pd.DataFrame:
        self.initialize()
        connection = self._connect(read_only=True)
        try:
            return connection.execute("SELECT * FROM security_identity_keys").df()
        finally:
            connection.close()

    def try_load_current_identity_keys(self) -> pd.DataFrame:
        """Read existing keys without creating catalog tables."""
        if not self.catalog_path.exists():
            return pd.DataFrame()
        connection = self._connect(read_only=True)
        try:
            exists = connection.execute("""
                SELECT count(*)
                FROM information_schema.tables
                WHERE table_name = 'security_identity_keys'
            """).fetchone()[0]
            if not exists:
                return pd.DataFrame()
            return connection.execute("SELECT * FROM security_identity_keys").df()
        finally:
            connection.close()

    def publish(self, candidate: SecurityMasterCandidate) -> SecurityMasterGeneration:
        if candidate.quality.get("status") != "PASS":
            raise RuntimeError(
                "Security Master candidate failed quality gates: "
                f"{candidate.quality.get('failures')}"
            )
        self.initialize()
        generation_id = uuid.uuid4().hex
        created_at = _utc_now()
        self.snapshot_root.mkdir(parents=True, exist_ok=True)
        staging = self.snapshot_root / f".staging_{generation_id}"
        destination = self.snapshot_root / f"generation={generation_id}"
        if staging.exists() or destination.exists():
            raise FileExistsError(f"Security Master generation already exists: {generation_id}")
        staging.mkdir(parents=True)
        try:
            artifacts = {
                "master": staging / "security_master.parquet",
                "symbols": staging / "security_symbol_history.parquet",
                "classifications": staging / "security_classification_history.parquet",
                "identity_keys": staging / "security_identity_keys.parquet",
                "history_policy": staging / "research_history_policy.parquet",
            }
            candidate.master.to_parquet(artifacts["master"], index=False)
            candidate.symbols.to_parquet(artifacts["symbols"], index=False)
            candidate.classifications.to_parquet(artifacts["classifications"], index=False)
            candidate.identity_keys.to_parquet(artifacts["identity_keys"], index=False)
            candidate.history_policy.to_parquet(
                artifacts["history_policy"], index=False
            )
            hashes = {name: _file_sha256(path) for name, path in artifacts.items()}
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "generation_id": generation_id,
                "target_session": candidate.target_session.isoformat(),
                "created_at": created_at.isoformat(),
                "status": "PUBLISHED",
                "classification_policy": CLASSIFICATION_POLICY,
                "quality": candidate.quality,
                "artifacts": {
                    name: {
                        "file": path.name,
                        "sha256": hashes[name],
                        "rows": int(len(getattr(candidate, name))),
                    }
                    for name, path in artifacts.items()
                },
            }
            manifest_path = staging / "manifest.json"
            atomic_save_json(manifest, manifest_path)
            manifest_sha256 = _file_sha256(manifest_path)
            os.replace(staging, destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        generation = SecurityMasterGeneration(
            generation_id=generation_id,
            target_session=candidate.target_session,
            created_at=created_at,
            status="PUBLISHED",
            row_count=len(candidate.master),
            active_count=int(candidate.master["trading_status"].eq("ACTIVE").sum()),
            master_path=str(destination / artifacts["master"].name),
            symbols_path=str(destination / artifacts["symbols"].name),
            classifications_path=str(destination / artifacts["classifications"].name),
            identity_keys_path=str(destination / artifacts["identity_keys"].name),
            manifest_path=str(destination / "manifest.json"),
            master_sha256=hashes["master"],
            symbols_sha256=hashes["symbols"],
            classifications_sha256=hashes["classifications"],
            identity_keys_sha256=hashes["identity_keys"],
            manifest_sha256=manifest_sha256,
        )
        connection = self._connect(read_only=False)
        try:
            connection.register("candidate_master", candidate.master)
            connection.register("candidate_symbols", candidate.symbols)
            connection.register("candidate_classifications", candidate.classifications)
            connection.register("candidate_identity_keys", candidate.identity_keys)
            connection.execute("BEGIN TRANSACTION")
            connection.execute("""
                INSERT INTO security_master_generations VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, [
                generation.generation_id,
                generation.target_session,
                generation.created_at,
                generation.status,
                generation.row_count,
                generation.active_count,
                generation.master_path,
                generation.symbols_path,
                generation.classifications_path,
                generation.identity_keys_path,
                generation.manifest_path,
                generation.master_sha256,
                generation.symbols_sha256,
                generation.classifications_sha256,
                generation.identity_keys_sha256,
                generation.manifest_sha256,
            ])
            for table, relation in (
                ("security_master", "candidate_master"),
                ("security_symbol_history", "candidate_symbols"),
                ("security_classification_history", "candidate_classifications"),
                ("security_identity_keys", "candidate_identity_keys"),
            ):
                connection.execute(f"DELETE FROM {table}")
                connection.execute(f"INSERT INTO {table} SELECT * FROM {relation}")
            connection.execute("DELETE FROM published_security_master")
            connection.execute(
                "INSERT INTO published_security_master VALUES (TRUE, ?, ?)",
                [generation.generation_id, _utc_now()],
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return generation

    def published_generation(self) -> SecurityMasterGeneration:
        connection = self._connect(read_only=True)
        try:
            try:
                row = connection.execute("""
                    SELECT g.*
                    FROM published_security_master AS p
                    JOIN security_master_generations AS g
                      ON g.generation_id = p.generation_id
                    WHERE p.singleton = TRUE
                """).fetchone()
            except duckdb.CatalogException as exc:
                raise FileNotFoundError(
                    "No Security Master generation is published"
                ) from exc
        finally:
            connection.close()
        if row is None:
            raise FileNotFoundError("No Security Master generation is published")
        return SecurityMasterGeneration(
            generation_id=str(row[0]),
            target_session=pd.Timestamp(row[1]).date(),
            created_at=pd.Timestamp(row[2]).to_pydatetime(),
            status=str(row[3]),
            row_count=int(row[4]),
            active_count=int(row[5]),
            master_path=str(row[6]),
            symbols_path=str(row[7]),
            classifications_path=str(row[8]),
            identity_keys_path=str(row[9]),
            manifest_path=str(row[10]),
            master_sha256=str(row[11]),
            symbols_sha256=str(row[12]),
            classifications_sha256=str(row[13]),
            identity_keys_sha256=str(row[14]),
            manifest_sha256=str(row[15]),
        )

    def load_published(self) -> tuple[SecurityMasterGeneration, dict[str, pd.DataFrame]]:
        generation = self.published_generation()
        paths = {
            "master": (Path(generation.master_path), generation.master_sha256),
            "symbols": (Path(generation.symbols_path), generation.symbols_sha256),
            "classifications": (
                Path(generation.classifications_path),
                generation.classifications_sha256,
            ),
            "identity_keys": (
                Path(generation.identity_keys_path),
                generation.identity_keys_sha256,
            ),
            "manifest": (Path(generation.manifest_path), generation.manifest_sha256),
        }
        for name, (path, expected) in paths.items():
            if not path.exists() or _file_sha256(path) != expected:
                raise RuntimeError(f"Security Master {name} hash verification failed")
        manifest = json.loads(paths["manifest"][0].read_text(encoding="utf-8"))
        if manifest.get("generation_id") != generation.generation_id:
            raise RuntimeError("Security Master manifest generation mismatch")
        frames = {
            name: pd.read_parquet(path)
            for name, (path, _) in paths.items()
            if name != "manifest"
        }
        policy_artifact = (manifest.get("artifacts") or {}).get(
            "history_policy"
        ) or {}
        if policy_artifact:
            policy_path = Path(generation.manifest_path).parent / str(
                policy_artifact.get("file") or ""
            )
            if (
                not policy_path.is_file()
                or _file_sha256(policy_path) != policy_artifact.get("sha256")
            ):
                raise RuntimeError(
                    "Security Master history_policy hash verification failed"
                )
            frames["history_policy"] = pd.read_parquet(policy_path)
        else:
            frames["history_policy"] = empty_history_policy()
        return generation, frames

    def resolve_ticker(
        self,
        ticker: str,
        *,
        asof: date | str | pd.Timestamp,
    ) -> SecurityResolution:
        """Resolve one ticker to exactly one security on a published date."""
        symbol = _ticker(ticker)
        if not symbol:
            raise ValueError("ticker is required")
        session = pd.Timestamp(asof).normalize()
        if pd.isna(session):
            raise ValueError("asof must be a valid date")
        generation, frames = self.load_published()
        if session > pd.Timestamp(generation.target_session):
            raise ValueError(
                "asof exceeds Security Master target_session: "
                f"{session.date()} > {generation.target_session}"
            )
        symbols = frames["symbols"].copy()
        starts = pd.to_datetime(symbols["effective_from"], errors="coerce")
        ends = pd.to_datetime(symbols["effective_to"], errors="coerce")
        matches = symbols.loc[
            symbols["ticker"].map(_ticker).eq(symbol)
            & (starts.isna() | starts.le(session))
            & (ends.isna() | ends.ge(session))
        ].copy()
        security_ids = sorted(matches["security_id"].astype(str).unique())
        if not security_ids:
            raise FileNotFoundError(
                f"Ticker {symbol} is not known on {session.date()}"
            )
        if len(security_ids) != 1:
            raise RuntimeError(
                f"Ticker {symbol} maps to multiple securities on "
                f"{session.date()}: {security_ids}"
            )
        security_id = security_ids[0]
        master = frames["master"].loc[
            frames["master"]["security_id"].astype(str).eq(security_id)
        ]
        if len(master) != 1:
            raise RuntimeError(
                f"Security Master row count for {security_id} is {len(master)}"
            )
        symbol_row = matches.loc[
            matches["security_id"].astype(str).eq(security_id)
        ].iloc[0]
        master_row = master.iloc[0]

        def _optional_date(value: Any) -> date | None:
            parsed = pd.to_datetime(value, errors="coerce")
            return None if pd.isna(parsed) else pd.Timestamp(parsed).date()

        return SecurityResolution(
            generation_id=generation.generation_id,
            asof=session.date(),
            security_id=security_id,
            queried_ticker=symbol,
            current_ticker=str(master_row["current_ticker"]),
            name=str(master_row["name"]),
            asset_type=str(master_row["asset_type"]),
            trading_status=str(master_row["trading_status"]),
            effective_from=_optional_date(symbol_row["effective_from"]),
            effective_to=_optional_date(symbol_row["effective_to"]),
        )


__all__ = [
    "SecurityMasterCandidate",
    "SecurityMasterGeneration",
    "SecurityResolution",
    "SecurityMasterStore",
    "build_security_master_candidate",
]
