"""Persistent cache for expensive broad-universe momentum scans."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from typing import Any, Mapping

from src.config import PROJECT_ROOT

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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"version": _CACHE_VERSION, "scan": scan}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def clear_scan_cache() -> int:
    if not _CACHE_DIR.exists():
        return 0
    removed = 0
    for path in _CACHE_DIR.glob("*.json"):
        path.unlink(missing_ok=True)
        removed += 1
    return removed


__all__ = ["clear_scan_cache", "load_scan_cache", "save_scan_cache"]
