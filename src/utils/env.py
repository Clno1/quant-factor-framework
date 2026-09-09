"""Small KEY=VALUE loader for local and systemd-managed secret files."""
from __future__ import annotations

import os
from pathlib import Path

from src.config import PROJECT_ROOT


def load_local_env(path: str | Path | None = None) -> Path | None:
    """Load simple KEY=VALUE entries without overriding process variables."""
    env_path = Path(path) if path is not None else PROJECT_ROOT / ".env.local"
    if not env_path.exists():
        return None
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return env_path


__all__ = ["load_local_env"]
