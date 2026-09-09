"""Orchestrate frozen single-universe publications into cross-pool evidence."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from src.config import CONFIG
from src.data.foundation import MarketDataReader
from src.factors.publication import (
    factor_confidence_path,
    research_publication_path,
    validate_factor_research_publication,
)
from src.research_universes.cross_universe import (
    EvidenceStatus,
    UniverseFactorEvidence,
    assess_factor_across_universes,
)
from src.research_universes.publication import publish_cross_universe_generation
from src.research_universes.registry import ResearchUniverseRegistry, research_universe_registry
from src.utils.io import load_json
from src.utils.market_calendar import latest_publishable_xnys_session


def _source_snapshot(
    universe,
    *,
    target_session: str,
    reader: MarketDataReader,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    pointer_path = research_publication_path(universe.universe_id)
    base = {
        "universe": universe.universe_id,
        "role": universe.role.value,
        "target_session_expected": target_session,
    }
    if not pointer_path.exists():
        return {**base, "status": "MISSING", "reason": "RESEARCH_PUBLICATION_MISSING"}, None
    try:
        candidate = load_json(pointer_path)
        publication_id = str(candidate.get("publication_id") or "")
        data = candidate.get("data_foundation")
        version_id = str(data.get("version_id") or "") if isinstance(data, dict) else ""
        version = reader.require_version(universe.universe_id, version_id)
        publication = validate_factor_research_publication(
            universe.universe_id,
            version=version,
            publication_id=publication_id,
        )
        observed_target = str(publication["data_foundation"]["target_session"])
        status = "AVAILABLE" if observed_target == target_session else "STALE"
        factors = publication.get("factors", {})
        binding = {
            **base,
            "status": status,
            "target_session": observed_target,
            "dataset_version_id": version.version_id,
            "research_publication_id": publication_id,
            "research_publication_path": str(pointer_path),
            "data_foundation": publication["data_foundation"],
            "factor_generations": {
                factor_id: {
                    "generation_id": value.get("generation_id"),
                    "manifest_sha256": value.get("manifest_sha256"),
                    "confidence_sha256": (
                        value.get("confidence", {}).get("sha256")
                        if isinstance(value.get("confidence"), dict)
                        else None
                    ),
                }
                for factor_id, value in factors.items()
                if isinstance(value, dict)
            },
        }
        if status == "STALE":
            binding["reason"] = "TARGET_SESSION_MISMATCH"
        return binding, publication
    except Exception as exc:  # noqa: BLE001
        return {**base, "status": "INVALID", "reason": str(exc)}, None


def publish_cross_universe_assessments(
    *,
    target_session: str | pd.Timestamp | None = None,
    registry: ResearchUniverseRegistry | None = None,
    reader: MarketDataReader | None = None,
) -> dict[str, Any]:
    registry = registry or research_universe_registry()
    entries = list(registry.cross_universe_entries())
    delay = int(getattr(CONFIG.data.foundation, "close_delay_minutes", 120))
    target = (
        pd.Timestamp(target_session)
        if target_session is not None
        else latest_publishable_xnys_session(delay_minutes=delay)
    )
    if target.tzinfo is not None:
        target = target.tz_localize(None)
    target_iso = target.normalize().date().isoformat()
    reader = reader or MarketDataReader()

    source_bindings: dict[str, Any] = {}
    publications: dict[str, dict[str, Any] | None] = {}
    for universe in entries:
        binding, publication = _source_snapshot(
            universe,
            target_session=target_iso,
            reader=reader,
        )
        source_bindings[universe.universe_id] = binding
        publications[universe.universe_id] = publication

    factor_ids = list(dict.fromkeys(str(value) for value in CONFIG.factors.enabled))
    assessments = []
    for factor_id in factor_ids:
        evidence: dict[str, UniverseFactorEvidence] = {}
        for universe in entries:
            binding = source_bindings[universe.universe_id]
            publication = publications[universe.universe_id]
            status = str(binding["status"])
            if status != "AVAILABLE" or publication is None:
                evidence[universe.universe_id] = UniverseFactorEvidence(
                    universe_id=universe.universe_id,
                    role=universe.role,
                    status={
                        "MISSING": EvidenceStatus.MISSING,
                        "STALE": EvidenceStatus.STALE,
                    }.get(status, EvidenceStatus.INVALID),
                    target_session=binding.get("target_session"),
                    reason=str(binding.get("reason") or status),
                )
                continue
            factor_binding = publication.get("factors", {}).get(factor_id)
            confidence_binding = (
                factor_binding.get("confidence")
                if isinstance(factor_binding, dict)
                else None
            )
            if not isinstance(factor_binding, dict) or not isinstance(
                confidence_binding, dict
            ):
                evidence[universe.universe_id] = UniverseFactorEvidence(
                    universe_id=universe.universe_id,
                    role=universe.role,
                    status=EvidenceStatus.MISSING,
                    target_session=target_iso,
                    reason="FACTOR_CONFIDENCE_BINDING_MISSING",
                )
                continue
            report_path = factor_confidence_path(universe.universe_id, factor_id)
            report = load_json(Path(report_path))
            evidence[universe.universe_id] = UniverseFactorEvidence.from_confidence_report(
                universe=universe,
                target_session=target_iso,
                dataset_version_id=str(binding["dataset_version_id"]),
                research_publication_id=str(binding["research_publication_id"]),
                factor_generation_id=str(factor_binding["generation_id"]),
                confidence_sha256=str(confidence_binding["sha256"]),
                report=report,
            )
        assessments.append(
            assess_factor_across_universes(
                factor_id=factor_id,
                target_session=target_iso,
                research_universes=entries,
                evidence=evidence,
            )
        )
    return publish_cross_universe_generation(
        target_session=target_iso,
        assessments=assessments,
        source_bindings=source_bindings,
    )


__all__ = ["publish_cross_universe_assessments"]
