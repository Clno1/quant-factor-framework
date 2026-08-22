"""Read-only systemd inspection used by the watchdog on Linux hosts."""
from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Any, Iterable

from src.operations.models import JobDefinition


_PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainStatus",
    "ActiveEnterTimestamp",
    "InactiveEnterTimestamp",
    "LastTriggerUSec",
    "NextElapseUSecRealtime",
    "UnitFileState",
)


class SystemdInspector:
    def __init__(self, mode: str = "auto"):
        self.mode = str(mode).lower()
        self.available = bool(
            self.mode != "off"
            and shutil.which("systemctl")
            and (self.mode == "on" or sys.platform.startswith("linux"))
        )

    @staticmethod
    def _parse(text: str) -> dict[str, dict[str, str]]:
        output: dict[str, dict[str, str]] = {}
        current: dict[str, str] = {}
        for line in [*text.splitlines(), ""]:
            if not line.strip():
                unit_id = current.get("Id")
                if unit_id:
                    output[unit_id] = current
                current = {}
                continue
            key, separator, value = line.partition("=")
            if separator:
                current[key] = value
        return output

    def inspect(self, jobs: Iterable[JobDefinition]) -> dict[str, dict[str, Any]]:
        if not self.available:
            return {}
        units = sorted({
            unit
            for job in jobs
            for unit in (job.service_unit, job.timer_unit)
            if unit
        })
        if not units:
            return {}
        command = [
            "systemctl",
            "show",
            *units,
            "--no-pager",
            "--property=" + ",".join(_PROPERTIES),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        by_unit = self._parse(completed.stdout)
        output: dict[str, dict[str, Any]] = {}
        for job in jobs:
            service = by_unit.get(str(job.service_unit), {})
            timer = by_unit.get(str(job.timer_unit), {})
            output[job.job_id] = {
                "available": True,
                "service": service,
                "timer": timer,
                "service_active": service.get("ActiveState"),
                "service_substate": service.get("SubState"),
                "service_result": service.get("Result"),
                "service_exit_status": service.get("ExecMainStatus"),
                "timer_active": timer.get("ActiveState"),
                "timer_enabled": timer.get("UnitFileState"),
                "last_trigger": timer.get("LastTriggerUSec"),
                "next_trigger": timer.get("NextElapseUSecRealtime"),
            }
        return output


__all__ = ["SystemdInspector"]
