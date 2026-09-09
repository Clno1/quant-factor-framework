"""One-shot watchdog that reconciles all operational evidence every minute."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import time
from typing import Any, Callable
from uuid import uuid4

from src.config import PROJECT_ROOT
from src.operations.adapters import (
    collect_application_evidence,
    collect_broad_evidence,
    collect_delivery_evidence,
    collect_market_evidence,
    collect_research_evidence,
)
from src.operations.evidence import safe_text
from src.operations.models import (
    CollectionResult,
    FreshnessObservation,
    IncidentCandidate,
    IncidentSeverity,
    JobDefinition,
    JobSnapshot,
    JobStatus,
)
from src.operations.registry import OperationsRegistry, operations_registry
from src.operations.store import OperationsStore
from src.operations.systemd import SystemdInspector


Collector = Callable[..., CollectionResult]

_COLLECTOR_ADAPTERS = {
    "market": {"market_data"},
    "research": {"factor_research", "group_analytics"},
    "delivery": {
        "premarket",
        "premarket_prepare",
        "hourly_momentum",
        "intraday_candidate_prepare",
        "intraday_momentum",
    },
    "application": {
        "data_requests",
        "paper_trading",
        "paper_notifications",
    },
    "broad": {"broad_pipeline"},
}

_STATUS_INCIDENT_TITLES = {
    JobStatus.FAILED: "任务执行失败",
    JobStatus.MISSED: "任务未按时运行",
    JobStatus.STALE: "任务证据已过期",
    JobStatus.DEGRADED: "任务部分异常",
    JobStatus.BLOCKED: "任务被前置条件阻断",
}


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _available_memory_mb() -> float | None:
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        values: dict[str, int] = {}
        try:
            for line in meminfo.read_text(encoding="ascii").splitlines():
                key, separator, raw = line.partition(":")
                if separator and raw.strip().split()[0].isdigit():
                    values[key] = int(raw.strip().split()[0])
        except (OSError, IndexError):
            return None
        if "MemAvailable" in values:
            return values["MemAvailable"] / 1024.0
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        return None
    return float(pages * page_size) / (1024.0 * 1024.0)


def _resource_evidence(
    *,
    observed_at: str,
    minimum_disk_gb: float,
    minimum_memory_mb: int,
) -> tuple[list[FreshnessObservation], list[IncidentCandidate]]:
    disk_gb = shutil.disk_usage(PROJECT_ROOT).free / (1024.0 ** 3)
    memory_mb = _available_memory_mb()
    disk_status = (
        JobStatus.SUCCESS if disk_gb >= minimum_disk_gb else JobStatus.DEGRADED
    )
    memory_status = (
        JobStatus.UNKNOWN if memory_mb is None
        else JobStatus.SUCCESS if memory_mb >= minimum_memory_mb
        else JobStatus.DEGRADED
    )
    rows = [
        FreshnessObservation(
            object_id="resource:disk",
            display_name="项目磁盘可用空间",
            category="RESOURCE",
            status=disk_status,
            observed_at=observed_at,
            quality={
                "available_gb": round(disk_gb, 3),
                "minimum_gb": minimum_disk_gb,
            },
            source="statvfs",
        ),
        FreshnessObservation(
            object_id="resource:memory",
            display_name="服务器可用内存",
            category="RESOURCE",
            status=memory_status,
            observed_at=observed_at,
            quality={
                "available_mb": round(memory_mb, 3) if memory_mb is not None else None,
                "minimum_mb": minimum_memory_mb,
            },
            source="proc_meminfo",
        ),
    ]
    incidents: list[IncidentCandidate] = []
    if disk_status == JobStatus.DEGRADED:
        incidents.append(IncidentCandidate(
            fingerprint="resource:disk_low",
            severity=IncidentSeverity.CRITICAL,
            code="DISK_SPACE_LOW",
            title="服务器磁盘空间不足",
            detail=f"当前仅剩 {disk_gb:.2f} GB，门槛为 {minimum_disk_gb:.2f} GB",
            job_id="operations_watchdog",
        ))
    if memory_status == JobStatus.DEGRADED:
        incidents.append(IncidentCandidate(
            fingerprint="resource:memory_low",
            severity=IncidentSeverity.WARNING,
            code="AVAILABLE_MEMORY_LOW",
            title="服务器可用内存偏低",
            detail=f"当前仅剩 {memory_mb:.1f} MB，门槛为 {minimum_memory_mb} MB",
            job_id="operations_watchdog",
        ))
    return rows, incidents


def _systemd_incidents(
    job: JobDefinition,
    systemd: dict[str, Any],
) -> list[IncidentCandidate]:
    if not systemd or not job.enabled_expected:
        return []
    incidents: list[IncidentCandidate] = []
    service = systemd.get("service") or {}
    timer = systemd.get("timer") or {}
    if job.service_unit and str(service.get("LoadState") or "") != "loaded":
        incidents.append(IncidentCandidate(
            fingerprint=f"systemd:{job.job_id}:service_not_loaded",
            severity=IncidentSeverity.CRITICAL,
            code="SYSTEMD_SERVICE_NOT_LOADED",
            title=f"{job.display_name} systemd 服务未安装",
            detail=f"{job.service_unit} 的 LoadState={service.get('LoadState') or 'unknown'}",
            job_id=job.job_id,
            metadata={"service_unit": job.service_unit},
        ))
    if job.timer_unit and str(timer.get("LoadState") or "") != "loaded":
        incidents.append(IncidentCandidate(
            fingerprint=f"systemd:{job.job_id}:timer_not_loaded",
            severity=IncidentSeverity.CRITICAL,
            code="SYSTEMD_TIMER_NOT_LOADED",
            title=f"{job.display_name} systemd 定时器未安装",
            detail=f"{job.timer_unit} 的 LoadState={timer.get('LoadState') or 'unknown'}",
            job_id=job.job_id,
            metadata={"timer_unit": job.timer_unit},
        ))
    service_result = str(systemd.get("service_result") or "")
    exit_status = str(systemd.get("service_exit_status") or "")
    if service_result == "failed" or (
        exit_status not in {"", "0"} and service_result not in {"success", "done"}
    ):
        incidents.append(IncidentCandidate(
            fingerprint=f"systemd:{job.job_id}:service_failed",
            severity=IncidentSeverity.CRITICAL,
            code="SYSTEMD_SERVICE_FAILED",
            title=f"{job.display_name} systemd 服务失败",
            detail=f"Result={service_result or 'unknown'}，ExecMainStatus={exit_status or 'unknown'}",
            job_id=job.job_id,
            metadata={"service_unit": job.service_unit},
        ))
    if (
        job.timer_unit
        and str(timer.get("LoadState") or "") == "loaded"
        and str(systemd.get("timer_active") or "") != "active"
    ):
        incidents.append(IncidentCandidate(
            fingerprint=f"systemd:{job.job_id}:timer_inactive",
            severity=IncidentSeverity.WARNING,
            code="SYSTEMD_TIMER_INACTIVE",
            title=f"{job.display_name} 定时器未运行",
            detail=f"{job.timer_unit} 当前 ActiveState={systemd.get('timer_active') or 'unknown'}",
            job_id=job.job_id,
            metadata={"timer_unit": job.timer_unit},
        ))
    if (
        job.timer_unit
        and str(timer.get("LoadState") or "") == "loaded"
        and str(systemd.get("timer_enabled") or "") not in {"enabled", "static"}
    ):
        incidents.append(IncidentCandidate(
            fingerprint=f"systemd:{job.job_id}:timer_disabled",
            severity=IncidentSeverity.WARNING,
            code="SYSTEMD_TIMER_DISABLED",
            title=f"{job.display_name} systemd 定时器未启用",
            detail=f"{job.timer_unit} 的 UnitFileState={systemd.get('timer_enabled') or 'unknown'}",
            job_id=job.job_id,
            metadata={"timer_unit": job.timer_unit},
        ))
    return incidents


def _status_incident(snapshot: JobSnapshot) -> IncidentCandidate | None:
    severity = {
        JobStatus.FAILED: IncidentSeverity.CRITICAL,
        JobStatus.MISSED: IncidentSeverity.CRITICAL,
        JobStatus.STALE: IncidentSeverity.CRITICAL,
        JobStatus.DEGRADED: IncidentSeverity.WARNING,
        JobStatus.BLOCKED: IncidentSeverity.WARNING,
    }.get(snapshot.status)
    if severity is None:
        return None
    return IncidentCandidate(
        fingerprint=f"job:{snapshot.job_id}:{snapshot.status.value}",
        severity=severity,
        code=f"JOB_{snapshot.status.value}",
        title=_STATUS_INCIDENT_TITLES[snapshot.status],
        detail=snapshot.status_reason or "任务证据未达到预期门槛",
        job_id=snapshot.job_id,
        target_session=snapshot.target_session,
        run_id=snapshot.run_id,
    )


class OperationsWatchdog:
    def __init__(
        self,
        registry: OperationsRegistry | None = None,
        store: OperationsStore | None = None,
        *,
        inspect_systemd: bool | None = None,
    ):
        self.registry = registry or operations_registry()
        settings = self.registry.settings
        self.store = store or OperationsStore(
            settings.database_path,
            settings.snapshot_path,
        )
        mode = settings.inspect_systemd
        if inspect_systemd is False:
            mode = "off"
        elif inspect_systemd is True:
            mode = "on"
        self.systemd = SystemdInspector(mode)

    def run_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        started_clock = time.perf_counter()
        started = now or datetime.now(timezone.utc)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        else:
            started = started.astimezone(timezone.utc)
        observed_at = _iso(started)
        run_id = "watchdog_" + started.strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
        jobs = self.registry.list()
        self.store.initialize()
        self.store.sync_job_definitions(jobs, observed_at=observed_at)

        combined = CollectionResult()
        errors: list[dict[str, str]] = []
        failed_adapters: set[str] = set()
        collectors: tuple[tuple[str, Collector], ...] = (
            ("market", collect_market_evidence),
            ("research", collect_research_evidence),
            ("delivery", collect_delivery_evidence),
            ("application", collect_application_evidence),
            ("broad", collect_broad_evidence),
        )
        for name, collector in collectors:
            try:
                combined.extend(collector(jobs, now=started, observed_at=observed_at))
            except Exception as exc:  # noqa: BLE001 - isolate evidence domains
                failed_adapters.update(_COLLECTOR_ADAPTERS[name])
                errors.append({"collector": name, "error": safe_text(exc) or type(exc).__name__})
                combined.incidents.append(IncidentCandidate(
                    fingerprint=f"watchdog:collector:{name}",
                    severity=IncidentSeverity.WARNING,
                    code="EVIDENCE_COLLECTOR_FAILED",
                    title=f"{name} 证据采集失败",
                    detail=safe_text(exc) or type(exc).__name__,
                    job_id="operations_watchdog",
                    metadata={"collector": name, "exception_type": type(exc).__name__},
                ))

        systemd_by_job = self.systemd.inspect(jobs)
        snapshots = {item.job_id: item for item in combined.snapshots}
        for job in jobs:
            if job.adapter == "watchdog_self":
                continue
            systemd = systemd_by_job.get(job.job_id, {})
            item = snapshots.get(job.job_id)
            if item is None:
                # Preserve the previous coherent snapshot while a source is
                # temporarily locked or unreadable.  The collector incident
                # exposes the outage without converting good evidence to MISS.
                if job.adapter in failed_adapters:
                    continue
                item = JobSnapshot(
                    job_id=job.job_id,
                    status=(JobStatus.DISABLED if not job.enabled_expected else JobStatus.UNKNOWN),
                    observed_at=observed_at,
                    stage="等待证据",
                    status_reason=(
                        "该任务按配置保持关闭"
                        if not job.enabled_expected else "尚未发现可识别的结构化运行证据"
                    ),
                )
            if systemd:
                item = replace(item, systemd=systemd)
                combined.incidents.extend(_systemd_incidents(job, systemd))
                if (
                    str(systemd.get("service_active")) == "active"
                    and str(systemd.get("service_substate")) in {"running", "start", "exited"}
                    and item.status not in {JobStatus.SUCCESS, JobStatus.FAILED}
                ):
                    item = replace(item, status=JobStatus.RUNNING)
            snapshots[job.job_id] = item

        resource_rows, resource_incidents = _resource_evidence(
            observed_at=observed_at,
            minimum_disk_gb=self.registry.settings.minimum_free_disk_gb,
            minimum_memory_mb=self.registry.settings.minimum_available_memory_mb,
        )
        combined.freshness.extend(resource_rows)
        combined.incidents.extend(resource_incidents)
        jobs_with_specific_incidents = {
            item.job_id for item in combined.incidents if item.job_id
        }
        for snapshot in snapshots.values():
            if snapshot.job_id in jobs_with_specific_incidents:
                continue
            incident = _status_incident(snapshot)
            if incident:
                combined.incidents.append(incident)

        watchdog_status = JobStatus.DEGRADED if errors else JobStatus.SUCCESS
        watchdog_systemd = systemd_by_job.get("operations_watchdog", {})
        snapshots["operations_watchdog"] = JobSnapshot(
            job_id="operations_watchdog",
            status=watchdog_status,
            observed_at=observed_at,
            run_id=run_id,
            stage="采集并发布只读快照",
            status_reason=(
                "所有证据适配器均完成"
                if not errors else f"{len(errors)} 个证据适配器降级"
            ),
            heartbeat_at=observed_at,
            last_success_at=observed_at if not errors else None,
            systemd=watchdog_systemd,
            metrics={
                "jobs_observed": len(snapshots),
                "collector_errors": errors,
                "external_notifications": False,
            },
        )
        combined.incidents.extend(_systemd_incidents(
            self.registry.get("operations_watchdog"), watchdog_systemd
        ))

        self.store.upsert_runs(combined.runs)
        self.store.upsert_snapshots(snapshots.values())
        self.store.upsert_freshness(combined.freshness)
        self.store.upsert_deliveries(combined.deliveries)
        self.store.upsert_projects(combined.projects)
        incidents_open = self.store.reconcile_incidents(
            combined.incidents,
            observed_at=observed_at,
            resolve_missing=not errors,
        )
        completed = datetime.now(timezone.utc)
        duration = max(0.0, time.perf_counter() - started_clock)
        self.store.record_watchdog_run(
            watchdog_run_id=run_id,
            status=watchdog_status.value,
            started_at=observed_at,
            completed_at=_iso(completed),
            duration_seconds=duration,
            jobs_observed=len(snapshots),
            incidents_open=incidents_open,
            error_summary="; ".join(item["error"] for item in errors) or None,
            metadata={
                "runs_collected": len(combined.runs),
                "freshness_collected": len(combined.freshness),
                "deliveries_collected": len(combined.deliveries),
                "projects_collected": len(combined.projects),
                "systemd_available": self.systemd.available,
            },
        )
        self.store.prune(retention_days=self.registry.settings.retention_days)
        snapshot_path = self.store.publish_snapshot()
        return {
            "watchdog_run_id": run_id,
            "status": watchdog_status.value,
            "observed_at": observed_at,
            "completed_at": _iso(completed),
            "duration_seconds": round(duration, 3),
            "jobs_observed": len(snapshots),
            "runs_collected": len(combined.runs),
            "freshness_collected": len(combined.freshness),
            "deliveries_collected": len(combined.deliveries),
            "projects_collected": len(combined.projects),
            "incidents_open": incidents_open,
            "collector_errors": errors,
            "snapshot_path": str(snapshot_path),
            "external_notifications": False,
        }


__all__ = ["OperationsWatchdog"]
