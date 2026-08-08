"""
Build the production S&P 500 point-in-time universe for main-factor research.

The FMP constituent endpoint is a current snapshot and its historical endpoint
is an event log.  The event log occasionally carries a when-issued ticker or
omits a later ticker-only corporate action.  Production corrections therefore
live in a reviewed YAML registry and are applied only when an exact dated event
matches.  Every raw payload, correction, diagnostic, and published membership
is hashed and retained for audit.

This module intentionally covers the main-factor window only.  The separate
1990-present market-regime study remains fail-closed until a permanent-security
master can resolve older ticker reuse.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

import pandas as pd
import yaml

from src.config import CONFIG, PROJECT_ROOT
from src.data.fmp import (
    get_historical_sp500_constituent_changes,
    get_sp500_constituents,
)
from src.market_regime_research.artifacts import (
    file_sha256,
    write_strict_json,
)
from src.market_regime_research.models import DataContractError
from src.market_regime_research.pit import (
    normalize_fmp_sp500_changes,
    publish_validated_membership,
    reconstruct_sp500_snapshots,
)
from src.utils.file_lock import file_lock
from src.utils.identifiers import canonical_ticker, safe_path_component
from src.utils.market_calendar import (
    is_xnys_session,
    latest_completed_xnys_session,
    latest_publishable_xnys_session,
)


CORRECTION_SCHEMA_VERSION = 1
MAIN_PIT_SCOPE = "main_factor"


@dataclass(frozen=True)
class MainSP500PITPublication:
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
            "membership_path": (
                str(self.membership_path) if self.membership_path else None
            ),
            "metadata_path": (
                str(self.metadata_path) if self.metadata_path else None
            ),
        }


def _project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _main_start(value: str | date | pd.Timestamp | None = None) -> pd.Timestamp:
    configured = (
        value
        if value is not None
        else getattr(
            CONFIG.universe.point_in_time,
            "main_factor_start",
            "2020-01-01",
        )
    )
    timestamp = pd.Timestamp(configured)
    if pd.isna(timestamp):
        raise ValueError("Main-factor PIT start is invalid")
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_localize(None)
    return timestamp.normalize()


def _membership_target(universe: str = "SP500") -> Path:
    root = _project_path(CONFIG.universe.point_in_time.membership_dir)
    name = safe_path_component(universe.upper(), label="universe")
    return root / f"{name}.parquet"


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


def load_sp500_pit_corrections(
    path: str | Path | None = None,
) -> tuple[dict[str, Any], Path, str]:
    """Load and strictly validate the reviewed correction registry."""
    configured = path or getattr(
        CONFIG.universe.point_in_time,
        "sp500_corrections",
        "configs/sp500_pit_corrections.yaml",
    )
    source_path = _project_path(configured)
    raw = source_path.read_bytes()
    payload = yaml.safe_load(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise DataContractError("SP500 PIT correction registry must be an object")
    if int(payload.get("schema_version") or 0) != CORRECTION_SCHEMA_VERSION:
        raise DataContractError("Unsupported SP500 PIT correction schema_version")
    if str(payload.get("universe") or "").strip().upper() != "SP500":
        raise DataContractError("SP500 PIT correction registry has wrong universe")

    seen_ids: set[str] = set()
    for section in ("event_ticker_corrections", "symbol_transitions"):
        entries = payload.get(section, [])
        if not isinstance(entries, list):
            raise DataContractError(f"{section} must be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise DataContractError(f"{section} entries must be objects")
            correction_id = str(entry.get("id") or "").strip()
            sources = entry.get("sources")
            if not correction_id or correction_id in seen_ids:
                raise DataContractError(
                    f"Missing or duplicate SP500 PIT correction id: {correction_id!r}"
                )
            if (
                not isinstance(sources, list)
                or not sources
                or any(
                    not str(source).startswith(("https://", "http://"))
                    for source in sources
                )
            ):
                raise DataContractError(
                    f"SP500 PIT correction {correction_id} requires source URLs"
                )
            try:
                pd.Timestamp(entry["effective_date"]).normalize()
            except (KeyError, TypeError, ValueError) as exc:
                raise DataContractError(
                    f"SP500 PIT correction {correction_id} has invalid date"
                ) from exc
            seen_ids.add(correction_id)
    return payload, source_path, f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _reason_codes(value: Any) -> list[str]:
    if isinstance(value, str):
        decoded = json.loads(value)
        return [str(item) for item in decoded]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if hasattr(value, "tolist"):
        decoded = value.tolist()
        if isinstance(decoded, list):
            return [str(item) for item in decoded]
    raise DataContractError("Normalized PIT reason_codes must be a list")


def apply_sp500_pit_corrections(
    changes: pd.DataFrame,
    registry: Mapping[str, Any],
    *,
    asof: str | date | pd.Timestamp,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """
    Apply exact reviewed event corrections and dated ticker transitions.

    A provider row that no longer matches its reviewed before/after value fails
    closed.  This prevents a broad alias from silently changing unrelated
    companies that happened to reuse the same ticker.
    """
    normalized = (
        changes.copy()
        if "effective_date" in changes.columns
        else normalize_fmp_sp500_changes(changes)
    )
    normalized["effective_date"] = pd.to_datetime(
        normalized["effective_date"],
        errors="coerce",
    ).dt.tz_localize(None).dt.normalize()
    if normalized["effective_date"].isna().any():
        raise DataContractError("Normalized PIT events contain invalid dates")
    normalized["reason_codes"] = normalized["reason_codes"].map(_reason_codes)
    asof_date = pd.Timestamp(asof)
    if asof_date.tzinfo is not None:
        asof_date = asof_date.tz_localize(None)
    asof_date = asof_date.normalize()
    audit: list[dict[str, Any]] = []

    allowed_fields = {"added_ticker", "removed_ticker"}
    for correction in registry.get("event_ticker_corrections", []):
        correction_id = str(correction["id"])
        field = str(correction.get("field") or "")
        if field not in allowed_fields:
            raise DataContractError(
                f"SP500 PIT correction {correction_id} has invalid field {field!r}"
            )
        effective_date = pd.Timestamp(correction["effective_date"]).normalize()
        provider_value = canonical_ticker(correction["provider_value"])
        corrected_value = canonical_ticker(correction["corrected_value"])
        security_column = (
            "added_security" if field == "added_ticker" else "removed_security"
        )
        matches = normalized["effective_date"].eq(effective_date)
        security_contains = str(correction.get("security_contains") or "").strip()
        if security_contains:
            security = normalized.get(
                security_column,
                pd.Series("", index=normalized.index),
            )
            matches &= security.fillna("").astype(str).str.contains(
                security_contains,
                case=False,
                regex=False,
            )
        positions = normalized.index[matches].tolist()
        if len(positions) != 1:
            raise DataContractError(
                f"SP500 PIT correction {correction_id} matched "
                f"{len(positions)} provider rows; expected exactly one"
            )
        position = positions[0]
        observed = str(normalized.at[position, field] or "").strip().upper()
        if observed == provider_value:
            normalized.at[position, field] = corrected_value
            codes = list(normalized.at[position, "reason_codes"])
            codes.append("REVIEWED_EVENT_TICKER_CORRECTION")
            normalized.at[position, "reason_codes"] = codes
            if normalized.at[position, "quality_status"] == "OK":
                normalized.at[position, "quality_status"] = "WARNING"
            action = "corrected"
        elif observed == corrected_value:
            action = "provider_already_corrected"
        else:
            raise DataContractError(
                f"SP500 PIT correction {correction_id} expected {field} "
                f"{provider_value} or {corrected_value}, observed {observed!r}"
            )
        audit.append(
            {
                "id": correction_id,
                "type": "event_ticker_correction",
                "effective_date": effective_date.date().isoformat(),
                "field": field,
                "provider_value": provider_value,
                "corrected_value": corrected_value,
                "action": action,
                "sources": list(correction["sources"]),
            }
        )

    next_source_row = (
        int(pd.to_numeric(normalized["source_row"], errors="raise").max()) + 1
        if not normalized.empty
        else 0
    )
    synthetic_rows: list[dict[str, Any]] = []
    for transition in registry.get("symbol_transitions", []):
        correction_id = str(transition["id"])
        effective_date = pd.Timestamp(transition["effective_date"]).normalize()
        added = canonical_ticker(transition["added_ticker"])
        removed = canonical_ticker(transition["removed_ticker"])
        if added == removed:
            raise DataContractError(
                f"SP500 PIT transition {correction_id} does not change ticker"
            )
        if effective_date > asof_date:
            audit.append(
                {
                    "id": correction_id,
                    "type": "symbol_transition",
                    "effective_date": effective_date.date().isoformat(),
                    "removed_ticker": removed,
                    "added_ticker": added,
                    "action": "future_not_applied",
                    "sources": list(transition["sources"]),
                }
            )
            continue
        existing = normalized[
            normalized["effective_date"].eq(effective_date)
            & normalized["added_ticker"].fillna("").astype(str).eq(added)
            & normalized["removed_ticker"].fillna("").astype(str).eq(removed)
        ]
        if existing.empty:
            security_name = str(transition.get("security_name") or "").strip() or None
            synthetic_rows.append(
                {
                    "effective_date": effective_date,
                    "added_ticker": added,
                    "removed_ticker": removed,
                    "added_security": security_name,
                    "removed_security": security_name,
                    "reason": str(transition.get("reason") or "").strip() or None,
                    "source_row": next_source_row,
                    "quality_status": "WARNING",
                    "reason_codes": ["REVIEWED_SYMBOL_TRANSITION"],
                }
            )
            next_source_row += 1
            action = "synthetic_event_added"
        elif len(existing) == 1:
            action = "provider_event_present"
        else:
            raise DataContractError(
                f"SP500 PIT transition {correction_id} matched duplicate events"
            )
        audit.append(
            {
                "id": correction_id,
                "type": "symbol_transition",
                "effective_date": effective_date.date().isoformat(),
                "removed_ticker": removed,
                "added_ticker": added,
                "action": action,
                "sources": list(transition["sources"]),
            }
        )

    if synthetic_rows:
        normalized = pd.concat(
            [normalized, pd.DataFrame(synthetic_rows)],
            ignore_index=True,
        )
    normalized = normalized.sort_values(
        ["effective_date", "source_row"],
        ascending=[False, True],
    ).reset_index(drop=True)
    return normalized, audit


def build_main_sp500_pit(
    *,
    target_session: str | date | pd.Timestamp | None = None,
    start: str | date | pd.Timestamp | None = None,
    candidate_only: bool = False,
    corrections_path: str | Path | None = None,
    current_frame: pd.DataFrame | None = None,
    changes_frame: pd.DataFrame | None = None,
) -> MainSP500PITPublication:
    """Build, audit, and optionally publish the main-factor SP500 PIT file."""
    delay = int(
        getattr(CONFIG.data.foundation, "close_delay_minutes", 120)
    )
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
    if current_frame is None or changes_frame is None:
        latest_completed = latest_completed_xnys_session()
        if target != latest_completed:
            raise ValueError(
                "FMP supplies a current constituent snapshot, so the PIT target "
                f"must be the latest completed XNYS session {latest_completed.date()}"
            )

    start_date = _main_start(start)
    if start_date > target:
        raise ValueError("Main-factor PIT start is after target session")
    registry, registry_path, registry_sha256 = load_sp500_pit_corrections(
        corrections_path
    )
    run_id = f"pit_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{uuid4().hex[:8]}"
    raw_root = _project_path(CONFIG.data.raw_dir)
    run_dir = (
        raw_root
        / "pit"
        / "SP500"
        / f"asof={target.date().isoformat()}"
        / f"run={run_id}"
    )
    lock_path = _membership_target().parent / ".sp500-pit-writer.lock"

    with file_lock(lock_path):
        current = (
            current_frame.copy()
            if current_frame is not None
            else get_sp500_constituents()
        )
        changes = (
            changes_frame.copy()
            if changes_frame is not None
            else get_historical_sp500_constituent_changes()
        )
        corrected_events, correction_audit = apply_sp500_pit_corrections(
            changes,
            registry,
            asof=target,
        )
        candidate = reconstruct_sp500_snapshots(
            current,
            corrected_events,
            asof=target,
            start=start_date,
            min_snapshot_members=450,
            max_snapshot_members=550,
            strict=False,
        )
        diagnostics = dict(candidate.diagnostics)
        diagnostics.update(
            {
                "scope": MAIN_PIT_SCOPE,
                "correction_registry": str(registry_path),
                "correction_registry_sha256": registry_sha256,
                "corrections_applied": correction_audit,
            }
        )

        run_dir.mkdir(parents=True, exist_ok=False)
        current_path = run_dir / "current_constituents.parquet"
        changes_path = run_dir / "provider_changes.parquet"
        events_path = run_dir / "normalized_events.parquet"
        candidate_path = run_dir / "candidate_membership.parquet"
        diagnostics_path = run_dir / "diagnostics.json"
        corrections_audit_path = run_dir / "corrections_audit.json"
        current.to_parquet(current_path, compression="snappy", index=False)
        changes.to_parquet(changes_path, compression="snappy", index=False)
        serialized_events = corrected_events.copy()
        serialized_events["reason_codes"] = serialized_events["reason_codes"].map(
            lambda values: json.dumps(values, ensure_ascii=False)
        )
        serialized_events.to_parquet(
            events_path,
            compression="snappy",
            index=False,
        )
        candidate.membership.to_parquet(
            candidate_path,
            compression="snappy",
            index=False,
        )
        write_strict_json(diagnostics_path, diagnostics)
        write_strict_json(
            corrections_audit_path,
            {
                "registry": str(registry_path),
                "registry_sha256": registry_sha256,
                "entries": correction_audit,
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
            return MainSP500PITPublication(status="FAILED", **common)
        if candidate_only:
            return MainSP500PITPublication(status="CANDIDATE_PASS", **common)

        strict_result = reconstruct_sp500_snapshots(
            current,
            corrected_events,
            asof=target,
            start=start_date,
            min_snapshot_members=450,
            max_snapshot_members=550,
            strict=True,
        )
        strict_result.diagnostics.update(
            {
                "scope": MAIN_PIT_SCOPE,
                "correction_registry": str(registry_path),
                "correction_registry_sha256": registry_sha256,
                "corrections_applied": correction_audit,
            }
        )
        membership_path, metadata_path = publish_validated_membership(
            strict_result,
            _membership_target(),
            source_metadata={
                "scope": MAIN_PIT_SCOPE,
                "provider": "FMP",
                "current_constituents_endpoint": "sp500-constituent",
                "historical_changes_endpoint": "historical-sp500-constituent",
                "current_rows": len(current),
                "change_rows": len(changes),
                "current_payload_sha256": _frame_sha256(current),
                "changes_payload_sha256": _frame_sha256(changes),
                "current_artifact_sha256": file_sha256(current_path),
                "changes_artifact_sha256": file_sha256(changes_path),
                "normalized_events_sha256": file_sha256(events_path),
                "correction_registry": str(registry_path),
                "correction_registry_sha256": registry_sha256,
                "corrections_applied": correction_audit,
                "raw_run_dir": str(run_dir),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return MainSP500PITPublication(
            status="PUBLISHED",
            membership_path=membership_path,
            metadata_path=metadata_path,
            **common,
        )


__all__ = [
    "CORRECTION_SCHEMA_VERSION",
    "MAIN_PIT_SCOPE",
    "MainSP500PITPublication",
    "apply_sp500_pit_corrections",
    "build_main_sp500_pit",
    "load_sp500_pit_corrections",
]
