"""Typed records shared by collectors, the watchdog and the read-only Web app."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    DEGRADED = "DEGRADED"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    MISSED = "MISSED"
    STALE = "STALE"
    DISABLED = "DISABLED"
    UNKNOWN = "UNKNOWN"


class IncidentSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class JobDefinition:
    job_id: str
    display_name: str
    category: str
    run_type: str
    adapter: str
    order: int
    enabled_expected: bool
    service_unit: str | None = None
    timer_unit: str | None = None
    schedule: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunStage:
    stage_name: str
    stage_order: int
    status: JobStatus
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None
    progress_current: float | None = None
    progress_total: float | None = None
    rows_processed: int | None = None
    detail: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


@dataclass(frozen=True)
class OperationRun:
    run_id: str
    source_run_id: str
    job_id: str
    status: JobStatus
    source: str
    observed_at: str
    target_session: str | None = None
    attempt: int = 1
    stage: str | None = None
    started_at: str | None = None
    heartbeat_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None
    progress_current: float | None = None
    progress_total: float | None = None
    rows_processed: int | None = None
    delivery_status: str | None = None
    error_code: str | None = None
    error_summary: str | None = None
    input_versions: dict[str, Any] = field(default_factory=dict)
    output_versions: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    stages: tuple[RunStage, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["stages"] = [stage.to_dict() for stage in self.stages]
        return payload


@dataclass(frozen=True)
class JobSnapshot:
    job_id: str
    status: JobStatus
    observed_at: str
    target_session: str | None = None
    run_id: str | None = None
    stage: str | None = None
    status_reason: str | None = None
    scheduled_for: str | None = None
    deadline_at: str | None = None
    last_success_at: str | None = None
    heartbeat_at: str | None = None
    progress_current: float | None = None
    progress_total: float | None = None
    output_version: str | None = None
    systemd: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


@dataclass(frozen=True)
class FreshnessObservation:
    object_id: str
    display_name: str
    category: str
    status: JobStatus
    observed_at: str
    expected_session: str | None = None
    actual_session: str | None = None
    delay_sessions: int | None = None
    version_id: str | None = None
    row_count: int | None = None
    item_count: int | None = None
    quality: dict[str, Any] = field(default_factory=dict)
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


@dataclass(frozen=True)
class DeliveryObservation:
    delivery_id: str
    job_id: str
    channel: str
    status: str
    observed_at: str
    target_session: str | None = None
    run_id: str | None = None
    attempts: int = 0
    sent_at: str | None = None
    message_id: str | None = None
    error_code: str | None = None
    error_summary: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProjectObservation:
    project_id: str
    display_name: str
    status: JobStatus
    observed_at: str
    summary: str
    stages: tuple[dict[str, Any], ...] = ()
    blockers: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["stages"] = list(self.stages)
        payload["blockers"] = list(self.blockers)
        return payload


@dataclass(frozen=True)
class IncidentCandidate:
    fingerprint: str
    severity: IncidentSeverity
    code: str
    title: str
    detail: str
    job_id: str | None = None
    target_session: str | None = None
    run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["severity"] = self.severity.value
        return payload


@dataclass
class CollectionResult:
    runs: list[OperationRun] = field(default_factory=list)
    snapshots: list[JobSnapshot] = field(default_factory=list)
    freshness: list[FreshnessObservation] = field(default_factory=list)
    deliveries: list[DeliveryObservation] = field(default_factory=list)
    projects: list[ProjectObservation] = field(default_factory=list)
    incidents: list[IncidentCandidate] = field(default_factory=list)

    def extend(self, other: "CollectionResult") -> None:
        self.runs.extend(other.runs)
        self.snapshots.extend(other.snapshots)
        self.freshness.extend(other.freshness)
        self.deliveries.extend(other.deliveries)
        self.projects.extend(other.projects)
        self.incidents.extend(other.incidents)


__all__ = [
    "CollectionResult",
    "DeliveryObservation",
    "FreshnessObservation",
    "IncidentCandidate",
    "IncidentSeverity",
    "JobDefinition",
    "JobSnapshot",
    "JobStatus",
    "OperationRun",
    "ProjectObservation",
    "RunStage",
]

