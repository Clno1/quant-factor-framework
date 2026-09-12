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


def default_root():
    return load_group_analytics_settings().output_root / "group_analytics" / "rotation"


def encoded(value):
    return json.dumps(normalize_json_value(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode()


class RotationStore:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else default_root()

    @property
    def initialized(self):
        return (self.root / "latest.json").exists() or (self.root / "last_attempt.json").exists()

    def publish(self, snapshot):
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
            _atomic_json(self.root / "last_attempt.json", {**pointer, "status": "SUCCESS"})
        return run_id

    def failure(self, source_session, code):
        with _exclusive_file_lock(self.root / ".lock"):
            _atomic_json(self.root / "last_attempt.json", {"source_session": source_session,
                                                           "status": "FAILED", "code": code})

    def last_attempt(self):
        try:
            value = json.loads((self.root / "last_attempt.json").read_text())
            return {"status": value["status"] if value["status"] in {"SUCCESS", "FAILED"} else "UNKNOWN",
                    "source_session": value.get("source_session")}
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
