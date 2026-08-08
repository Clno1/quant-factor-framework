"""Neutral access to persisted factor matrices used outside the Web layer."""
from __future__ import annotations

import hashlib
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from src.config import CONFIG, PROJECT_ROOT
from src.utils.identifiers import safe_path_component
from src.utils.io import atomic_save_json, ensure_dir, load_json, read_parquet


DEFAULT_UNIVERSE = "SP500"
FACTOR_BUNDLE_SCHEMA_VERSION = 1
_BUNDLE_LOCKS: dict[str, threading.RLock] = {}
_BUNDLE_LOCKS_GUARD = threading.Lock()


def _factor_dir(name: str, universe: str) -> Path:
    return factor_values_path(name, universe).parent


def factor_bundle_manifest_path(
    name: str,
    universe: str = DEFAULT_UNIVERSE,
) -> Path:
    return _factor_dir(name, universe) / "factor_matrix_manifest.json"


@contextmanager
def _bundle_lock(directory: Path, *, exclusive: bool):
    ensure_dir(directory)
    key = str(directory.resolve())
    with _BUNDLE_LOCKS_GUARD:
        thread_lock = _BUNDLE_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        stream = (directory / ".factor_matrix.lock").open("a+b")
        try:
            try:
                import fcntl

                operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(stream.fileno(), operation)
            except ImportError:
                pass
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except ImportError:
                pass
            stream.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _temp_parquet(values: pd.DataFrame, destination: Path) -> Path:
    temp = destination.with_name(
        f".tmp_{uuid4().hex}_{destination.name}"
    )
    values.to_parquet(temp, compression="snappy")
    return temp


def factor_values_path(name: str, universe: str = DEFAULT_UNIVERSE) -> Path:
    name = safe_path_component(name, label="factor_id")
    universe = safe_path_component(universe, label="universe")
    configured = Path(CONFIG.webapp.output_dir)
    output_dir = configured if configured.is_absolute() else PROJECT_ROOT / configured
    return output_dir / "universes" / universe / "factors" / name / "factor_values.parquet"


def factor_raw_values_path(name: str, universe: str = DEFAULT_UNIVERSE) -> Path:
    name = safe_path_component(name, label="factor_id")
    universe = safe_path_component(universe, label="universe")
    configured = Path(CONFIG.webapp.output_dir)
    output_dir = configured if configured.is_absolute() else PROJECT_ROOT / configured
    return (
        output_dir
        / "universes"
        / universe
        / "factors"
        / name
        / "factor_raw_values.parquet"
    )


def load_factor_values(
    name: str,
    universe: str = DEFAULT_UNIVERSE,
) -> pd.DataFrame | None:
    """Load a persisted factor matrix without depending on FastAPI modules."""
    path = factor_values_path(name, universe)
    return read_parquet(path) if path.exists() else None


def load_factor_raw_values(
    name: str,
    universe: str = DEFAULT_UNIVERSE,
) -> pd.DataFrame | None:
    """Load the formula-level factor matrix when the research pipeline saved it."""
    path = factor_raw_values_path(name, universe)
    return read_parquet(path) if path.exists() else None


def save_factor_matrix_bundle(
    name: str,
    *,
    raw: pd.DataFrame,
    clean: pd.DataFrame,
    universe: str = DEFAULT_UNIVERSE,
    provenance: dict[str, Any] | None = None,
) -> Path:
    """Atomically publish formula-level and cleaned matrices as one generation."""
    if not raw.index.equals(clean.index) or not raw.columns.equals(clean.columns):
        raise ValueError(
            f"Raw/clean factor matrices are misaligned for {universe}/{name}"
        )
    if raw.index.has_duplicates or raw.columns.has_duplicates:
        raise ValueError(
            f"Factor matrix contains duplicate dates/tickers for "
            f"{universe}/{name}"
        )
    directory = _factor_dir(name, universe)
    raw_path = factor_raw_values_path(name, universe)
    clean_path = factor_values_path(name, universe)
    manifest_path = factor_bundle_manifest_path(name, universe)
    with _bundle_lock(directory, exclusive=True):
        manifest_path.unlink(missing_ok=True)
        raw_temp: Path | None = None
        clean_temp: Path | None = None
        try:
            raw_temp = _temp_parquet(raw, raw_path)
            clean_temp = _temp_parquet(clean, clean_path)
            os.replace(raw_temp, raw_path)
            os.replace(clean_temp, clean_path)
            manifest = {
                "schema_version": FACTOR_BUNDLE_SCHEMA_VERSION,
                "generation_id": str(uuid4()),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "factor_id": name,
                "universe": universe,
                "raw_shape": list(raw.shape),
                "clean_shape": list(clean.shape),
                "date_start": (
                    pd.Timestamp(clean.index.min()).strftime("%Y-%m-%d")
                    if not clean.empty
                    else None
                ),
                "date_end": (
                    pd.Timestamp(clean.index.max()).strftime("%Y-%m-%d")
                    if not clean.empty
                    else None
                ),
                "provenance": dict(provenance or {}),
                "artifact_sha256": {
                    raw_path.name: _sha256(raw_path),
                    clean_path.name: _sha256(clean_path),
                },
            }
            atomic_save_json(manifest, manifest_path)
        finally:
            if raw_temp is not None:
                raw_temp.unlink(missing_ok=True)
            if clean_temp is not None:
                clean_temp.unlink(missing_ok=True)
    return manifest_path


def load_factor_matrix_bundle(
    name: str,
    universe: str = DEFAULT_UNIVERSE,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Load a verified raw/clean pair from one published generation."""
    directory = _factor_dir(name, universe)
    raw_path = factor_raw_values_path(name, universe)
    clean_path = factor_values_path(name, universe)
    manifest_path = factor_bundle_manifest_path(name, universe)
    with _bundle_lock(directory, exclusive=False):
        if not manifest_path.exists():
            raise FileNotFoundError(
                f"Factor matrix manifest is missing for {universe}/{name}; "
                "re-run the research pipeline to rebuild raw and clean values "
                "as one verified generation."
            )
        manifest = load_json(manifest_path)
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version")
            != FACTOR_BUNDLE_SCHEMA_VERSION
            or manifest.get("factor_id") != name
            or manifest.get("universe") != universe
        ):
            raise ValueError(
                f"Invalid factor matrix manifest for {universe}/{name}"
            )
        hashes = manifest.get("artifact_sha256")
        if not isinstance(hashes, dict):
            raise ValueError(
                f"Factor matrix manifest has no hashes for {universe}/{name}"
            )
        for path in (raw_path, clean_path):
            expected = hashes.get(path.name)
            if not path.exists() or not isinstance(expected, str):
                raise ValueError(
                    f"Factor matrix artifact is missing: {path}"
                )
            if _sha256(path) != expected:
                raise ValueError(
                    f"Factor matrix artifact hash mismatch: {path}"
                )
        return read_parquet(raw_path), read_parquet(clean_path), manifest


__all__ = [
    "DEFAULT_UNIVERSE",
    "factor_values_path",
    "factor_raw_values_path",
    "factor_bundle_manifest_path",
    "load_factor_values",
    "load_factor_raw_values",
    "save_factor_matrix_bundle",
    "load_factor_matrix_bundle",
]
