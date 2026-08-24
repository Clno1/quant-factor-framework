"""Run-level publication contract for daily factor research artifacts."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from src.config import CONFIG, PROJECT_ROOT
from src.data.foundation import DatasetVersion, MarketDataReader
from src.data.price_semantics import validate_price_semantics_contract
from src.factors.artifacts import factor_bundle_manifest_path
from src.utils.identifiers import safe_path_component
from src.utils.io import atomic_save_json, load_json


RESEARCH_PUBLICATION_SCHEMA_VERSION = 3
RESEARCH_METHODOLOGY_VERSION = "factor_research_v3_price_hac_censor_aware"
CONFIDENCE_METHODOLOGY_VERSION = "factor_confidence_v2_hac_censor_aware"
RESEARCH_PUBLICATION_FILE = "research_publication.json"


class ResearchPublicationError(RuntimeError):
    """Raised when factor artifacts are stale or do not form one complete run."""


def _output_root() -> Path:
    configured = Path(CONFIG.webapp.output_dir)
    return configured if configured.is_absolute() else PROJECT_ROOT / configured


def research_publication_path(universe: str) -> Path:
    name = safe_path_component(universe.upper(), label="universe")
    return _output_root() / "universes" / name / RESEARCH_PUBLICATION_FILE


def factor_confidence_path(universe: str, factor_id: str) -> Path:
    universe = safe_path_component(universe.upper(), label="universe")
    factor_id = safe_path_component(factor_id, label="factor_id")
    return (
        _output_root()
        / "universes"
        / universe
        / "factors"
        / factor_id
        / "confidence.json"
    )


def _confidence_required(universe: str) -> bool:
    try:
        from src.research_universes import research_universe_registry

        return bool(
            research_universe_registry().get(universe).confidence_enabled
        )
    except (KeyError, ValueError):
        # Test/private universes outside the formal registry remain supported.
        return False


def _confidence_binding(universe: str, factor_id: str) -> dict[str, Any]:
    path = factor_confidence_path(universe, factor_id)
    if not path.exists():
        raise ResearchPublicationError(
            f"Confidence report is missing for {universe}/{factor_id}: {path}"
        )
    report = load_json(path)
    if (
        not isinstance(report, dict)
        or report.get("factor") != factor_id
        or report.get("verdict") not in {"PASS", "WATCH", "FAIL"}
        or report.get("methodology_version") != CONFIDENCE_METHODOLOGY_VERSION
    ):
        raise ResearchPublicationError(
            f"Confidence report is invalid for {universe}/{factor_id}"
        )
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "methodology_version": report["methodology_version"],
        "generated_at": report.get("generated_at"),
        "verdict": report["verdict"],
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def dataset_version_provenance(version: DatasetVersion) -> dict[str, Any]:
    """Return the immutable market-data identity embedded in factor artifacts."""
    manifest = MarketDataReader().verify_version(
        version,
        require_price_semantics=True,
    )
    return {
        "backend": "duckdb",
        "version_id": version.version_id,
        "run_id": version.run_id,
        "universe": version.universe,
        "target_session": version.target_session.isoformat(),
        "bars_sha256": version.checksum_sha256,
        "universe_sha256": version.universe_checksum_sha256,
        "membership_sha256": version.membership_checksum_sha256,
        "manifest_sha256": version.manifest_checksum_sha256,
        "row_count": int(version.row_count),
        "ticker_count": int(version.ticker_count),
        "price_semantics": validate_price_semantics_contract(
            manifest.get("price_semantics")
        ),
    }


def _load_factor_manifest(
    factor_id: str,
    universe: str,
    *,
    expected_data: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    path = factor_bundle_manifest_path(factor_id, universe)
    if not path.exists():
        raise ResearchPublicationError(
            f"Factor manifest is missing for {universe}/{factor_id}: {path}"
        )
    manifest = load_json(path)
    if not isinstance(manifest, dict):
        raise ResearchPublicationError(
            f"Factor manifest is invalid for {universe}/{factor_id}"
        )
    provenance = manifest.get("provenance")
    data = provenance.get("data_foundation") if isinstance(provenance, dict) else None
    if not isinstance(data, dict):
        raise ResearchPublicationError(
            f"Factor manifest has no data version for {universe}/{factor_id}"
        )
    for field in (
        "backend",
        "version_id",
        "universe",
        "target_session",
        "bars_sha256",
        "universe_sha256",
        "membership_sha256",
        "manifest_sha256",
        "price_semantics",
    ):
        if data.get(field) != expected_data.get(field):
            raise ResearchPublicationError(
                f"Factor {universe}/{factor_id} was built from a different "
                f"market-data version: field={field} "
                f"expected={expected_data.get(field)!r} "
                f"observed={data.get(field)!r}"
            )
    if not manifest.get("generation_id"):
        raise ResearchPublicationError(
            f"Factor manifest has no generation_id for {universe}/{factor_id}"
        )
    return path, manifest


def publish_factor_research(
    *,
    universe: str,
    version: DatasetVersion,
    factor_ids: Iterable[str],
) -> Path:
    """Atomically publish a completion pointer after every factor has finished."""
    universe = safe_path_component(universe.upper(), label="universe")
    factors = list(dict.fromkeys(str(value).strip() for value in factor_ids))
    if not factors:
        raise ResearchPublicationError("Factor research cannot publish an empty run")
    expected_data = dataset_version_provenance(version)
    confidence_required = _confidence_required(universe)
    factor_payload: dict[str, dict[str, Any]] = {}
    for factor_id in factors:
        path, manifest = _load_factor_manifest(
            factor_id,
            universe,
            expected_data=expected_data,
        )
        factor_payload[factor_id] = {
            "generation_id": manifest["generation_id"],
            "date_start": manifest.get("date_start"),
            "date_end": manifest.get("date_end"),
            "manifest_path": str(path),
            "manifest_sha256": _sha256(path),
            "confidence": (
                _confidence_binding(universe, factor_id)
                if confidence_required
                else None
            ),
        }

    payload = {
        "schema_version": RESEARCH_PUBLICATION_SCHEMA_VERSION,
        "status": "PUBLISHED",
        "publication_id": str(uuid4()),
        "published_at": datetime.now(timezone.utc).isoformat(),
        "universe": universe,
        "confidence_required": confidence_required,
        "methodology_version": RESEARCH_METHODOLOGY_VERSION,
        "data_foundation": expected_data,
        "factors": factor_payload,
    }
    path = research_publication_path(universe)
    atomic_save_json(payload, path)
    return path


def validate_factor_research_publication(
    universe: str,
    *,
    version: DatasetVersion | None = None,
    factor_ids: Iterable[str] | None = None,
    publication_id: str | None = None,
) -> dict[str, Any]:
    """Require one complete factor run built from the latest published bars."""
    universe = safe_path_component(universe.upper(), label="universe")
    expected_version = version or MarketDataReader().require_latest(universe)
    expected_data = dataset_version_provenance(expected_version)
    path = research_publication_path(universe)
    if not path.exists():
        raise ResearchPublicationError(
            f"Research publication is missing for {universe}: {path}"
        )
    publication = load_json(path)
    if (
        not isinstance(publication, dict)
        or publication.get("schema_version")
        != RESEARCH_PUBLICATION_SCHEMA_VERSION
        or publication.get("status") != "PUBLISHED"
        or publication.get("universe") != universe
        or publication.get("methodology_version") != RESEARCH_METHODOLOGY_VERSION
    ):
        raise ResearchPublicationError(
            f"Research publication is invalid for {universe}"
        )
    if (
        publication_id is not None
        and publication.get("publication_id") != publication_id
    ):
        raise ResearchPublicationError(
            f"Research publication changed for {universe}: "
            f"expected={publication_id!r} "
            f"observed={publication.get('publication_id')!r}"
        )
    observed_data = publication.get("data_foundation")
    if not isinstance(observed_data, dict):
        raise ResearchPublicationError(
            f"Research publication has no data version for {universe}"
        )
    for field in (
        "backend",
        "version_id",
        "universe",
        "target_session",
        "bars_sha256",
        "universe_sha256",
        "membership_sha256",
        "manifest_sha256",
        "price_semantics",
    ):
        if observed_data.get(field) != expected_data.get(field):
            raise ResearchPublicationError(
                f"Research publication for {universe} is stale: field={field} "
                f"expected={expected_data.get(field)!r} "
                f"observed={observed_data.get(field)!r}"
            )

    published_factors = publication.get("factors")
    if not isinstance(published_factors, dict):
        raise ResearchPublicationError(
            f"Research publication has no factor set for {universe}"
        )
    expected_factors = list(
        dict.fromkeys(
            str(value).strip()
            for value in (
                factor_ids
                if factor_ids is not None
                else list(CONFIG.factors.enabled)
            )
        )
    )
    missing = sorted(set(expected_factors) - set(published_factors))
    if missing:
        raise ResearchPublicationError(
            f"Research publication for {universe} is missing factors: {missing}"
        )
    for factor_id in expected_factors:
        manifest_path, manifest = _load_factor_manifest(
            factor_id,
            universe,
            expected_data=expected_data,
        )
        published = published_factors[factor_id]
        if (
            not isinstance(published, dict)
            or published.get("generation_id") != manifest.get("generation_id")
            or published.get("manifest_sha256") != _sha256(manifest_path)
        ):
            raise ResearchPublicationError(
                f"Factor generation changed after publication for "
                f"{universe}/{factor_id}"
            )
        confidence = published.get("confidence")
        if publication.get("confidence_required"):
            if not isinstance(confidence, dict):
                raise ResearchPublicationError(
                    f"Research publication has no confidence binding for "
                    f"{universe}/{factor_id}"
                )
            confidence_path = factor_confidence_path(universe, factor_id)
            if (
                not confidence_path.exists()
                or confidence.get("sha256") != _sha256(confidence_path)
            ):
                raise ResearchPublicationError(
                    f"Confidence report changed after publication for "
                    f"{universe}/{factor_id}"
                )
    return publication


__all__ = [
    "CONFIDENCE_METHODOLOGY_VERSION",
    "RESEARCH_PUBLICATION_FILE",
    "RESEARCH_METHODOLOGY_VERSION",
    "RESEARCH_PUBLICATION_SCHEMA_VERSION",
    "ResearchPublicationError",
    "dataset_version_provenance",
    "factor_confidence_path",
    "publish_factor_research",
    "research_publication_path",
    "validate_factor_research_publication",
]
