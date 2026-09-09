"""Version-controlled registry for expected production jobs and deadlines."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import yaml

from src.config import PROJECT_ROOT
from src.operations.models import JobDefinition


DEFAULT_OPERATIONS_CONFIG = PROJECT_ROOT / "configs" / "operations.yaml"
_JOB_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SYSTEMD_UNIT = re.compile(r"^[A-Za-z0-9_.@:-]+\.(service|timer)$")


@dataclass(frozen=True)
class OperationsSettings:
    database_path: Path
    snapshot_path: Path
    retention_days: int
    web_host: str
    web_port: int
    refresh_seconds: int
    watchdog_interval_seconds: int
    heartbeat_grace_seconds: int
    inspect_systemd: str
    external_notifications: bool
    minimum_free_disk_gb: float
    minimum_available_memory_mb: int


def _project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


class OperationsRegistry:
    def __init__(self, path: str | Path = DEFAULT_OPERATIONS_CONFIG):
        self.path = Path(path)
        self._settings: OperationsSettings | None = None
        self._jobs: tuple[JobDefinition, ...] | None = None

    def _load(self) -> None:
        payload = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        if int(payload.get("schema_version") or 0) != 1:
            raise ValueError("operations config schema_version must be 1")
        root = payload.get("operations") or {}
        web = root.get("web") or {}
        watchdog = root.get("watchdog") or {}
        resources = root.get("resources") or {}
        inspect_systemd = str(watchdog.get("inspect_systemd") or "auto").lower()
        if inspect_systemd not in {"auto", "on", "off"}:
            raise ValueError("operations.watchdog.inspect_systemd must be auto/on/off")
        self._settings = OperationsSettings(
            database_path=_project_path(root["database_path"]),
            snapshot_path=_project_path(root["snapshot_path"]),
            retention_days=max(1, int(root.get("retention_days") or 180)),
            web_host=str(web.get("host") or "127.0.0.1"),
            web_port=int(web.get("port") or 18825),
            refresh_seconds=max(5, int(web.get("refresh_seconds") or 15)),
            watchdog_interval_seconds=max(
                15, int(watchdog.get("interval_seconds") or 60)
            ),
            heartbeat_grace_seconds=max(
                30, int(watchdog.get("heartbeat_grace_seconds") or 120)
            ),
            inspect_systemd=inspect_systemd,
            external_notifications=bool(
                watchdog.get("external_notifications", False)
            ),
            minimum_free_disk_gb=float(
                resources.get("minimum_free_disk_gb") or 15
            ),
            minimum_available_memory_mb=int(
                resources.get("minimum_available_memory_mb") or 350
            ),
        )
        if self._settings.external_notifications:
            raise ValueError(
                "external operations notifications are disabled by product decision"
            )

        jobs: list[JobDefinition] = []
        seen: set[str] = set()
        for raw in payload.get("jobs") or []:
            if not isinstance(raw, dict):
                raise ValueError("operations jobs must be mappings")
            job_id = str(raw.get("job_id") or "")
            if not _JOB_ID.fullmatch(job_id) or job_id in seen:
                raise ValueError(f"invalid or duplicate operations job_id: {job_id!r}")
            seen.add(job_id)
            for key in ("service_unit", "timer_unit"):
                value = raw.get(key)
                if value and not _SYSTEMD_UNIT.fullmatch(str(value)):
                    raise ValueError(f"invalid {key} for {job_id}: {value!r}")
            jobs.append(JobDefinition(
                job_id=job_id,
                display_name=str(raw.get("display_name") or job_id),
                category=str(raw.get("category") or "other").upper(),
                run_type=str(raw.get("run_type") or "scheduled_batch").upper(),
                adapter=str(raw.get("adapter") or "").lower(),
                order=int(raw.get("order") or 0),
                enabled_expected=bool(raw.get("enabled_expected", True)),
                service_unit=(
                    str(raw["service_unit"]) if raw.get("service_unit") else None
                ),
                timer_unit=(
                    str(raw["timer_unit"]) if raw.get("timer_unit") else None
                ),
                schedule=dict(raw.get("schedule") or {}),
                evidence=dict(raw.get("evidence") or {}),
                description=str(raw.get("description") or ""),
            ))
        if not jobs:
            raise ValueError("operations registry cannot be empty")
        self._jobs = tuple(sorted(jobs, key=lambda item: (item.order, item.job_id)))

    @property
    def settings(self) -> OperationsSettings:
        if self._settings is None:
            self._load()
        assert self._settings is not None
        return self._settings

    def list(self) -> list[JobDefinition]:
        if self._jobs is None:
            self._load()
        assert self._jobs is not None
        return list(self._jobs)

    def get(self, job_id: str) -> JobDefinition:
        for job in self.list():
            if job.job_id == job_id:
                return job
        raise KeyError(job_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "settings": {
                **self.settings.__dict__,
                "database_path": str(self.settings.database_path),
                "snapshot_path": str(self.settings.snapshot_path),
            },
            "jobs": [job.to_dict() for job in self.list()],
        }


_REGISTRY: OperationsRegistry | None = None


def operations_registry(*, reload: bool = False) -> OperationsRegistry:
    global _REGISTRY
    if _REGISTRY is None or reload:
        _REGISTRY = OperationsRegistry()
    return _REGISTRY


__all__ = [
    "DEFAULT_OPERATIONS_CONFIG",
    "OperationsRegistry",
    "OperationsSettings",
    "operations_registry",
]

