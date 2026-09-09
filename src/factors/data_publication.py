"""Immutable long-form factor-data publications for broad US equities.

This publication is intentionally separate from ``research_publication.json``.
It proves that raw/clean observations are queryable under one authenticated
market-data, PIT-universe and Security Master identity.  It does not claim that
IC, ICIR, confidence or portfolio tests have passed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Iterable
from uuid import uuid4

import numpy as np
import pandas as pd

from src.config import CONFIG, PROJECT_ROOT
from src.data.foundation import (
    DataFoundationError,
    DatasetVersion,
    MarketDataReader,
    QualityCheck,
)
from src.data.security_master_store import SecurityMasterGeneration
from src.data.universe_publication import (
    DerivedUniverseStore,
    DerivedUniverseVersion,
)
from src.utils.file_lock import file_lock
from src.utils.identifiers import safe_path_component
from src.utils.io import atomic_save_json


FACTOR_DATA_SCHEMA_VERSION = 1
FACTOR_PARTITION_SCHEMA_VERSION = 1
FACTOR_DATA_PUBLICATION_FILE = "factor_data_publication.json"
FACTOR_DATA_MANIFEST_FILE = "manifest.json"
FACTOR_MANIFEST_FILE = "factor_manifest.json"

FACTOR_OBSERVATION_COLUMNS = [
    "date",
    "security_id",
    "ticker",
    "factor_id",
    "raw_value",
    "clean_value",
    "pit_member",
    "status",
]

FACTOR_OBSERVATION_STATUSES = {
    "VALID",
    "NOT_PIT_MEMBER",
    "CALCULATION_WINDOW_INSUFFICIENT",
    "RAW_MISSING",
    "CLEAN_MISSING",
    "CLASSIFICATION_MISSING",
    "DATA_QUALITY_REJECTED",
}


@dataclass(frozen=True)
class FactorPartition:
    factor_id: str
    path: str
    sha256: str
    row_count: int
    date_start: str
    date_end: str
    year: int
    month: int
    source_generation_id: str
    input_fingerprint_sha256: str | None = None
    input_fingerprint_method: str | None = None
    latest_raw_coverage: float | None = None
    latest_clean_coverage: float | None = None
    zero_std_cross_sections: int | None = None
    eligible_cross_sections: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(path: Path) -> str:
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


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _configured_output_root() -> Path:
    configured = Path(CONFIG.data.broad_factor_data.output_dir)
    return configured if configured.is_absolute() else PROJECT_ROOT / configured


def _normalize_factor_observations(
    frame: pd.DataFrame,
    *,
    factor_id: str,
    target_session: date,
) -> pd.DataFrame:
    missing = sorted(set(FACTOR_OBSERVATION_COLUMNS) - set(frame.columns))
    if missing:
        raise DataFoundationError(
            f"[{factor_id}] factor observations are missing columns: {missing}"
        )
    out = frame.loc[:, FACTOR_OBSERVATION_COLUMNS].copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    if out.empty or out["date"].isna().any():
        raise DataFoundationError(f"[{factor_id}] factor partition has invalid dates")
    if out["date"].gt(pd.Timestamp(target_session)).any():
        raise DataFoundationError(f"[{factor_id}] factor partition contains future rows")
    periods = out["date"].dt.to_period("M").unique()
    if len(periods) != 1:
        raise DataFoundationError(
            f"[{factor_id}] one factor partition must contain exactly one month"
        )
    for column in ("security_id", "ticker", "factor_id", "status"):
        out[column] = out[column].fillna("").astype(str).str.strip()
    out["ticker"] = (
        out["ticker"].str.upper().str.replace(".", "-", regex=False)
    )
    out["factor_id"] = out["factor_id"].str.upper()
    expected_factor = safe_path_component(factor_id.upper(), label="factor_id")
    if not out["factor_id"].eq(expected_factor).all():
        raise DataFoundationError(
            f"[{factor_id}] factor partition mixes factor identities"
        )
    if out["security_id"].eq("").any() or out["ticker"].eq("").any():
        raise DataFoundationError(
            f"[{factor_id}] factor partition contains an empty security identity"
        )
    duplicate_count = int(out.duplicated(["date", "security_id"]).sum())
    if duplicate_count:
        raise DataFoundationError(
            f"[{factor_id}] factor partition has {duplicate_count} duplicate rows"
        )
    for column in ("raw_value", "clean_value"):
        out[column] = pd.to_numeric(out[column], errors="coerce").astype(float)
        out[column] = out[column].where(np.isfinite(out[column]))
    out["pit_member"] = out["pit_member"].fillna(False).astype(bool)
    invalid_statuses = sorted(set(out["status"]) - FACTOR_OBSERVATION_STATUSES)
    if invalid_statuses:
        raise DataFoundationError(
            f"[{factor_id}] factor partition has invalid statuses: {invalid_statuses}"
        )
    invalid_clean = int((out["clean_value"].notna() & ~out["pit_member"]).sum())
    if invalid_clean:
        raise DataFoundationError(
            f"[{factor_id}] {invalid_clean} non-members contain clean values"
        )
    invalid_valid = int(
        (
            out["status"].eq("VALID")
            & (
                ~out["pit_member"]
                | out["raw_value"].isna()
                | out["clean_value"].isna()
            )
        ).sum()
    )
    if invalid_valid:
        raise DataFoundationError(
            f"[{factor_id}] {invalid_valid} VALID rows violate the value contract"
        )
    return out.sort_values(["date", "security_id"]).reset_index(drop=True)


class FactorDataStore:
    """Write and verify one atomic eight-factor broad-data publication."""

    def __init__(
        self,
        *,
        output_root: str | Path | None = None,
        market_reader: MarketDataReader | None = None,
        universe_store: DerivedUniverseStore | None = None,
    ):
        self.output_root = Path(output_root) if output_root else _configured_output_root()
        self.market_reader = market_reader or MarketDataReader()
        self.universe_store = universe_store or DerivedUniverseStore(
            catalog=self.market_reader.catalog,
            snapshot_root=CONFIG.abs_path(CONFIG.data.broad_universe.snapshot_dir),
            market_reader=self.market_reader,
        )
        self.lock_path = self.output_root / ".factor_data.lock"

    @property
    def publication_path(self) -> Path:
        return self.output_root / FACTOR_DATA_PUBLICATION_FILE

    def new_generation_id(self) -> str:
        return uuid4().hex

    def staging_directory(self, generation_id: str) -> Path:
        value = safe_path_component(generation_id, label="generation_id")
        return self.output_root / f".staging_{value}"

    def generation_directory(self, generation_id: str) -> Path:
        value = safe_path_component(generation_id, label="generation_id")
        return self.output_root / f"generation={value}"

    def staged_partition_path(
        self,
        partition: FactorPartition,
        *,
        generation_id: str,
    ) -> Path:
        final_generation = self.generation_directory(generation_id).resolve()
        final_path = _resolve_path(partition.path).resolve()
        try:
            relative = final_path.relative_to(final_generation)
        except ValueError as exc:
            raise DataFoundationError(
                "new factor partition path is outside its generation"
            ) from exc
        return self.staging_directory(generation_id) / relative

    def write_partition(
        self,
        frame: pd.DataFrame,
        *,
        generation_id: str,
        factor_id: str,
        target_session: date | str | pd.Timestamp,
        input_fingerprint_sha256: str | None = None,
        input_fingerprint_method: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> FactorPartition:
        target = pd.Timestamp(target_session).normalize()
        normalized = _normalize_factor_observations(
            frame,
            factor_id=factor_id,
            target_session=target.date(),
        )
        period = normalized["date"].dt.to_period("M").iloc[0]
        factor = safe_path_component(factor_id.upper(), label="factor_id")
        relative = (
            Path(f"factor_id={factor}")
            / f"year={period.year:04d}"
            / f"month={period.month:02d}"
            / "part.parquet"
        )
        staging = self.staging_directory(generation_id)
        path = staging / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".parquet.tmp")
        normalized.to_parquet(temporary, index=False, compression="snappy")
        os.replace(temporary, path)
        final_path = self.generation_directory(generation_id) / relative
        diagnostics = diagnostics or {}
        return FactorPartition(
            factor_id=factor,
            path=_portable_path(final_path),
            sha256=_sha256(path),
            row_count=len(normalized),
            date_start=normalized["date"].min().date().isoformat(),
            date_end=normalized["date"].max().date().isoformat(),
            year=int(period.year),
            month=int(period.month),
            source_generation_id=generation_id,
            input_fingerprint_sha256=input_fingerprint_sha256,
            input_fingerprint_method=input_fingerprint_method,
            latest_raw_coverage=(
                float(diagnostics["latest_raw_coverage"])
                if diagnostics.get("latest_raw_coverage") is not None
                else None
            ),
            latest_clean_coverage=(
                float(diagnostics["latest_clean_coverage"])
                if diagnostics.get("latest_clean_coverage") is not None
                else None
            ),
            zero_std_cross_sections=(
                int(diagnostics["zero_std_cross_sections"])
                if diagnostics.get("zero_std_cross_sections") is not None
                else None
            ),
            eligible_cross_sections=(
                int(diagnostics["eligible_cross_sections"])
                if diagnostics.get("eligible_cross_sections") is not None
                else None
            ),
        )

    @staticmethod
    def _validate_partition_set(
        factor_id: str,
        partitions: Iterable[FactorPartition],
        *,
        target_session: date,
    ) -> list[FactorPartition]:
        values = sorted(
            list(partitions),
            key=lambda item: (item.year, item.month, item.date_start),
        )
        if not values:
            raise DataFoundationError(f"[{factor_id}] has no factor partitions")
        seen_periods: set[tuple[int, int]] = set()
        previous_end: pd.Timestamp | None = None
        for item in values:
            if item.factor_id != factor_id:
                raise DataFoundationError(
                    f"[{factor_id}] partition set mixes factor identities"
                )
            period = (int(item.year), int(item.month))
            if period in seen_periods:
                raise DataFoundationError(
                    f"[{factor_id}] duplicate factor partition for {period}"
                )
            seen_periods.add(period)
            start = pd.Timestamp(item.date_start)
            end = pd.Timestamp(item.date_end)
            if start > end or (previous_end is not None and start <= previous_end):
                raise DataFoundationError(
                    f"[{factor_id}] factor partition date ranges overlap"
                )
            previous_end = end
        if pd.Timestamp(values[-1].date_end).date() != target_session:
            raise DataFoundationError(
                f"[{factor_id}] latest factor row does not reach {target_session}"
            )
        return values

    @staticmethod
    def _binding_payload(
        *,
        parent_version: DatasetVersion,
        universe_version: DerivedUniverseVersion,
        security_master: SecurityMasterGeneration,
        methodology_version: str,
        preprocessing_methodology_version: str,
        classification_policy: str,
    ) -> dict[str, Any]:
        return {
            "target_session": parent_version.target_session.isoformat(),
            "parent_dataset_version_id": parent_version.version_id,
            "parent_dataset_manifest_sha256": parent_version.manifest_checksum_sha256,
            "universe_version_id": universe_version.universe_version_id,
            "membership_sha256": universe_version.membership_sha256,
            "eligibility_sha256": universe_version.eligibility_sha256,
            "security_master_generation_id": security_master.generation_id,
            "security_master_sha256": security_master.manifest_sha256,
            "security_master_manifest_path": _portable_path(
                _resolve_path(security_master.manifest_path)
            ),
            "methodology_version": methodology_version,
            "preprocessing_methodology_version": preprocessing_methodology_version,
            "classification_policy": classification_policy,
        }

    def _verify_input_binding(
        self,
        *,
        universe: str,
        parent_version: DatasetVersion,
        universe_version: DerivedUniverseVersion,
        security_master: SecurityMasterGeneration,
    ) -> None:
        if parent_version.status != "PUBLISHED":
            raise DataFoundationError("parent coverage dataset is not published")
        self.market_reader.verify_version(parent_version)
        if universe_version.universe != universe:
            raise DataFoundationError("derived universe identity mismatch")
        if universe_version.parent_dataset_version_id != parent_version.version_id:
            raise DataFoundationError("derived universe uses a different parent dataset")
        if universe_version.target_session != parent_version.target_session:
            raise DataFoundationError("data and universe target sessions differ")
        self.universe_store.verify(universe_version)
        if universe_version.security_master_generation_id != security_master.generation_id:
            raise DataFoundationError("universe and Security Master generations differ")
        if (
            universe_version.security_master_manifest_sha256
            != security_master.manifest_sha256
        ):
            raise DataFoundationError("universe and Security Master hashes differ")
        if security_master.status != "PUBLISHED":
            raise DataFoundationError("Security Master generation is not published")
        manifest_path = _resolve_path(security_master.manifest_path)
        if not manifest_path.is_file() or _sha256(manifest_path) != security_master.manifest_sha256:
            raise DataFoundationError("Security Master manifest hash verification failed")

    def publish(
        self,
        *,
        generation_id: str,
        universe: str,
        parent_version: DatasetVersion,
        universe_version: DerivedUniverseVersion,
        security_master: SecurityMasterGeneration,
        factor_partitions: dict[str, Iterable[FactorPartition]],
        factor_metadata: dict[str, dict[str, Any]],
        checks: Iterable[QualityCheck],
        methodology_version: str,
        preprocessing_methodology_version: str,
        classification_policy: str,
        preprocessing_audit_path: str | Path | None = None,
        required_factor_ids: Iterable[str] | None = None,
        require_input_fingerprints: bool = True,
    ) -> dict[str, Any]:
        universe = safe_path_component(universe.upper(), label="universe")
        failed = [check for check in checks if not check.passed]
        if failed:
            detail = "; ".join(f"{item.name}: {item.message}" for item in failed)
            raise DataFoundationError(
                f"[{universe}] factor-data publication rejected: {detail}"
            )
        normalized_metadata = {
            safe_path_component(value.upper(), label="factor_id"): dict(metadata)
            for value, metadata in factor_metadata.items()
        }
        normalized_partitions = {
            safe_path_component(value.upper(), label="factor_id"): list(partitions)
            for value, partitions in factor_partitions.items()
        }
        expected_factors = sorted(normalized_metadata)
        if not expected_factors or sorted(normalized_partitions) != expected_factors:
            raise DataFoundationError(
                "factor metadata and partition identities must be non-empty and equal"
            )
        required_factors = sorted(
            safe_path_component(str(value).upper(), label="factor_id")
            for value in (
                required_factor_ids
                if required_factor_ids is not None
                else CONFIG.factors.enabled
            )
        )
        if expected_factors != required_factors:
            raise DataFoundationError(
                "factor-data publication must contain the complete configured "
                f"factor set: expected={required_factors} observed={expected_factors}"
            )
        self._verify_input_binding(
            universe=universe,
            parent_version=parent_version,
            universe_version=universe_version,
            security_master=security_master,
        )
        staging = self.staging_directory(generation_id)
        destination = self.generation_directory(generation_id)
        if not staging.is_dir():
            raise DataFoundationError(f"factor-data staging directory is missing: {staging}")
        if destination.exists():
            raise FileExistsError(f"factor-data generation already exists: {destination}")

        binding = self._binding_payload(
            parent_version=parent_version,
            universe_version=universe_version,
            security_master=security_master,
            methodology_version=methodology_version,
            preprocessing_methodology_version=preprocessing_methodology_version,
            classification_policy=classification_policy,
        )
        target_session = parent_version.target_session
        created_at = _utc_now()
        factor_bindings: dict[str, dict[str, Any]] = {}
        for factor_id in expected_factors:
            factor = safe_path_component(factor_id.upper(), label="factor_id")
            partitions = self._validate_partition_set(
                factor,
                normalized_partitions[factor_id],
                target_session=target_session,
            )
            if require_input_fingerprints and any(
                not partition.input_fingerprint_sha256
                or not partition.input_fingerprint_method
                for partition in partitions
            ):
                raise DataFoundationError(
                    f"[{factor}] every formal partition requires an input-equivalence fingerprint"
                )
            for partition in partitions:
                path = (
                    self.staged_partition_path(
                        partition, generation_id=generation_id
                    )
                    if partition.source_generation_id == generation_id
                    else _resolve_path(partition.path)
                )
                if not path.is_file() or _sha256(path) != partition.sha256:
                    raise DataFoundationError(
                        f"[{factor}] candidate partition hash verification failed: {path}"
                    )
            metadata = dict(normalized_metadata[factor_id])
            direction = int(metadata.get("direction") or 0)
            if direction not in {-1, 1}:
                raise DataFoundationError(f"[{factor}] direction must be +1 or -1")
            factor_generation_id = str(metadata.get("generation_id") or uuid4().hex)
            payload = {
                "schema_version": FACTOR_DATA_SCHEMA_VERSION,
                "publication_type": "BROAD_FACTOR",
                "generation_id": factor_generation_id,
                "factor_data_generation_id": generation_id,
                "factor_id": factor,
                "direction": direction,
                "factor_module": metadata.get("factor_module"),
                "factor_class": metadata.get("factor_class"),
                "factor_parameters": metadata.get("factor_parameters") or {},
                "date_start": partitions[0].date_start,
                "date_end": partitions[-1].date_end,
                "row_count": sum(item.row_count for item in partitions),
                "created_at": created_at.isoformat(),
                **binding,
                "partitions": [item.to_dict() for item in partitions],
            }
            path = staging / f"factor_id={factor}" / FACTOR_MANIFEST_FILE
            atomic_save_json(payload, path)
            factor_bindings[factor] = {
                "generation_id": factor_generation_id,
                "direction": direction,
                "date_start": payload["date_start"],
                "date_end": payload["date_end"],
                "row_count": payload["row_count"],
                "manifest_path": _portable_path(
                    destination / f"factor_id={factor}" / FACTOR_MANIFEST_FILE
                ),
                "manifest_sha256": _sha256(path),
            }

        audit_binding = None
        if preprocessing_audit_path is not None:
            source = Path(preprocessing_audit_path)
            if not source.is_file():
                raise DataFoundationError(
                    f"preprocessing audit is missing: {preprocessing_audit_path}"
                )
            audit_destination = staging / "preprocessing_audit.parquet"
            if source.resolve() != audit_destination.resolve():
                shutil.copy2(source, audit_destination)
            audit_binding = {
                "path": _portable_path(destination / audit_destination.name),
                "sha256": _sha256(audit_destination),
            }

        manifest = {
            "schema_version": FACTOR_DATA_SCHEMA_VERSION,
            "publication_type": "BROAD_FACTOR_DATA",
            "publication_mode": "FACTOR_DATA",
            "status": "PUBLISHED",
            "generation_id": generation_id,
            "universe": universe,
            "created_at": created_at.isoformat(),
            **binding,
            "factors": factor_bindings,
            "preprocessing_audit": audit_binding,
            "quality_checks": [check.to_dict() for check in checks],
        }
        manifest_path = staging / FACTOR_DATA_MANIFEST_FILE
        atomic_save_json(manifest, manifest_path)
        manifest_sha = _sha256(manifest_path)
        pointer = {
            "schema_version": FACTOR_DATA_SCHEMA_VERSION,
            "status": "PUBLISHED",
            "publication_id": str(uuid4()),
            "publication_mode": "FACTOR_DATA",
            "published_at": created_at.isoformat(),
            "generation_id": generation_id,
            "universe": universe,
            **binding,
            "manifest_path": _portable_path(destination / FACTOR_DATA_MANIFEST_FILE),
            "manifest_sha256": manifest_sha,
            "factors": factor_bindings,
        }
        self.output_root.mkdir(parents=True, exist_ok=True)
        with file_lock(self.lock_path):
            os.replace(staging, destination)
            # Verify the prospective publication before advancing the pointer.
            # A crash or validation failure leaves only an unreferenced immutable
            # generation; readers continue using the previous publication.
            self.verify_publication(pointer, verify_partitions=True)
            atomic_save_json(pointer, self.publication_path)
        return pointer

    def load_publication(self, *, verify_partitions: bool = True) -> dict[str, Any]:
        if not self.publication_path.is_file():
            raise DataFoundationError(
                f"factor-data publication is missing: {self.publication_path}"
            )
        try:
            pointer = json.loads(self.publication_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataFoundationError("factor-data publication is unreadable") from exc
        self.verify_publication(pointer, verify_partitions=verify_partitions)
        return pointer

    def verify_publication(
        self,
        pointer: dict[str, Any],
        *,
        verify_partitions: bool,
    ) -> dict[str, Any]:
        required = (
            pointer.get("schema_version") == FACTOR_DATA_SCHEMA_VERSION
            and pointer.get("status") == "PUBLISHED"
            and pointer.get("publication_mode") == "FACTOR_DATA"
            and pointer.get("publication_id")
            and pointer.get("generation_id")
            and pointer.get("manifest_path")
            and pointer.get("manifest_sha256")
        )
        if not required:
            raise DataFoundationError("factor-data publication contract is incomplete")
        manifest_path = _resolve_path(str(pointer["manifest_path"]))
        if not manifest_path.is_file() or _sha256(manifest_path) != pointer["manifest_sha256"]:
            raise DataFoundationError("factor-data manifest hash verification failed")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        identity_fields = (
            "schema_version",
            "status",
            "publication_mode",
            "generation_id",
            "universe",
            "target_session",
            "parent_dataset_version_id",
            "parent_dataset_manifest_sha256",
            "universe_version_id",
            "membership_sha256",
            "eligibility_sha256",
            "security_master_generation_id",
            "security_master_sha256",
            "security_master_manifest_path",
            "methodology_version",
            "preprocessing_methodology_version",
            "classification_policy",
        )
        mismatches = [
            field
            for field in identity_fields
            if str(manifest.get(field)) != str(pointer.get(field))
        ]
        if mismatches:
            raise DataFoundationError(
                f"factor-data manifest/pointer mismatch: {mismatches}"
            )
        parent = self.market_reader.require_version(
            "US_EQUITY_COVERAGE",
            str(pointer["parent_dataset_version_id"]),
            verify_partition_children=verify_partitions,
        )
        if (
            parent.manifest_checksum_sha256
            != pointer.get("parent_dataset_manifest_sha256")
            or parent.target_session.isoformat() != pointer.get("target_session")
        ):
            raise DataFoundationError("factor-data parent dataset binding is stale")
        universe_version = self.universe_store.get(
            str(pointer["universe"]), str(pointer["universe_version_id"])
        )
        if universe_version is None:
            raise DataFoundationError("factor-data universe version is missing")
        self.universe_store.verify(
            universe_version,
            verify_parent_partition_children=verify_partitions,
        )
        if (
            universe_version.parent_dataset_version_id != parent.version_id
            or universe_version.membership_sha256 != pointer.get("membership_sha256")
            or universe_version.eligibility_sha256 != pointer.get("eligibility_sha256")
            or universe_version.security_master_generation_id
            != pointer.get("security_master_generation_id")
            or universe_version.security_master_manifest_sha256
            != pointer.get("security_master_sha256")
        ):
            raise DataFoundationError("factor-data universe binding is stale")
        security_manifest = _resolve_path(
            str(pointer.get("security_master_manifest_path") or "")
        )
        if (
            not security_manifest.is_file()
            or _sha256(security_manifest) != pointer.get("security_master_sha256")
        ):
            raise DataFoundationError(
                "factor-data Security Master binding is stale"
            )
        factors = pointer.get("factors")
        manifest_factors = manifest.get("factors")
        if not isinstance(factors, dict) or not factors or set(factors) != set(manifest_factors or {}):
            raise DataFoundationError("factor-data publication has an invalid factor set")
        for factor_id, binding in factors.items():
            manifest_binding = manifest_factors[factor_id]
            if binding != manifest_binding:
                raise DataFoundationError(
                    f"[{factor_id}] factor binding differs from the main manifest"
                )
            factor_path = _resolve_path(str(binding.get("manifest_path") or ""))
            expected_hash = str(binding.get("manifest_sha256") or "")
            if not factor_path.is_file() or _sha256(factor_path) != expected_hash:
                raise DataFoundationError(
                    f"[{factor_id}] factor manifest hash verification failed"
                )
            factor_manifest = json.loads(factor_path.read_text(encoding="utf-8"))
            if (
                factor_manifest.get("factor_id") != factor_id
                or factor_manifest.get("generation_id") != binding.get("generation_id")
                or factor_manifest.get("factor_data_generation_id") != pointer.get("generation_id")
            ):
                raise DataFoundationError(f"[{factor_id}] factor manifest identity mismatch")
            if verify_partitions:
                for entry in factor_manifest.get("partitions") or []:
                    path = _resolve_path(str(entry.get("path") or ""))
                    if not path.is_file() or _sha256(path) != entry.get("sha256"):
                        raise DataFoundationError(
                            f"[{factor_id}] factor partition hash verification failed: {path}"
                        )
        return manifest

    def factor_manifest(
        self,
        publication: dict[str, Any],
        factor_id: str,
    ) -> dict[str, Any]:
        factor = safe_path_component(factor_id.upper(), label="factor_id")
        binding = (publication.get("factors") or {}).get(factor)
        if not isinstance(binding, dict):
            raise DataFoundationError(f"factor is not published: {factor}")
        path = _resolve_path(str(binding["manifest_path"]))
        if _sha256(path) != binding.get("manifest_sha256"):
            raise DataFoundationError(f"[{factor}] factor manifest hash mismatch")
        return json.loads(path.read_text(encoding="utf-8"))

    def preprocessing_audit(
        self,
        publication: dict[str, Any],
    ) -> pd.DataFrame:
        manifest_path = _resolve_path(str(publication["manifest_path"]))
        if _sha256(manifest_path) != publication.get("manifest_sha256"):
            raise DataFoundationError("factor-data manifest hash mismatch")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        binding = manifest.get("preprocessing_audit")
        if not isinstance(binding, dict):
            raise DataFoundationError("factor-data preprocessing audit is missing")
        path = _resolve_path(str(binding.get("path") or ""))
        if not path.is_file() or _sha256(path) != binding.get("sha256"):
            raise DataFoundationError(
                "factor-data preprocessing audit hash verification failed"
            )
        return pd.read_parquet(path)

    def partition_entries(
        self,
        publication: dict[str, Any],
        factor_id: str,
        *,
        start: str | date | pd.Timestamp | None = None,
        end: str | date | pd.Timestamp | None = None,
    ) -> list[FactorPartition]:
        manifest = self.factor_manifest(publication, factor_id)
        start_ts = pd.Timestamp(start).normalize() if start is not None else None
        end_ts = pd.Timestamp(end).normalize() if end is not None else None
        values: list[FactorPartition] = []
        for raw in manifest.get("partitions") or []:
            item = FactorPartition(**raw)
            if start_ts is not None and pd.Timestamp(item.date_end) < start_ts:
                continue
            if end_ts is not None and pd.Timestamp(item.date_start) > end_ts:
                continue
            values.append(item)
        return values

    def verified_partition_paths(
        self,
        partitions: Iterable[FactorPartition],
    ) -> list[Path]:
        paths: list[Path] = []
        for partition in partitions:
            path = _resolve_path(partition.path)
            if not path.is_file() or _sha256(path) != partition.sha256:
                raise DataFoundationError(
                    f"[{partition.factor_id}] factor partition hash verification failed: {path}"
                )
            paths.append(path)
        if not paths:
            raise DataFoundationError("factor query resolved no immutable partitions")
        return paths


__all__ = [
    "FACTOR_DATA_MANIFEST_FILE",
    "FACTOR_DATA_PUBLICATION_FILE",
    "FACTOR_DATA_SCHEMA_VERSION",
    "FACTOR_OBSERVATION_COLUMNS",
    "FACTOR_OBSERVATION_STATUSES",
    "FactorDataStore",
    "FactorPartition",
]
