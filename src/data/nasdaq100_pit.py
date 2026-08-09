"""Build and publish the audited NASDAQ-100 point-in-time universe."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

import pandas as pd
import requests
import yaml

from src.config import CONFIG, PROJECT_ROOT
from src.data.fmp import (
    get_historical_nasdaq100_constituent_changes,
    get_nasdaq100_constituents,
)
from src.market_regime_research.artifacts import file_sha256, write_strict_json
from src.market_regime_research.models import DataContractError
from src.market_regime_research.pit import (
    publish_validated_membership,
    reconstruct_sp500_snapshots,
)
from src.utils.file_lock import file_lock
from src.utils.identifiers import InvalidResourceId, canonical_ticker, safe_path_component
from src.utils.market_calendar import (
    is_xnys_session,
    latest_completed_xnys_session,
    latest_publishable_xnys_session,
)


VERIFICATION_SCHEMA_VERSION = 1
MINIMUM_VERIFIED_EVENT_GROUPS = 10
NASDAQ100_PIT_SCOPE = "main_factor"
NASDAQ100_OFFICIAL_CURRENT_URL = (
    "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
)
_NASDAQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass(frozen=True)
class NASDAQ100PITPublication:
    run_id: str
    status: str
    target_session: date
    start: date
    diagnostics: dict[str, Any]
    run_dir: Path
    candidate_path: Path
    events_path: Path
    diagnostics_path: Path
    membership_path: Path | None = None
    metadata_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "universe": "NASDAQ100",
            "run_id": self.run_id,
            "status": self.status,
            "target_session": self.target_session.isoformat(),
            "start": self.start.isoformat(),
            "quality_status": self.diagnostics.get("quality_status"),
            "snapshots": self.diagnostics.get("snapshots"),
            "inconsistency_count": self.diagnostics.get("inconsistency_count"),
            "run_dir": str(self.run_dir),
            "candidate_path": str(self.candidate_path),
            "events_path": str(self.events_path),
            "diagnostics_path": str(self.diagnostics_path),
            "membership_path": str(self.membership_path) if self.membership_path else None,
            "metadata_path": str(self.metadata_path) if self.metadata_path else None,
        }


def _project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _main_start(value: str | date | pd.Timestamp | None) -> pd.Timestamp:
    configured = value or getattr(
        CONFIG.universe.point_in_time,
        "main_factor_start",
        "2020-01-01",
    )
    timestamp = pd.Timestamp(configured)
    if pd.isna(timestamp):
        raise ValueError("NASDAQ100 PIT start is invalid")
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_localize(None)
    return timestamp.normalize()


def _membership_target() -> Path:
    root = _project_path(CONFIG.universe.point_in_time.membership_dir)
    return root / f"{safe_path_component('NASDAQ100', label='universe')}.parquet"


def _frame_sha256(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {
                "columns": [str(column) for column in frame.columns],
                "dtypes": [str(dtype) for dtype in frame.dtypes],
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    digest.update(pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes())
    return f"sha256:{digest.hexdigest()}"


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _ticker(value: Any) -> str:
    return canonical_ticker(str(value).strip().upper().replace(".", "-"))


def fetch_official_nasdaq100_constituents(
    *,
    url: str = NASDAQ100_OFFICIAL_CURRENT_URL,
    timeout: float = 30.0,
) -> tuple[pd.DataFrame, pd.Timestamp]:
    """Fetch Nasdaq's public current component list for exact set comparison."""
    response = requests.get(url, headers=_NASDAQ_HEADERS, timeout=float(timeout))
    response.raise_for_status()
    payload = response.json()
    try:
        container = payload["data"]
        rows = container["data"]["rows"]
        asof = pd.Timestamp(container["date"])
        reported_total = int(container["totalrecords"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DataContractError(
            "Nasdaq official NASDAQ-100 response contract changed"
        ) from exc
    if not isinstance(rows, list) or not rows:
        raise DataContractError("Nasdaq official NASDAQ-100 response is empty")

    frame = pd.DataFrame(rows).rename(
        columns={"symbol": "ticker", "companyName": "name"}
    )
    if not {"ticker", "name"}.issubset(frame.columns):
        raise DataContractError("Nasdaq official component rows are incomplete")
    try:
        frame["ticker"] = frame["ticker"].map(_ticker)
    except InvalidResourceId as exc:
        raise DataContractError(
            "Nasdaq official component list contains an invalid ticker"
        ) from exc
    frame = frame[["ticker", "name"]].drop_duplicates("ticker")
    if len(frame) != reported_total:
        raise DataContractError(
            "Nasdaq official component count does not match totalrecords"
        )
    return frame.sort_values("ticker").reset_index(drop=True), asof.normalize()


def normalize_fmp_nasdaq100_changes(payload: pd.DataFrame) -> pd.DataFrame:
    """Normalize FMP events using dateAdded as the only effective-date field."""
    if payload is None or payload.empty:
        raise DataContractError("FMP historical NASDAQ-100 payload is empty")
    required = {
        "date",
        "dateAdded",
        "symbol",
        "addedSecurity",
        "removedTicker",
        "removedSecurity",
    }
    missing = required - set(payload.columns)
    if missing:
        raise DataContractError(
            f"FMP historical NASDAQ-100 changes missing fields: {sorted(missing)}"
        )

    source = payload.copy().reset_index(drop=True)
    provider_dates = pd.to_datetime(source["date"], errors="coerce", utc=True)
    effective_dates = pd.to_datetime(source["dateAdded"], errors="coerce", utc=True)
    if provider_dates.isna().any() or effective_dates.isna().any():
        raise DataContractError("FMP NASDAQ-100 changes contain invalid dates")

    rows: list[dict[str, Any]] = []
    for position, row in source.iterrows():
        added_name = _text(row.get("addedSecurity"))
        removed_name = _text(row.get("removedSecurity"))
        symbol = _text(row.get("symbol"))
        removed_symbol = _text(row.get("removedTicker"))
        reasons: list[str] = []
        warnings: list[str] = []

        added_ticker = ""
        if added_name:
            if not symbol:
                reasons.append("ADDITION_NAME_WITHOUT_SYMBOL")
            else:
                try:
                    added_ticker = _ticker(symbol)
                except InvalidResourceId:
                    reasons.append("INVALID_ADDED_TICKER")

        removed_ticker = ""
        if removed_symbol:
            try:
                removed_ticker = _ticker(removed_symbol)
            except InvalidResourceId:
                reasons.append("INVALID_REMOVED_TICKER")
        elif removed_name:
            reasons.append("REMOVAL_NAME_WITHOUT_TICKER")

        provider_date = provider_dates.iloc[position].tz_convert(None).normalize()
        effective_date = effective_dates.iloc[position].tz_convert(None).normalize()
        if provider_date != effective_date:
            warnings.append("PROVIDER_DATE_DIFFERS_FROM_EFFECTIVE_DATE")
        if not added_ticker and not removed_ticker:
            reasons.append("UNCLASSIFIED_EVENT")
        if added_ticker and removed_ticker and added_ticker == removed_ticker:
            reasons.append("SAME_TICKER_ADDED_AND_REMOVED")

        rows.append(
            {
                "effective_date": effective_date,
                "provider_date": provider_date,
                "effective_date_source": "dateAdded",
                "added_ticker": added_ticker or None,
                "removed_ticker": removed_ticker or None,
                "added_security": added_name or None,
                "removed_security": removed_name or None,
                "reason": _text(row.get("reason")) or None,
                "source_row": int(position),
                "quality_status": (
                    "ERROR" if reasons else "WARNING" if warnings else "OK"
                ),
                "reason_codes": reasons + warnings,
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["effective_date", "source_row"],
        ascending=[False, True],
    ).reset_index(drop=True)


def load_nasdaq100_verification_registry(
    path: str | Path | None = None,
) -> tuple[dict[str, Any], Path, str]:
    configured = path or getattr(
        CONFIG.universe.point_in_time,
        "nasdaq100_verification",
        "configs/nasdaq100_pit_verification.yaml",
    )
    source_path = _project_path(configured)
    raw = source_path.read_bytes()
    payload = yaml.safe_load(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise DataContractError("NASDAQ100 verification registry must be an object")
    if int(payload.get("schema_version") or 0) != VERIFICATION_SCHEMA_VERSION:
        raise DataContractError("Unsupported NASDAQ100 verification schema_version")
    if str(payload.get("universe") or "").upper() != "NASDAQ100":
        raise DataContractError("NASDAQ100 verification registry has wrong universe")

    official = payload.get("official_current")
    if not isinstance(official, Mapping) or not str(official.get("url") or "").startswith(
        "https://"
    ):
        raise DataContractError("NASDAQ100 official_current contract is invalid")
    event_groups = payload.get("verified_event_groups", [])
    if not isinstance(event_groups, list) or len(event_groups) < MINIMUM_VERIFIED_EVENT_GROUPS:
        raise DataContractError(
            "NASDAQ100 verification requires at least "
            f"{MINIMUM_VERIFIED_EVENT_GROUPS} official event groups"
        )
    seen: set[str] = set()
    for entry in event_groups:
        if not isinstance(entry, Mapping):
            raise DataContractError("NASDAQ100 verified event entries must be objects")
        event_id = str(entry.get("id") or "").strip()
        sources = entry.get("sources")
        if not event_id or event_id in seen:
            raise DataContractError("NASDAQ100 verified event id is missing or duplicated")
        if not isinstance(sources, list) or not sources or any(
            not str(source).startswith("https://") for source in sources
        ):
            raise DataContractError(f"NASDAQ100 verified event {event_id} lacks sources")
        pd.Timestamp(entry.get("effective_date"))
        for field in ("additions", "removals"):
            values = entry.get(field, [])
            if not isinstance(values, list):
                raise DataContractError(f"NASDAQ100 event {event_id} {field} must be a list")
            if any(not isinstance(value, str) for value in values):
                raise DataContractError(
                    f"NASDAQ100 event {event_id} {field} tickers must be quoted strings"
                )
            normalized = [_ticker(value) for value in values]
            if len(normalized) != len(set(normalized)):
                raise DataContractError(f"NASDAQ100 event {event_id} duplicates {field}")
        seen.add(event_id)
    return payload, source_path, f"sha256:{hashlib.sha256(raw).hexdigest()}"


def verify_nasdaq100_event_groups(
    events: pd.DataFrame,
    registry: Mapping[str, Any],
    *,
    asof: str | date | pd.Timestamp,
) -> list[dict[str, Any]]:
    """Require every dated official canary to match the provider event set."""
    asof_date = pd.Timestamp(asof).normalize()
    audit: list[dict[str, Any]] = []
    for entry in registry.get("verified_event_groups", []):
        event_date = pd.Timestamp(entry["effective_date"]).normalize()
        if event_date > asof_date:
            audit.append(
                {
                    "id": entry["id"],
                    "effective_date": event_date.date().isoformat(),
                    "status": "FUTURE_NOT_REQUIRED",
                    "sources": list(entry["sources"]),
                }
            )
            continue
        day = events.loc[events["effective_date"].eq(event_date)]
        observed_additions = sorted(day["added_ticker"].dropna().astype(str).unique())
        observed_removals = sorted(day["removed_ticker"].dropna().astype(str).unique())
        expected_additions = sorted(_ticker(value) for value in entry.get("additions", []))
        expected_removals = sorted(_ticker(value) for value in entry.get("removals", []))
        if observed_additions != expected_additions or observed_removals != expected_removals:
            raise DataContractError(
                f"NASDAQ100 verified event {entry['id']} drifted: "
                f"additions={observed_additions}, removals={observed_removals}"
            )
        audit.append(
            {
                "id": entry["id"],
                "effective_date": event_date.date().isoformat(),
                "status": "MATCH",
                "additions": observed_additions,
                "removals": observed_removals,
                "sources": list(entry["sources"]),
            }
        )
    return audit


def current_constituent_diagnostics(
    provider: pd.DataFrame,
    official: pd.DataFrame,
) -> dict[str, Any]:
    if "ticker" not in provider.columns or "ticker" not in official.columns:
        raise DataContractError("Current constituent frames require ticker")
    provider_set = {_ticker(value) for value in provider["ticker"]}
    official_set = {_ticker(value) for value in official["ticker"]}
    provider_only = sorted(provider_set - official_set)
    official_only = sorted(official_set - provider_set)
    diagnostics = {
        "quality_status": "PASS" if not provider_only and not official_only else "FAIL",
        "provider_members": len(provider_set),
        "official_members": len(official_set),
        "provider_only": provider_only,
        "official_only": official_only,
    }
    return diagnostics


def compare_current_constituents(
    provider: pd.DataFrame,
    official: pd.DataFrame,
) -> dict[str, Any]:
    diagnostics = current_constituent_diagnostics(provider, official)
    if diagnostics["quality_status"] != "PASS":
        raise DataContractError(
            "FMP and Nasdaq official NASDAQ-100 current constituents differ: "
            f"provider_only={diagnostics['provider_only']}, "
            f"official_only={diagnostics['official_only']}"
        )
    return diagnostics


def build_main_nasdaq100_pit(
    *,
    target_session: str | date | pd.Timestamp | None = None,
    start: str | date | pd.Timestamp | None = None,
    candidate_only: bool = False,
    verification_path: str | Path | None = None,
    current_frame: pd.DataFrame | None = None,
    official_frame: pd.DataFrame | None = None,
    official_asof: str | date | pd.Timestamp | None = None,
    changes_frame: pd.DataFrame | None = None,
) -> NASDAQ100PITPublication:
    """Build, audit, and optionally publish NASDAQ100 PIT membership."""
    delay = int(getattr(CONFIG.data.foundation, "close_delay_minutes", 120))
    target = (
        latest_publishable_xnys_session(delay_minutes=delay)
        if target_session is None
        else pd.Timestamp(target_session)
    )
    if target.tzinfo is not None:
        target = target.tz_localize(None)
    target = target.normalize()
    if not is_xnys_session(target):
        raise ValueError(f"{target.date()} is not an XNYS trading session")
    using_live_current = current_frame is None or official_frame is None or changes_frame is None
    if using_live_current and target != latest_completed_xnys_session():
        raise ValueError(
            "Current NASDAQ100 snapshots can only publish the latest completed session"
        )

    start_date = _main_start(start)
    if start_date > target:
        raise ValueError("NASDAQ100 PIT start is after target session")
    registry, registry_path, registry_sha256 = load_nasdaq100_verification_registry(
        verification_path
    )
    official_contract = registry["official_current"]
    provider = current_frame.copy() if current_frame is not None else get_nasdaq100_constituents()
    if official_frame is None:
        official, resolved_official_asof = fetch_official_nasdaq100_constituents(
            url=str(official_contract["url"])
        )
    else:
        official = official_frame.copy()
        resolved_official_asof = pd.Timestamp(official_asof or target)
    if resolved_official_asof.tzinfo is not None:
        resolved_official_asof = resolved_official_asof.tz_localize(None)
    resolved_official_asof = resolved_official_asof.normalize()
    maximum_staleness = int(official_contract["maximum_staleness_calendar_days"])
    if resolved_official_asof > target or (target - resolved_official_asof).days > maximum_staleness:
        raise DataContractError(
            f"Nasdaq official current list is stale for target {target.date()}: "
            f"official_asof={resolved_official_asof.date()}"
        )
    changes = (
        changes_frame.copy()
        if changes_frame is not None
        else get_historical_nasdaq100_constituent_changes()
    )

    run_id = f"pit_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{uuid4().hex[:8]}"
    raw_root = _project_path(CONFIG.data.raw_dir)
    run_dir = raw_root / "pit" / "NASDAQ100" / f"asof={target.date()}" / f"run={run_id}"
    lock_path = _membership_target().parent / ".nasdaq100-pit-writer.lock"

    with file_lock(lock_path):
        run_dir.mkdir(parents=True, exist_ok=False)
        provider_path = run_dir / "fmp_current_constituents.parquet"
        official_path = run_dir / "nasdaq_official_current_constituents.parquet"
        changes_path = run_dir / "provider_changes.parquet"
        events_path = run_dir / "normalized_events.parquet"
        candidate_path = run_dir / "candidate_membership.parquet"
        diagnostics_path = run_dir / "diagnostics.json"
        verification_audit_path = run_dir / "verification_audit.json"
        provider.to_parquet(provider_path, compression="snappy", index=False)
        official.to_parquet(official_path, compression="snappy", index=False)
        changes.to_parquet(changes_path, compression="snappy", index=False)

        current_audit = current_constituent_diagnostics(provider, official)
        if current_audit["quality_status"] != "PASS":
            diagnostics = {
                "quality_status": "FAIL",
                "strict": True,
                "scope": NASDAQ100_PIT_SCOPE,
                "asof": target.date().isoformat(),
                "start": start_date.date().isoformat(),
                "official_asof": resolved_official_asof.date().isoformat(),
                "publication_gate": "CURRENT_CONSTITUENT_EXACT_MATCH",
                "failure_reason": "FMP_CURRENT_SNAPSHOT_LAGS_NASDAQ_OFFICIAL",
                "current_constituent_comparison": current_audit,
                "verification_registry": str(registry_path),
                "verification_registry_sha256": registry_sha256,
                "inconsistency_count": 1,
                "inconsistencies": [
                    {
                        "type": "CURRENT_CONSTITUENT_MISMATCH",
                        "provider_only": current_audit["provider_only"],
                        "official_only": current_audit["official_only"],
                    }
                ],
            }
            write_strict_json(diagnostics_path, diagnostics)
            return NASDAQ100PITPublication(
                run_id=run_id,
                status="WAITING_FOR_PROVIDER",
                target_session=target.date(),
                start=start_date.date(),
                diagnostics=diagnostics,
                run_dir=run_dir,
                candidate_path=candidate_path,
                events_path=events_path,
                diagnostics_path=diagnostics_path,
            )
        events = normalize_fmp_nasdaq100_changes(changes)
        verification_audit = verify_nasdaq100_event_groups(
            events,
            registry,
            asof=target,
        )
        candidate = reconstruct_sp500_snapshots(
            provider,
            events,
            asof=target,
            start=start_date,
            min_snapshot_members=int(official_contract["minimum_members"]),
            max_snapshot_members=int(official_contract["maximum_members"]),
            strict=False,
        )
        diagnostics = dict(candidate.diagnostics)
        diagnostics.update(
            {
                "scope": NASDAQ100_PIT_SCOPE,
                "effective_date_field": "dateAdded",
                "official_asof": resolved_official_asof.date().isoformat(),
                "current_constituent_comparison": current_audit,
                "verification_registry": str(registry_path),
                "verification_registry_sha256": registry_sha256,
                "verified_event_groups": verification_audit,
            }
        )
        serialized_events = events.copy()
        serialized_events["reason_codes"] = serialized_events["reason_codes"].map(
            lambda values: json.dumps(values, ensure_ascii=False)
        )
        serialized_events.to_parquet(events_path, compression="snappy", index=False)
        candidate.membership.to_parquet(candidate_path, compression="snappy", index=False)
        write_strict_json(diagnostics_path, diagnostics)
        write_strict_json(
            verification_audit_path,
            {
                "registry": str(registry_path),
                "registry_sha256": registry_sha256,
                "entries": verification_audit,
            },
        )

        common = {
            "run_id": run_id,
            "target_session": target.date(),
            "start": start_date.date(),
            "diagnostics": diagnostics,
            "run_dir": run_dir,
            "candidate_path": candidate_path,
            "events_path": events_path,
            "diagnostics_path": diagnostics_path,
        }
        if diagnostics["quality_status"] != "PASS":
            return NASDAQ100PITPublication(status="FAILED", **common)
        if candidate_only:
            return NASDAQ100PITPublication(status="CANDIDATE_PASS", **common)

        strict_result = reconstruct_sp500_snapshots(
            provider,
            events,
            asof=target,
            start=start_date,
            min_snapshot_members=int(official_contract["minimum_members"]),
            max_snapshot_members=int(official_contract["maximum_members"]),
            strict=True,
        )
        strict_result.diagnostics.update(diagnostics)
        membership_path, metadata_path = publish_validated_membership(
            strict_result,
            _membership_target(),
            source_metadata={
                "scope": NASDAQ100_PIT_SCOPE,
                "provider": "FMP",
                "current_constituents_endpoint": "nasdaq-constituent",
                "historical_changes_endpoint": "historical-nasdaq-constituent",
                "effective_date_field": "dateAdded",
                "official_current_url": str(official_contract["url"]),
                "official_asof": resolved_official_asof.date().isoformat(),
                "current_comparison": current_audit,
                "provider_current_sha256": file_sha256(provider_path),
                "official_current_sha256": file_sha256(official_path),
                "changes_sha256": file_sha256(changes_path),
                "normalized_events_sha256": file_sha256(events_path),
                "provider_current_frame_sha256": _frame_sha256(provider),
                "official_current_frame_sha256": _frame_sha256(official),
                "verification_registry": str(registry_path),
                "verification_registry_sha256": registry_sha256,
                "verified_event_groups": verification_audit,
                "raw_run_dir": str(run_dir),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return NASDAQ100PITPublication(
            status="PUBLISHED",
            membership_path=membership_path,
            metadata_path=metadata_path,
            **common,
        )


__all__ = [
    "NASDAQ100_OFFICIAL_CURRENT_URL",
    "MINIMUM_VERIFIED_EVENT_GROUPS",
    "NASDAQ100PITPublication",
    "build_main_nasdaq100_pit",
    "compare_current_constituents",
    "current_constituent_diagnostics",
    "fetch_official_nasdaq100_constituents",
    "load_nasdaq100_verification_registry",
    "normalize_fmp_nasdaq100_changes",
    "verify_nasdaq100_event_groups",
]
