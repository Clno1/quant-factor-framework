"""Immutable publications for universes derived from a parent data version.

The broad coverage dataset and the liquid comparison universe have different
lifecycles.  This store freezes PIT membership and the complete eligibility
audit separately, then atomically advances a small DuckDB pointer.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

import pandas as pd

from src.config import PROJECT_ROOT
from src.data.foundation import (
    DataFoundationError,
    DatasetVersion,
    MarketDataCatalog,
    MarketDataReader,
    QualityCheck,
)
from src.data.membership_state import (
    complete_snapshot_dates,
    resolve_membership_asof,
)
from src.data.security_master_store import SecurityMasterGeneration
from src.utils.file_lock import file_lock
from src.utils.identifiers import safe_path_component


DERIVED_UNIVERSE_SCHEMA_VERSION = 2
MEMBERSHIP_COLUMNS = [
    "date",
    "security_id",
    "ticker",
    "active",
    "selection_price",
    "adv20_usd",
    "valid_sessions_20d",
    "asset_type_pass",
    "price_pass",
    "liquidity_pass",
    "reason_codes",
    "snapshot_type",
    "source_data_version_id",
]


@dataclass(frozen=True)
class DerivedUniverseVersion:
    universe_version_id: str
    universe: str
    parent_dataset_version_id: str
    target_session: date
    status: str
    created_at: datetime
    methodology_version: str
    security_master_generation_id: str
    security_master_manifest_sha256: str
    membership_path: str
    membership_sha256: str
    eligibility_path: str
    eligibility_sha256: str
    manifest_path: str
    manifest_sha256: str
    snapshot_count: int
    membership_row_count: int
    current_member_count: int
    historical_member_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _portable_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(resolved)


def _resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _normalize_membership(
    frame: pd.DataFrame,
    *,
    universe: str,
    parent_version_id: str,
    target_session: date,
) -> pd.DataFrame:
    missing = sorted(set(MEMBERSHIP_COLUMNS) - set(frame.columns))
    if missing:
        raise DataFoundationError(
            f"[{universe}] derived membership is missing columns: {missing}"
        )
    out = frame.loc[:, MEMBERSHIP_COLUMNS].copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    if out["date"].isna().any():
        raise DataFoundationError(f"[{universe}] membership contains invalid dates")
    if out["date"].gt(pd.Timestamp(target_session)).any():
        raise DataFoundationError(f"[{universe}] membership contains future dates")
    for column in ("security_id", "ticker", "reason_codes", "snapshot_type"):
        out[column] = out[column].fillna("").astype(str).str.strip()
    out["ticker"] = out["ticker"].str.upper().str.replace(".", "-", regex=False)
    out["active"] = out["active"].fillna(False).astype(bool)
    for column in ("asset_type_pass", "price_pass", "liquidity_pass"):
        out[column] = out[column].fillna(False).astype(bool)
    for column in ("selection_price", "adv20_usd", "valid_sessions_20d"):
        out[column] = pd.to_numeric(out[column], errors="coerce")
    if out["security_id"].eq("").any() or out["ticker"].eq("").any():
        raise DataFoundationError(
            f"[{universe}] membership contains an empty security identity"
        )
    duplicate_count = int(out.duplicated(["date", "security_id"]).sum())
    if duplicate_count:
        raise DataFoundationError(
            f"[{universe}] membership has {duplicate_count} duplicate date/security rows"
        )
    inactive = ~out["active"]
    invalid_inactive = inactive & ~out["snapshot_type"].eq("FORCED_EXIT")
    invalid_active_event = out["active"] & out["snapshot_type"].eq("FORCED_EXIT")
    if invalid_inactive.any() or invalid_active_event.any():
        raise DataFoundationError(
            f"[{universe}] membership removal events violate the compact contract"
        )
    if not out["source_data_version_id"].astype(str).eq(parent_version_id).all():
        raise DataFoundationError(
            f"[{universe}] membership mixes parent dataset versions"
        )
    if out.empty:
        raise DataFoundationError(f"[{universe}] membership is empty")
    return out.sort_values(["date", "security_id"]).reset_index(drop=True)


def _normalize_eligibility(
    frame: pd.DataFrame,
    *,
    universe: str,
    parent_version_id: str,
    target_session: date,
) -> pd.DataFrame:
    required = {
        "date",
        "security_id",
        "ticker",
        "eligible",
        "reason_codes",
        "source_data_version_id",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise DataFoundationError(
            f"[{universe}] eligibility audit is missing columns: {missing}"
        )
    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    if out["date"].isna().any() or out["date"].gt(pd.Timestamp(target_session)).any():
        raise DataFoundationError(
            f"[{universe}] eligibility audit contains invalid or future dates"
        )
    out["security_id"] = out["security_id"].fillna("").astype(str).str.strip()
    out["ticker"] = (
        out["ticker"].fillna("").astype(str).str.strip().str.upper()
        .str.replace(".", "-", regex=False)
    )
    out["eligible"] = out["eligible"].fillna(False).astype(bool)
    out["reason_codes"] = out["reason_codes"].fillna("").astype(str)
    if out.duplicated(["date", "security_id"]).any():
        raise DataFoundationError(
            f"[{universe}] eligibility audit has duplicate date/security rows"
        )
    if not out["source_data_version_id"].astype(str).eq(parent_version_id).all():
        raise DataFoundationError(
            f"[{universe}] eligibility audit mixes parent dataset versions"
        )
    return out.sort_values(["date", "security_id"]).reset_index(drop=True)


class DerivedUniverseStore:
    """Publish and authenticate PIT universes derived from market data."""

    def __init__(
        self,
        *,
        catalog: MarketDataCatalog | None = None,
        snapshot_root: str | Path,
        market_reader: MarketDataReader | None = None,
    ):
        self.catalog = catalog or MarketDataCatalog()
        self.snapshot_root = Path(snapshot_root)
        self.market_reader = market_reader or MarketDataReader(catalog=self.catalog)

    def initialize(self) -> None:
        self.catalog.initialize()
        connection = self.catalog._connect(read_only=False)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS derived_universe_versions (
                    universe_version_id VARCHAR PRIMARY KEY,
                    universe VARCHAR NOT NULL,
                    parent_dataset_version_id VARCHAR NOT NULL,
                    target_session DATE NOT NULL,
                    status VARCHAR NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    methodology_version VARCHAR NOT NULL,
                    security_master_generation_id VARCHAR NOT NULL,
                    security_master_manifest_sha256 VARCHAR NOT NULL,
                    membership_path VARCHAR NOT NULL,
                    membership_sha256 VARCHAR NOT NULL,
                    eligibility_path VARCHAR NOT NULL,
                    eligibility_sha256 VARCHAR NOT NULL,
                    manifest_path VARCHAR NOT NULL,
                    manifest_sha256 VARCHAR NOT NULL,
                    snapshot_count BIGINT NOT NULL,
                    membership_row_count BIGINT NOT NULL,
                    current_member_count BIGINT NOT NULL,
                    historical_member_count BIGINT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS derived_universe_quality_checks (
                    universe_version_id VARCHAR NOT NULL,
                    check_name VARCHAR NOT NULL,
                    passed BOOLEAN NOT NULL,
                    observed_value VARCHAR,
                    threshold_value VARCHAR,
                    message VARCHAR NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS published_universe_versions (
                    universe VARCHAR PRIMARY KEY,
                    universe_version_id VARCHAR NOT NULL,
                    published_at TIMESTAMPTZ NOT NULL
                )
                """
            )
        finally:
            connection.close()

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> DerivedUniverseVersion:
        return DerivedUniverseVersion(
            universe_version_id=str(row[0]),
            universe=str(row[1]),
            parent_dataset_version_id=str(row[2]),
            target_session=pd.Timestamp(row[3]).date(),
            status=str(row[4]),
            created_at=pd.Timestamp(row[5]).to_pydatetime(),
            methodology_version=str(row[6]),
            security_master_generation_id=str(row[7]),
            security_master_manifest_sha256=str(row[8]),
            membership_path=str(row[9]),
            membership_sha256=str(row[10]),
            eligibility_path=str(row[11]),
            eligibility_sha256=str(row[12]),
            manifest_path=str(row[13]),
            manifest_sha256=str(row[14]),
            snapshot_count=int(row[15]),
            membership_row_count=int(row[16]),
            current_member_count=int(row[17]),
            historical_member_count=int(row[18]),
        )

    def publish(
        self,
        *,
        universe: str,
        parent_version: DatasetVersion,
        security_master: SecurityMasterGeneration,
        membership: pd.DataFrame,
        eligibility: pd.DataFrame,
        methodology_version: str,
        checks: list[QualityCheck],
    ) -> DerivedUniverseVersion:
        universe = safe_path_component(universe.upper(), label="universe")
        failed = [check for check in checks if not check.passed]
        if failed:
            detail = "; ".join(f"{item.name}: {item.message}" for item in failed)
            raise DataFoundationError(
                f"[{universe}] derived-universe publication rejected: {detail}"
            )
        if parent_version.status != "PUBLISHED":
            raise DataFoundationError("parent dataset version is not published")
        self.market_reader.verify_version(parent_version)
        target_session = parent_version.target_session
        normalized_membership = _normalize_membership(
            membership,
            universe=universe,
            parent_version_id=parent_version.version_id,
            target_session=target_session,
        )
        normalized_eligibility = _normalize_eligibility(
            eligibility,
            universe=universe,
            parent_version_id=parent_version.version_id,
            target_session=target_session,
        )
        current_state = resolve_membership_asof(
            normalized_membership,
            pd.Timestamp(target_session),
        )
        current_members = current_state["security_id"].nunique()
        complete_dates = complete_snapshot_dates(normalized_membership)
        removal_event_count = int((~normalized_membership["active"]).sum())
        version_id = uuid4().hex
        created_at = _utc_now()
        base = self.snapshot_root / universe
        staging = base / f".staging_{version_id}"
        destination = base / f"version={version_id}"
        if staging.exists() or destination.exists():
            raise FileExistsError(f"derived universe version exists: {version_id}")
        staging.mkdir(parents=True)
        try:
            membership_path = staging / "membership.parquet"
            eligibility_path = staging / "eligibility_audit.parquet"
            normalized_membership.to_parquet(membership_path, index=False)
            normalized_eligibility.to_parquet(eligibility_path, index=False)
            membership_sha = _file_sha256(membership_path)
            eligibility_sha = _file_sha256(eligibility_path)
            manifest = {
                "schema_version": DERIVED_UNIVERSE_SCHEMA_VERSION,
                "publication_type": "DERIVED_UNIVERSE",
                "universe_version_id": version_id,
                "universe": universe,
                "parent_dataset_version_id": parent_version.version_id,
                "parent_dataset_manifest_sha256": (
                    parent_version.manifest_checksum_sha256
                ),
                "target_session": target_session.isoformat(),
                "created_at": created_at.isoformat(),
                "methodology_version": methodology_version,
                "security_master_generation_id": security_master.generation_id,
                "security_master_manifest_sha256": security_master.manifest_sha256,
                "membership": {
                    "file": membership_path.name,
                    "sha256": membership_sha,
                    "rows": len(normalized_membership),
                    "contract": "COMPLETE_SNAPSHOT_PLUS_REMOVAL_EVENTS_V1",
                    "snapshot_count": len(complete_dates),
                    "removal_event_count": removal_event_count,
                    "current_member_count": int(current_members),
                    "historical_member_count": int(
                        normalized_membership.loc[
                            normalized_membership["active"], "security_id"
                        ].nunique()
                    ),
                },
                "eligibility": {
                    "file": eligibility_path.name,
                    "sha256": eligibility_sha,
                    "rows": len(normalized_eligibility),
                },
                "quality_checks": [check.to_dict() for check in checks],
            }
            manifest_path = staging / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            manifest_sha = _file_sha256(manifest_path)
            base.mkdir(parents=True, exist_ok=True)
            os.replace(staging, destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        version = DerivedUniverseVersion(
            universe_version_id=version_id,
            universe=universe,
            parent_dataset_version_id=parent_version.version_id,
            target_session=target_session,
            status="PUBLISHED",
            created_at=created_at,
            methodology_version=methodology_version,
            security_master_generation_id=security_master.generation_id,
            security_master_manifest_sha256=security_master.manifest_sha256,
            membership_path=_portable_path(destination / membership_path.name),
            membership_sha256=membership_sha,
            eligibility_path=_portable_path(destination / eligibility_path.name),
            eligibility_sha256=eligibility_sha,
            manifest_path=_portable_path(destination / manifest_path.name),
            manifest_sha256=manifest_sha,
            snapshot_count=int(len(complete_dates)),
            membership_row_count=len(normalized_membership),
            current_member_count=int(current_members),
            historical_member_count=int(
                normalized_membership.loc[
                    normalized_membership["active"], "security_id"
                ].nunique()
            ),
        )
        self.initialize()
        with file_lock(self.catalog.writer_lock_path):
            connection = self.catalog._connect(read_only=False)
            try:
                connection.execute("BEGIN TRANSACTION")
                connection.execute(
                    "INSERT INTO derived_universe_versions VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    list(asdict(version).values()),
                )
                for check in checks:
                    connection.execute(
                        "INSERT INTO derived_universe_quality_checks VALUES "
                        "(?, ?, ?, ?, ?, ?, ?)",
                        [
                            version_id,
                            check.name,
                            check.passed,
                            json.dumps(check.observed, ensure_ascii=False, default=str),
                            json.dumps(check.threshold, ensure_ascii=False, default=str),
                            check.message,
                            _utc_now(),
                        ],
                    )
                connection.execute(
                    "DELETE FROM published_universe_versions WHERE universe = ?",
                    [universe],
                )
                connection.execute(
                    "INSERT INTO published_universe_versions VALUES (?, ?, ?)",
                    [universe, version_id, _utc_now()],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        return version

    def latest(self, universe: str) -> DerivedUniverseVersion | None:
        universe = safe_path_component(universe.upper(), label="universe")
        if not self.catalog.path.exists():
            return None
        connection = self.catalog._connect(read_only=True)
        try:
            exists = connection.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name = 'published_universe_versions'"
            ).fetchone()[0]
            if not exists:
                return None
            row = connection.execute(
                """
                SELECT d.*
                FROM published_universe_versions AS p
                JOIN derived_universe_versions AS d
                  ON d.universe_version_id = p.universe_version_id
                WHERE p.universe = ?
                """,
                [universe],
            ).fetchone()
        finally:
            connection.close()
        return self._from_row(row) if row is not None else None

    def get(self, universe: str, version_id: str) -> DerivedUniverseVersion | None:
        universe = safe_path_component(universe.upper(), label="universe")
        if not self.catalog.path.exists():
            return None
        connection = self.catalog._connect(read_only=True)
        try:
            row = connection.execute(
                "SELECT * FROM derived_universe_versions "
                "WHERE universe = ? AND universe_version_id = ? "
                "AND status = 'PUBLISHED'",
                [universe, str(version_id)],
            ).fetchone()
        finally:
            connection.close()
        return self._from_row(row) if row is not None else None

    def verify(
        self,
        version: DerivedUniverseVersion,
        *,
        verify_parent_partition_children: bool = True,
    ) -> dict[str, Any]:
        artifacts = {
            "membership": (version.membership_path, version.membership_sha256),
            "eligibility": (version.eligibility_path, version.eligibility_sha256),
            "manifest": (version.manifest_path, version.manifest_sha256),
        }
        for label, (raw_path, expected) in artifacts.items():
            path = _resolve_path(raw_path)
            if not path.is_file() or _file_sha256(path) != expected:
                raise DataFoundationError(
                    f"[{version.universe}] {label} hash verification failed"
                )
        manifest = json.loads(
            _resolve_path(version.manifest_path).read_text(encoding="utf-8")
        )
        expected_fields = {
            "schema_version": DERIVED_UNIVERSE_SCHEMA_VERSION,
            "universe_version_id": version.universe_version_id,
            "universe": version.universe,
            "parent_dataset_version_id": version.parent_dataset_version_id,
            "target_session": version.target_session.isoformat(),
            "methodology_version": version.methodology_version,
            "security_master_generation_id": version.security_master_generation_id,
        }
        mismatches = [
            key
            for key, expected in expected_fields.items()
            if str(manifest.get(key)) != str(expected)
        ]
        if mismatches:
            raise DataFoundationError(
                f"[{version.universe}] derived manifest mismatch: {mismatches}"
            )
        parent = self.market_reader.require_version(
            "US_EQUITY_COVERAGE",
            version.parent_dataset_version_id,
            verify_partition_children=verify_parent_partition_children,
        )
        if (
            manifest.get("parent_dataset_manifest_sha256")
            != parent.manifest_checksum_sha256
        ):
            raise DataFoundationError(
                f"[{version.universe}] parent dataset manifest hash mismatch"
            )
        return manifest

    def require_latest(
        self,
        universe: str,
        *,
        verify_parent_partition_children: bool = True,
    ) -> DerivedUniverseVersion:
        version = self.latest(universe)
        if version is None:
            raise DataFoundationError(
                f"[{universe}] no derived universe publication exists"
            )
        self.verify(
            version,
            verify_parent_partition_children=verify_parent_partition_children,
        )
        return version

    def load_membership(
        self, universe: str, *, version_id: str | None = None
    ) -> pd.DataFrame:
        version = (
            self.get(universe, version_id)
            if version_id is not None
            else self.latest(universe)
        )
        if version is None:
            raise DataFoundationError(f"[{universe}] universe version not found")
        self.verify(version)
        frame = pd.read_parquet(_resolve_path(version.membership_path))
        frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
        return frame.sort_values(["date", "security_id"]).reset_index(drop=True)

    def load_eligibility(
        self, universe: str, *, version_id: str | None = None
    ) -> pd.DataFrame:
        version = (
            self.get(universe, version_id)
            if version_id is not None
            else self.latest(universe)
        )
        if version is None:
            raise DataFoundationError(f"[{universe}] universe version not found")
        self.verify(version)
        frame = pd.read_parquet(_resolve_path(version.eligibility_path))
        frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
        return frame.sort_values(["date", "security_id"]).reset_index(drop=True)


__all__ = [
    "DERIVED_UNIVERSE_SCHEMA_VERSION",
    "DerivedUniverseStore",
    "DerivedUniverseVersion",
    "MEMBERSHIP_COLUMNS",
]
