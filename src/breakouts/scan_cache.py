"""Persistent cache for expensive broad-universe momentum scans."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Mapping

from src.config import PROJECT_ROOT
from src.utils.file_lock import file_lock
from src.utils.io import atomic_save_json

_CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "momentum_scans"
_CACHE_VERSION = 1


def _cache_path(parameters: Mapping[str, Any]) -> Path:
    encoded = json.dumps(parameters, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return _CACHE_DIR / f"{digest}.json"


def load_scan_cache(
    parameters: Mapping[str, Any],
    *,
    max_age_seconds: float = 6 * 60 * 60,
) -> dict[str, Any] | None:
    path = _cache_path(parameters)
    if not path.exists() or time.time() - path.stat().st_mtime > max(0.0, max_age_seconds):
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("version") != _CACHE_VERSION or not isinstance(payload.get("scan"), dict):
        return None
    return payload["scan"]


def save_scan_cache(parameters: Mapping[str, Any], scan: Mapping[str, Any]) -> Path:
    path = _cache_path(parameters)
    return atomic_save_json({"version": _CACHE_VERSION, "scan": scan}, path)


def request_scan_build(
    parameters: Mapping[str, Any],
    *,
    enabled_universes: Iterable[str],
    force: bool = False,
) -> dict[str, Any]:
    """Enqueue a version-bound scan; Web callers never materialize price matrices."""
    path = _CACHE_DIR / "requests" / _cache_path(parameters).name
    with file_lock(_CACHE_DIR / "requests.lock"):
        job = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if job and (
            job["status"] in {"PENDING", "RUNNING"}
            or (job["status"] == "FAILED" and not force)
        ):
            return job
        job = {
            "request_id": path.stem,
            "parameters": dict(parameters),
            "enabled_universes": list(enabled_universes),
            "status": "PENDING",
            "attempts": 0,
            "requested_at": time.time(),
        }
        atomic_save_json(job, path)
        return job


def process_scan_build_requests(
    builder: Callable[..., dict[str, Any]], *, limit: int = 1,
) -> list[dict[str, Any]]:
    """Single background worker, bounded retries and recoverable interrupted jobs."""
    results: list[dict[str, Any]] = []
    with file_lock(_CACHE_DIR / "worker.lock"):
        paths = sorted(
            (_CACHE_DIR / "requests").glob("*.json"),
            key=lambda path: path.stat().st_mtime,
        )
        for path in paths:
            if len(results) >= max(1, limit):
                break
            with file_lock(_CACHE_DIR / "requests.lock"):
                job = json.loads(path.read_text(encoding="utf-8"))
                if job["attempts"] >= 3 and job["status"] == "RUNNING":
                    job.update(status="FAILED", error="Background scan interrupted; retry limit reached")
                    atomic_save_json(job, path)
                if job["status"] == "SUCCESS" or job["attempts"] >= 3:
                    continue
                job.update(status="RUNNING", attempts=job["attempts"] + 1)
                atomic_save_json(job, path)
            try:
                scan = builder(
                    **job["parameters"], enabled_universes=job["enabled_universes"],
                )
                save_scan_cache(job["parameters"], scan)
                job.update(status="SUCCESS", error=None)
            except Exception as exc:
                job.update(status="FAILED", error=f"{type(exc).__name__}: {exc}")
            job["finished_at"] = time.time()
            with file_lock(_CACHE_DIR / "requests.lock"):
                atomic_save_json(job, path)
            results.append(job)
    return results


def clear_scan_cache() -> int:
    if not _CACHE_DIR.exists():
        return 0
    removed = 0
    for path in _CACHE_DIR.glob("*.json"):
        path.unlink(missing_ok=True)
        removed += 1
    return removed


__all__ = [
    "clear_scan_cache", "load_scan_cache", "save_scan_cache",
    "request_scan_build", "process_scan_build_requests",
]
