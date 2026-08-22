"""Orchestration and provenance checks for Stage B effectiveness screening."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from src.market_regime_research.artifacts import (
    FEATURES_FILE,
    FEATURE_REGISTRY_FILE,
    LABELS_FILE,
    MANIFEST_FILE as RESEARCH_MANIFEST_FILE,
    file_sha256,
)
from src.market_regime_research.models import (
    DataContractError,
    ScreeningRunResult,
)
from src.market_regime_research.screening import (
    load_candidate_registry,
    run_univariate_screening,
)
from src.market_regime_research.screening_artifacts import (
    publish_screening_run,
)
from src.market_regime_research.settings import MarketRegimeResearchSettings


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataContractError(f"Cannot read valid JSON from {path}") from exc
    if not isinstance(value, dict):
        raise DataContractError(f"JSON artifact must contain an object: {path}")
    return value


def resolve_research_run(
    output_root: Path,
    run_id: str | None,
) -> tuple[str, Path]:
    """Resolve an explicit immutable run or the current research pointer."""
    output_root = Path(output_root)
    if run_id is None:
        pointer = _read_json(output_root / "latest.json")
        run_id = str(pointer.get("run_id", "")).strip()
    if not run_id or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
        for character in run_id
    ):
        raise DataContractError("Research run_id is missing or unsafe")
    run_dir = output_root / "runs" / run_id
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Research run not found: {run_id}")
    return run_id, run_dir


def load_validated_research_run(
    run_dir: Path,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Hash-check Stage A artifacts before any statistic is calculated."""
    run_dir = Path(run_dir)
    manifest_path = run_dir / RESEARCH_MANIFEST_FILE
    manifest = _read_json(manifest_path)
    artifact_hashes = manifest.get("artifacts")
    if not isinstance(artifact_hashes, Mapping):
        raise DataContractError("Research manifest has no artifact hash mapping")
    required = (FEATURES_FILE, LABELS_FILE, FEATURE_REGISTRY_FILE)
    for filename in required:
        path = run_dir / filename
        expected = str(artifact_hashes.get(filename, ""))
        if not path.is_file() or not expected:
            raise DataContractError(
                f"Research manifest is missing required artifact {filename}"
            )
        observed = file_sha256(path)
        if observed != expected:
            raise DataContractError(
                f"Research artifact hash differs for {filename}: "
                f"expected {expected}, observed {observed}"
            )

    features = pd.read_parquet(run_dir / FEATURES_FILE)
    labels = pd.read_parquet(run_dir / LABELS_FILE)
    registry = pd.read_parquet(run_dir / FEATURE_REGISTRY_FILE)
    if features.empty or labels.empty or registry.empty:
        raise DataContractError("Research artifacts cannot be empty")
    if not isinstance(features.index, pd.DatetimeIndex):
        raise DataContractError("Feature matrix requires a DatetimeIndex")
    if not features.index.equals(labels.index):
        raise DataContractError("Feature and label indexes do not align")
    if (
        features.index.has_duplicates
        or not features.index.is_monotonic_increasing
    ):
        raise DataContractError("Research index must be unique and increasing")
    numeric = features.select_dtypes(include=[np.number]).to_numpy(
        dtype=float,
        na_value=np.nan,
    )
    if np.isinf(numeric).any():
        raise DataContractError("Feature matrix contains infinite values")
    if registry["feature_name"].tolist() != features.columns.tolist():
        raise DataContractError(
            "Feature registry order differs from the feature matrix"
        )
    return features, labels, registry, manifest


def run_effectiveness_screen(
    settings: MarketRegimeResearchSettings,
    *,
    research_run_id: str | None = None,
    screening_id: str | None = None,
    candidate_registry_path: Path | None = None,
) -> ScreeningRunResult:
    """Run Stage B against a validated Stage A artifact set."""
    resolved_run_id, run_dir = resolve_research_run(
        settings.output_root,
        research_run_id,
    )
    features, labels, feature_registry, research_manifest = (
        load_validated_research_run(run_dir)
    )
    resolved_candidate_registry = Path(
        candidate_registry_path or settings.screening.candidate_registry_path
    ).resolve()
    candidates, registry_metadata = load_candidate_registry(
        resolved_candidate_registry,
        feature_registry,
        horizons=settings.labels.horizons,
        scan_unregistered=settings.screening.scan_unregistered,
    )
    outputs = run_univariate_screening(
        features=features,
        labels=labels,
        feature_registry=feature_registry,
        candidates=candidates,
        settings=settings.screening,
        registry_metadata=registry_metadata,
    )
    source_manifest = {
        "run_id": resolved_run_id,
        "run_path": str(run_dir),
        "research_manifest_sha256": file_sha256(
            run_dir / RESEARCH_MANIFEST_FILE
        ),
        "research_schema_version": research_manifest.get("schema_version"),
        "research_algorithm_version": research_manifest.get(
            "algorithm_version"
        ),
        "research_artifacts": dict(research_manifest.get("artifacts", {})),
        "candidate_registry_path": str(resolved_candidate_registry),
        "candidate_registry_sha256": file_sha256(
            resolved_candidate_registry
        ),
    }
    return publish_screening_run(
        output_root=settings.output_root,
        outputs=outputs,
        settings=settings.screening,
        source_manifest=source_manifest,
        screening_id=screening_id,
    )


__all__ = [
    "load_validated_research_run",
    "resolve_research_run",
    "run_effectiveness_screen",
]
