"""Immutable generation and atomic pointer for cross-universe research."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

import pandas as pd

from src.config import CONFIG, PROJECT_ROOT
from src.market_regime_research.artifacts import file_sha256, write_strict_json
from src.research_universes.cross_universe import CrossUniverseFactorAssessment
from src.utils.io import load_json


CROSS_UNIVERSE_SCHEMA_VERSION = 1


class CrossUniversePublicationError(RuntimeError):
    pass


def cross_universe_root() -> Path:
    configured = Path(CONFIG.webapp.output_dir)
    output_root = configured if configured.is_absolute() else PROJECT_ROOT / configured
    return output_root / "research" / "cross_universe"


def cross_universe_publication_path() -> Path:
    return cross_universe_root() / "publication.json"


def _inside_root(path: Path) -> bool:
    try:
        path.resolve().relative_to(cross_universe_root().resolve())
        return True
    except ValueError:
        return False


def publish_cross_universe_generation(
    *,
    target_session: str,
    assessments: Iterable[CrossUniverseFactorAssessment],
    source_bindings: Mapping[str, Any],
) -> dict[str, Any]:
    items = list(assessments)
    if not items:
        raise CrossUniversePublicationError("Cross-universe publication is empty")
    if any(item.target_session != target_session for item in items):
        raise CrossUniversePublicationError("Assessment target sessions are mixed")

    generation_id = (
        f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{uuid4().hex[:10]}"
    )
    generation_dir = cross_universe_root() / f"generation={generation_id}"
    generation_dir.mkdir(parents=True, exist_ok=False)
    assessments_path = generation_dir / "factor_assessments.parquet"
    manifest_path = generation_dir / "manifest.json"

    records = []
    for item in items:
        payload = item.to_dict()
        records.append(
            {
                "factor_id": item.factor_id,
                "target_session": item.target_session,
                "verdict": item.verdict.value,
                "direction_consistent": item.direction_consistent,
                "summary": item.summary,
                "universes_json": json.dumps(
                    payload["universes"],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            }
        )
    pd.DataFrame(records).sort_values("factor_id").to_parquet(
        assessments_path,
        compression="snappy",
        index=False,
    )
    verdict_counts = dict(Counter(item.verdict.value for item in items))
    manifest = {
        "schema_version": CROSS_UNIVERSE_SCHEMA_VERSION,
        "generation_id": generation_id,
        "status": "COMPLETE",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target_session": target_session,
        "factor_count": len(items),
        "verdict_counts": verdict_counts,
        "source_bindings": dict(source_bindings),
        "factor_assessments_path": str(assessments_path),
        "factor_assessments_sha256": file_sha256(assessments_path),
    }
    write_strict_json(manifest_path, manifest)
    pointer = {
        "schema_version": CROSS_UNIVERSE_SCHEMA_VERSION,
        "status": "PUBLISHED",
        "generation_id": generation_id,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "target_session": target_session,
        "factor_count": len(items),
        "verdict_counts": verdict_counts,
        "has_insufficient": "INSUFFICIENT" in verdict_counts,
        "generation_dir": str(generation_dir),
        "factor_assessments_path": str(assessments_path),
        "factor_assessments_sha256": file_sha256(assessments_path),
        "manifest_path": str(manifest_path),
        "manifest_sha256": file_sha256(manifest_path),
    }
    write_strict_json(cross_universe_publication_path(), pointer)
    return pointer


def load_cross_universe_publication() -> tuple[dict[str, Any], pd.DataFrame, dict[str, Any]]:
    pointer_path = cross_universe_publication_path()
    if not pointer_path.exists():
        raise CrossUniversePublicationError("Cross-universe publication is missing")
    pointer = load_json(pointer_path)
    if (
        not isinstance(pointer, dict)
        or pointer.get("schema_version") != CROSS_UNIVERSE_SCHEMA_VERSION
        or pointer.get("status") != "PUBLISHED"
    ):
        raise CrossUniversePublicationError("Cross-universe pointer is invalid")
    assessments_path = Path(str(pointer.get("factor_assessments_path") or ""))
    manifest_path = Path(str(pointer.get("manifest_path") or ""))
    if not _inside_root(assessments_path) or not _inside_root(manifest_path):
        raise CrossUniversePublicationError("Cross-universe pointer escapes output root")
    if (
        not assessments_path.exists()
        or file_sha256(assessments_path) != pointer.get("factor_assessments_sha256")
        or not manifest_path.exists()
        or file_sha256(manifest_path) != pointer.get("manifest_sha256")
    ):
        raise CrossUniversePublicationError("Cross-universe generation checksum failed")
    manifest = load_json(manifest_path)
    if (
        not isinstance(manifest, dict)
        or manifest.get("generation_id") != pointer.get("generation_id")
        or manifest.get("target_session") != pointer.get("target_session")
        or manifest.get("factor_assessments_sha256")
        != pointer.get("factor_assessments_sha256")
    ):
        raise CrossUniversePublicationError("Cross-universe manifest is inconsistent")
    frame = pd.read_parquet(assessments_path)
    if len(frame) != int(pointer.get("factor_count") or 0):
        raise CrossUniversePublicationError("Cross-universe factor count changed")
    return pointer, frame, manifest


__all__ = [
    "CROSS_UNIVERSE_SCHEMA_VERSION",
    "CrossUniversePublicationError",
    "cross_universe_publication_path",
    "cross_universe_root",
    "load_cross_universe_publication",
    "publish_cross_universe_generation",
]
