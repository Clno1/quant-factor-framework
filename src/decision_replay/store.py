"""Atomic Parquet persistence for strategy decision-replay snapshots."""
from __future__ import annotations

import hashlib
import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from src.decision_replay.models import DecisionReplaySnapshot
from src.utils.io import atomic_save_json, ensure_dir, load_json, read_parquet


REPLAY_DIRNAME = "decision_replay"
SUPPORTED_SCHEMA_VERSIONS = {1}
_ROOT_LOCKS: dict[str, threading.RLock] = {}
_ROOT_LOCKS_GUARD = threading.Lock()


def replay_dir(run_dir: str | Path) -> Path:
    return Path(run_dir) / REPLAY_DIRNAME


def replay_exists(run_dir: str | Path) -> bool:
    return (replay_dir(run_dir) / "manifest.json").exists()


@contextmanager
def _snapshot_lock(root: Path, *, exclusive: bool):
    """Coordinate snapshot readers/writers across threads and processes."""
    ensure_dir(root)
    key = str(root.resolve())
    with _ROOT_LOCKS_GUARD:
        thread_lock = _ROOT_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        lock_path = root / ".snapshot.lock"
        stream = lock_path.open("a+b")
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


def _remove_stale_temps(root: Path) -> None:
    for path in root.rglob(".tmp_*"):
        if path.is_file():
            path.unlink(missing_ok=True)


def _atomic_write_parquet(values: pd.DataFrame, path: Path) -> None:
    ensure_dir(path)
    temp = path.with_name(f".tmp_{uuid4().hex}_{path.name}")
    try:
        values.to_parquet(temp, compression="snappy")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_group(
    root: Path,
    group: str,
    matrices: dict[str, pd.DataFrame],
) -> list[Path]:
    paths: list[Path] = []
    for name, values in matrices.items():
        path = root / group / f"{name}.parquet"
        _atomic_write_parquet(values, path)
        paths.append(path)
    return paths


def save_snapshot(
    run_dir: str | Path,
    snapshot: DecisionReplaySnapshot,
) -> Path:
    """Write a complete snapshot and publish ``manifest.json`` last."""
    root = replay_dir(run_dir)
    with _snapshot_lock(root, exclusive=True):
        return _save_snapshot_locked(root, snapshot)


def _save_snapshot_locked(
    root: Path,
    snapshot: DecisionReplaySnapshot,
) -> Path:
    _remove_stale_temps(root)
    manifest_path = root / "manifest.json"
    # An interrupted rewrite must become unavailable, never look complete while
    # some Parquet files already belong to the new generation.
    manifest_path.unlink(missing_ok=True)
    for artifact in root.rglob("*.parquet"):
        artifact.unlink()
    for managed_group in ("market", "signals", "portfolio", "factors"):
        shutil.rmtree(root / managed_group, ignore_errors=True)
    written: list[Path] = []

    summary_path = root / "daily_summary.parquet"
    _atomic_write_parquet(snapshot.daily_summary, summary_path)
    written.append(summary_path)
    written.extend(_write_group(root, "market", snapshot.market))
    written.extend(_write_group(root, "signals", snapshot.signals))
    written.extend(_write_group(root, "portfolio", snapshot.portfolio))
    for factor_id, matrices in snapshot.factors.items():
        written.extend(
            _write_group(root, f"factors/{factor_id}", matrices)
        )

    manifest = dict(snapshot.manifest)
    manifest["artifact_sha256"] = {
        str(path.relative_to(root)): _file_sha256(path)
        for path in sorted(written)
    }
    atomic_save_json(manifest, manifest_path)
    return root


