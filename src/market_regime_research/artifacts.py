"""Transactional artifact publication for local market-regime research runs."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Mapping
from uuid import uuid4

import numpy as np
import pandas as pd

from src.market_regime_research import ALGORITHM_VERSION, SCHEMA_VERSION
from src.market_regime_research.models import (
    DataContractError,
    FeatureBundle,
    ResearchRunResult,
)

FEATURES_FILE = "features.parquet"
LABELS_FILE = "labels.parquet"
FEATURE_REGISTRY_FILE = "feature_registry.parquet"
MANIFEST_FILE = "data_manifest.json"
DIAGNOSTICS_FILE = "diagnostics.json"
RUN_FILE = "run.json"

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def generate_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"mr_{stamp}_{uuid4().hex[:8]}"


def _json_value(value: Any) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, Enum):
        return _json_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, np.generic):
        return _json_value(value.item())
    if isinstance(value, np.ndarray):
        return _json_value(value.tolist())
    if isinstance(value, (pd.Timestamp, datetime)):
        if pd.isna(value):
            return None
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    raise TypeError(f"Unsupported research JSON type: {type(value).__name__}")


def write_strict_json(path: Path, value: Mapping[str, Any]) -> None:
    normalized = _json_value(value)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                normalized,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def publish_research_run(
    *,
    output_root: Path,
    features: FeatureBundle,
    labels: pd.DataFrame,
    input_manifest: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
    run_id: str | None = None,
) -> ResearchRunResult:
    """Validate and atomically publish one immutable research run directory."""
    run_id = str(run_id or generate_run_id())
    if not _RUN_ID.fullmatch(run_id) or run_id in {".", ".."}:
        raise ValueError("run_id contains unsafe characters")
    if features.values.empty or labels.empty:
        raise DataContractError("Features and labels must both be non-empty")
    if (
        features.values.index.has_duplicates
        or labels.index.has_duplicates
        or not features.values.index.is_monotonic_increasing
        or not labels.index.is_monotonic_increasing
    ):
        raise DataContractError("Feature/label indexes must be unique and increasing")
    if not features.values.index.equals(labels.index):
        raise DataContractError("Feature and label indexes must align exactly")
    numeric_features = features.values.select_dtypes(include=[np.number])
    if np.isinf(
        numeric_features.to_numpy(dtype=float, na_value=np.nan)
    ).any():
        raise DataContractError("Feature matrix contains infinite values")
    registry_frame = pd.DataFrame(
        [definition.as_dict() for definition in features.registry]
    )
    if registry_frame.empty:
        raise DataContractError("Feature registry is empty")
    if registry_frame["feature_name"].tolist() != features.values.columns.tolist():
        raise DataContractError(
            "Feature registry order does not match feature columns"
        )
    if registry_frame["feature_name"].duplicated().any():
        raise DataContractError("Feature registry contains duplicates")

    output_root = Path(output_root)
    runs_root = output_root / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    final_dir = runs_root / run_id
    if final_dir.exists():
        raise FileExistsError(f"Research run already exists: {run_id}")
    temporary_dir = Path(
        tempfile.mkdtemp(prefix=f".{run_id}.", dir=str(runs_root))
    )
    try:
        features_path = temporary_dir / FEATURES_FILE
        labels_path = temporary_dir / LABELS_FILE
        registry_path = temporary_dir / FEATURE_REGISTRY_FILE
        features.values.to_parquet(features_path, compression="snappy")
        labels.to_parquet(labels_path, compression="snappy")
        registry_frame.to_parquet(registry_path, compression="snappy", index=False)

        artifact_hashes = {
            FEATURES_FILE: file_sha256(features_path),
            LABELS_FILE: file_sha256(labels_path),
            FEATURE_REGISTRY_FILE: file_sha256(registry_path),
        }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "run_id": run_id,
            "created_at": _utc_now(),
            "inputs": input_manifest,
            "artifacts": artifact_hashes,
            "feature_rows": len(features.values),
            "feature_columns": len(features.values.columns),
            "label_rows": len(labels),
        }
        write_strict_json(temporary_dir / MANIFEST_FILE, manifest)
        write_strict_json(
            temporary_dir / DIAGNOSTICS_FILE,
            {
                "run_id": run_id,
                "feature_domains": features.diagnostics,
                **dict(diagnostics),
            },
        )
        write_strict_json(
            temporary_dir / RUN_FILE,
            {
                "run_id": run_id,
                "status": "SUCCESS",
                "created_at": manifest["created_at"],
                "schema_version": SCHEMA_VERSION,
                "algorithm_version": ALGORITHM_VERSION,
            },
        )
        os.replace(temporary_dir, final_dir)

        pointer_temp = output_root / f".latest.{uuid4().hex}.tmp"
        write_strict_json(
            pointer_temp,
            {
                "run_id": run_id,
                "run_path": f"runs/{run_id}",
                "published_at": _utc_now(),
            },
        )
        os.replace(pointer_temp, output_root / "latest.json")
    except Exception:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise

    return ResearchRunResult(
        run_id=run_id,
        run_dir=final_dir,
        features_path=final_dir / FEATURES_FILE,
        labels_path=final_dir / LABELS_FILE,
        feature_registry_path=final_dir / FEATURE_REGISTRY_FILE,
        manifest_path=final_dir / MANIFEST_FILE,
        diagnostics_path=final_dir / DIAGNOSTICS_FILE,
    )


__all__ = [
    "DIAGNOSTICS_FILE",
    "FEATURES_FILE",
    "FEATURE_REGISTRY_FILE",
    "LABELS_FILE",
    "MANIFEST_FILE",
    "RUN_FILE",
    "file_sha256",
    "generate_run_id",
    "publish_research_run",
    "write_strict_json",
]
