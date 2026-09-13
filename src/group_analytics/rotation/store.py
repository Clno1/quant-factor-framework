"""Immutable, checksum-verified snapshots and atomic last-success pointer."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from ..adapters import _atomic_json, _exclusive_file_lock
from ..artifacts import normalize_json_value
from ..settings import load_group_analytics_settings
from . import READABLE_SCHEMA_VERSIONS, SCHEMA_VERSION

SAFE_RUN = re.compile(r"^rot_[0-9]{8}_[a-f0-9]{16}$")
ATTEMPT_STAGES = frozenset({"price", "linkage"})


class RotationParentMoved(ValueError):
    """Latest price identity changed before a linkage write could take the lock."""

    code = "ROTATION_PARENT_MOVED"

    def __init__(self, message="关联期间价格快照已被更新的价格版本替换，未覆盖较新结果"):
        super().__init__(message)


def default_root():
    return load_group_analytics_settings().output_root / "group_analytics" / "rotation"


def encoded(value):
    return json.dumps(normalize_json_value(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode()


def price_parent_identity(snapshot):
    if not snapshot:
        return None
    return (
        snapshot.get("source_session"),
        snapshot.get("publish_fingerprint") or snapshot.get("input_fingerprint"),
        snapshot.get("run_id"),
    )


class RotationStore:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else default_root()

    @property
    def initialized(self):
        return (self.root / "latest.json").exists() or (self.root / "last_attempt.json").exists()

    def _read_attempt_unlocked(self):
        try:
            value = json.loads((self.root / "last_attempt.json").read_text())
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _merge_checks(self, current, *, stage, status, source_session, published=None,
                      code=None, run_id=None):
        checks = {}
        raw = current.get("checks") if isinstance(current, dict) else None
        if isinstance(raw, dict):
            for key, entry in raw.items():
                if key in ATTEMPT_STAGES and isinstance(entry, dict):
                    checks[key] = dict(entry)
        if stage in ATTEMPT_STAGES:
            entry = {"status": status, "source_session": source_session}
            if published is not None:
                entry["published"] = bool(published)
            if code:
                entry["code"] = code
            if run_id:
                entry["run_id"] = run_id
            checks[stage] = entry
        return checks

    def publish(self, snapshot, *, expected_parent=None, stage=None):
        snapshot = normalize_json_value(snapshot)
        snapshot.pop("run_id", None)
        snapshot.pop("schema_legacy", None)
        if snapshot.get("schema_version") != SCHEMA_VERSION or not snapshot.get("rows"):
            raise ValueError("Invalid rotation snapshot")
        data = encoded(snapshot)
        digest = hashlib.sha256(data).hexdigest()
        run_id = "rot_" + snapshot["source_session"].replace("-", "") + "_" + digest[:16]
        if not SAFE_RUN.fullmatch(run_id):
            raise ValueError("Invalid source session")
        with _exclusive_file_lock(self.root / ".lock"):
            if expected_parent is not None:
                latest = self.load() if (self.root / "latest.json").exists() else None
                if price_parent_identity(latest) != tuple(expected_parent):
                    raise RotationParentMoved()
            target = self.root / "runs" / (run_id + ".json")
            if target.exists():
                if hashlib.sha256(encoded(json.loads(target.read_text())["snapshot"])).hexdigest() != digest:
                    raise ValueError("Immutable run conflict")
            else:
                # _atomic_json has a different whitespace serialization; write
                # a normalized envelope whose digest covers canonical payload.
                _atomic_json(target, {"sha256": digest, "snapshot": snapshot})
            pointer = {"run_id": run_id, "sha256": digest, "source_session": snapshot["source_session"]}
            old = self.load() if (self.root / "latest.json").exists() else None
            if old is None or old["source_session"] <= snapshot["source_session"]:
                _atomic_json(self.root / "latest.json", pointer)
            payload = {**pointer, "status": "SUCCESS", "published": True}
            if stage in ATTEMPT_STAGES:
                existing = self._read_attempt_unlocked()
                payload["stage"] = stage
                payload["checks"] = self._merge_checks(
                    existing, stage=stage, status="SUCCESS",
                    source_session=snapshot["source_session"], published=True, run_id=run_id,
                )
            _atomic_json(self.root / "last_attempt.json", payload)
        return run_id

    def failure(self, source_session, code, *, stage=None):
        with _exclusive_file_lock(self.root / ".lock"):
            if stage in ATTEMPT_STAGES:
                existing = self._read_attempt_unlocked()
                payload = {
                    "source_session": source_session,
                    "status": "FAILED",
                    "code": code,
                    "stage": stage,
                    "published": False,
                    "checks": self._merge_checks(
                        existing, stage=stage, status="FAILED",
                        source_session=source_session, published=False, code=code,
                    ),
                }
            else:
                payload = {"source_session": source_session, "status": "FAILED", "code": code}
            _atomic_json(self.root / "last_attempt.json", payload)

    def record_check(self, source_session, *, stage, published=False, run_id=None):
        if stage not in ATTEMPT_STAGES:
            raise ValueError("Invalid rotation check stage")
        with _exclusive_file_lock(self.root / ".lock"):
            existing = self._read_attempt_unlocked()
            payload = {
                "source_session": source_session,
                "status": "SUCCESS",
                "stage": stage,
                "published": bool(published),
                "checks": self._merge_checks(
                    existing, stage=stage, status="SUCCESS",
                    source_session=source_session, published=published, run_id=run_id,
                ),
            }
            if run_id:
                payload["run_id"] = run_id
            elif existing.get("run_id"):
                payload["run_id"] = existing["run_id"]
            _atomic_json(self.root / "last_attempt.json", payload)

    def last_attempt(self):
        try:
            value = json.loads((self.root / "last_attempt.json").read_text())
            checks = {}
            raw = value.get("checks") if isinstance(value, dict) else None
            if isinstance(raw, dict):
                for key in ("price", "linkage"):
                    entry = raw.get(key)
                    if isinstance(entry, dict) and entry.get("status") in {"SUCCESS", "FAILED"}:
                        checks[key] = {
                            "status": entry["status"],
                            "source_session": entry.get("source_session"),
                            "published": entry.get("published"),
                        }
            if checks:
                status = "FAILED" if any(item["status"] == "FAILED" for item in checks.values()) else "SUCCESS"
            else:
                status = value["status"] if value["status"] in {"SUCCESS", "FAILED"} else "UNKNOWN"
            stage = value.get("stage") if value.get("stage") in ATTEMPT_STAGES else None
            return {
                "status": status,
                "source_session": value.get("source_session"),
                "stage": stage,
                "published": value.get("published"),
                "checks": checks,
            }
        except (OSError, ValueError, KeyError, TypeError):
            return {"status": "UNKNOWN", "source_session": None}

    def load(self, run_id=None):
        pointer = None
        if run_id is None:
            pointer = json.loads((self.root / "latest.json").read_text())
            run_id = pointer["run_id"]
        if not isinstance(run_id, str) or not SAFE_RUN.fullmatch(run_id):
            raise ValueError("Invalid rotation run id")
        path = self.root / "runs" / (run_id + ".json")
        if path.is_symlink() or self.root.resolve() not in path.resolve().parents:
            raise ValueError("Unsafe rotation path")
        envelope = json.loads(path.read_text())
        snapshot = envelope["snapshot"]
        digest = hashlib.sha256(encoded(snapshot)).hexdigest()
        if digest != envelope.get("sha256") or run_id[-16:] != digest[:16]:
            raise ValueError("Rotation integrity check failed")
        if pointer and (pointer.get("sha256") != digest or pointer.get("source_session") != snapshot.get("source_session")):
            raise ValueError("Rotation pointer mismatch")
        schema = snapshot.get("schema_version")
        if schema not in READABLE_SCHEMA_VERSIONS or not isinstance(snapshot.get("rows"), list):
            raise ValueError("Unsupported rotation schema")
        return {**snapshot, "run_id": run_id, "schema_legacy": schema != SCHEMA_VERSION}