def _merge_rows(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    if existing.empty:
        out = incoming.copy()
    elif incoming.empty:
        out = existing.copy()
    else:
        out = pd.concat([existing, incoming])
        out = out[~out.index.duplicated(keep="last")]
    return out.sort_index()


def _upsert_group(
    root: Path,
    group: str,
    matrices: dict[str, pd.DataFrame],
) -> list[Path]:
    paths: list[Path] = []
    for name, incoming in matrices.items():
        path = root / group / f"{name}.parquet"
        existing = read_parquet(path) if path.exists() else pd.DataFrame()
        _atomic_write_parquet(_merge_rows(existing, incoming), path)
        paths.append(path)
    return paths


def upsert_snapshot(
    run_dir: str | Path,
    snapshot: DecisionReplaySnapshot,
) -> Path:
    """
    Idempotently add or replace snapshot dates.

    Paper trading uses this on every run. Matrix rows with the same date are
    replaced, so retrying one decision date never creates duplicates.
    """
    root = replay_dir(run_dir)
    with _snapshot_lock(root, exclusive=True):
        return _upsert_snapshot_locked(root, snapshot)


def _upsert_snapshot_locked(
    root: Path,
    snapshot: DecisionReplaySnapshot,
) -> Path:
    _remove_stale_temps(root)
    manifest_path = root / "manifest.json"
    previous_manifest = load_json(manifest_path) if manifest_path.exists() else {}
    manifest_path.unlink(missing_ok=True)
    summary_path = root / "daily_summary.parquet"
    existing_summary = (
        read_parquet(summary_path) if summary_path.exists() else pd.DataFrame()
    )
    _atomic_write_parquet(
        _merge_rows(existing_summary, snapshot.daily_summary),
        summary_path,
    )
    written = [summary_path]
    written.extend(_upsert_group(root, "market", snapshot.market))
    written.extend(_upsert_group(root, "signals", snapshot.signals))
    written.extend(_upsert_group(root, "portfolio", snapshot.portfolio))
    for factor_id, matrices in snapshot.factors.items():
        written.extend(
            _upsert_group(root, f"factors/{factor_id}", matrices)
        )

    manifest = {**previous_manifest, **snapshot.manifest}
    merged_summary = read_parquet(summary_path)
    if not merged_summary.empty:
        merged_dates = pd.DatetimeIndex(merged_summary.index)
        manifest["date_start"] = merged_dates.min().strftime("%Y-%m-%d")
        manifest["date_end"] = merged_dates.max().strftime("%Y-%m-%d")
        manifest["trading_days"] = int(len(merged_dates.unique()))
    manifest["updated_at"] = snapshot.manifest.get("created_at")
    manifest["artifact_sha256"] = {
        str(path.relative_to(root)): _file_sha256(path)
        for path in sorted(root.rglob("*.parquet"))
    }
    atomic_save_json(manifest, manifest_path)
    return root


def _read_group(root: Path, group: str) -> dict[str, pd.DataFrame]:
    directory = root / group
    if not directory.exists():
        return {}
    return {
        path.stem: read_parquet(path)
        for path in sorted(directory.glob("*.parquet"))
    }


def _validated_manifest(root: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError("Decision replay manifest must be a JSON object")
    version = manifest.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(
            f"Unsupported decision replay schema_version={version!r}"
        )
    hashes = manifest.get("artifact_sha256")
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("Decision replay manifest has no artifact hashes")

    root_resolved = root.resolve()
    expected_paths: dict[str, tuple[Path, str]] = {}
    for relative, expected_hash in hashes.items():
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            raise ValueError("Invalid decision replay artifact hash entry")
        relative_path = Path(relative)
        path = (root / relative_path).resolve()
        if (
            relative_path.is_absolute()
            or root_resolved not in path.parents
            or path.suffix != ".parquet"
        ):
            raise ValueError(
                f"Unsafe decision replay artifact path: {relative!r}"
            )
        normalized = relative_path.as_posix()
        if normalized in expected_paths:
            raise ValueError(
                f"Duplicate decision replay artifact path: {relative!r}"
            )
        expected_paths[normalized] = (path, expected_hash)

    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.parquet")
        if path.is_file()
    }
    expected_names = set(expected_paths)
    if actual_paths != expected_names:
        missing = sorted(expected_names - actual_paths)
        unexpected = sorted(actual_paths - expected_names)
        raise ValueError(
            "Decision replay artifact set does not match manifest: "
            f"missing={missing}, unexpected={unexpected}"
        )
    for relative, (path, expected_hash) in expected_paths.items():
        actual_hash = _file_sha256(path)
        if actual_hash != expected_hash:
            raise ValueError(
                f"Decision replay artifact hash mismatch: {relative}"
            )
    return manifest


def load_snapshot(run_dir: str | Path) -> DecisionReplaySnapshot | None:
    root = replay_dir(run_dir)
    with _snapshot_lock(root, exclusive=False):
        manifest_path = root / "manifest.json"
        if not manifest_path.exists():
            return None
        manifest = _validated_manifest(root, manifest_path)
        factor_root = root / "factors"
        factors: dict[str, dict[str, pd.DataFrame]] = {}
        if factor_root.exists():
            for directory in sorted(factor_root.iterdir()):
                if directory.is_dir():
                    factors[directory.name] = _read_group(
                        root,
                        f"factors/{directory.name}",
                    )
        summary_path = root / "daily_summary.parquet"
        summary = (
            read_parquet(summary_path)
            if summary_path.exists()
            else pd.DataFrame()
        )
        return DecisionReplaySnapshot(
            manifest=manifest,
            daily_summary=summary,
            market=_read_group(root, "market"),
            signals=_read_group(root, "signals"),
            factors=factors,
            portfolio=_read_group(root, "portfolio"),
        )


def manifest_mtime_ns(run_dir: str | Path) -> int:
    path = replay_dir(run_dir) / "manifest.json"
    return path.stat().st_mtime_ns if path.exists() else 0


def artifact_state_token(run_dir: str | Path) -> tuple[tuple[str, int, int], ...]:
    """Return a cheap cache token that changes when any artifact changes."""
    root = replay_dir(run_dir)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return ()
    paths = [manifest_path, *sorted(root.rglob("*.parquet"))]
    token: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return ()
        token.append((
            path.relative_to(root).as_posix(),
            stat.st_mtime_ns,
            stat.st_size,
        ))
    return tuple(token)


__all__ = [
    "REPLAY_DIRNAME",
    "replay_dir",
    "replay_exists",
    "save_snapshot",
    "upsert_snapshot",
    "load_snapshot",
    "manifest_mtime_ns",
    "artifact_state_token",
]
