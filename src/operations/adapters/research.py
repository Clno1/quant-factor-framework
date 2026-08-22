"""Evidence adapters for factor research and group analytics publications."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from src.config import PROJECT_ROOT
from src.operations.evidence import (
    expected_target_session,
    iso_utc,
    load_json,
    safe_text,
    schedule_bounds,
    session_delay,
    stable_id,
    status_from_source,
    time_relative_status,
)
from src.operations.models import (
    CollectionResult,
    FreshnessObservation,
    JobDefinition,
    JobSnapshot,
    JobStatus,
    OperationRun,
    RunStage,
)


def _publication(universe: str) -> dict[str, Any] | None:
    return load_json(
        PROJECT_ROOT / "outputs" / "universes" / universe / "research_publication.json"
    )


def _factor_status(
    publication: dict[str, Any] | None,
    *,
    expected: str | None,
) -> JobStatus:
    if publication is None:
        return JobStatus.MISSED
    target = str((publication.get("data_foundation") or {}).get("target_session") or "")
    if publication.get("status") == "PUBLISHED" and target == expected:
        return JobStatus.SUCCESS
    return JobStatus.STALE


def _collect_factor_research(
    job: JobDefinition,
    *,
    now: datetime,
    observed_at: str,
) -> CollectionResult:
    result = CollectionResult()
    universes = [str(value).upper() for value in job.evidence.get("universes", [])]
    expected = expected_target_session(job, now=now)
    scheduled_for, deadline_at = schedule_bounds(
        job,
        now=now,
        target_session=expected,
    )
    statuses: list[JobStatus] = []
    stages: list[RunStage] = []
    publication_ids: dict[str, str] = {}
    published_times: list[str] = []
    factor_total = 0
    for order, universe in enumerate(universes, start=1):
        publication = _publication(universe)
        status = _factor_status(publication, expected=expected)
        statuses.append(status)
        foundation = (publication or {}).get("data_foundation") or {}
        actual = str(foundation.get("target_session") or "") or None
        publication_id = str((publication or {}).get("publication_id") or "")
        published_at = iso_utc((publication or {}).get("published_at"))
        factors = (publication or {}).get("factors") or {}
        factor_total += len(factors)
        if publication_id:
            publication_ids[universe] = publication_id
        if published_at:
            published_times.append(published_at)
        stages.append(RunStage(
            stage_name=universe,
            stage_order=order,
            status=status,
            completed_at=published_at,
            rows_processed=len(factors) if publication else None,
            detail=(
                f"{len(factors)} 个因子，数据截止 {actual}"
                if publication else "研究发布文件不存在"
            ),
            metadata={
                "publication_id": publication_id or None,
                "dataset_version_id": foundation.get("version_id"),
                "factor_count": len(factors),
            },
        ))
        result.freshness.append(FreshnessObservation(
            object_id=f"factor_research:{universe}",
            display_name=f"{universe} 多因子研究",
            category="RESEARCH",
            status=status,
            observed_at=observed_at,
            expected_session=expected,
            actual_session=actual,
            delay_sessions=session_delay(expected, actual),
            version_id=publication_id or None,
            row_count=int(foundation.get("row_count") or 0) if publication else None,
            item_count=len(factors) if publication else None,
            quality={
                "publication_status": (publication or {}).get("status"),
                "confidence_required": (publication or {}).get("confidence_required"),
            },
            source="research_publication.json",
        ))
        if publication:
            result.runs.append(OperationRun(
                run_id=stable_id("run_", job.job_id, universe, publication_id),
                source_run_id=publication_id or f"{universe}:{actual}",
                job_id=job.job_id,
                status=status,
                source="research_publication.json",
                observed_at=observed_at,
                target_session=actual,
                stage=universe,
                completed_at=published_at,
                rows_processed=len(factors),
                input_versions={
                    "dataset_version_id": foundation.get("version_id"),
                    "dataset_run_id": foundation.get("run_id"),
                },
                output_versions={"research_publication_id": publication_id},
                metadata={
                    "universe": universe,
                    "factor_count": len(factors),
                },
            ))

    all_current = bool(statuses) and all(value == JobStatus.SUCCESS for value in statuses)
    if all_current:
        status = JobStatus.SUCCESS
    else:
        timing = time_relative_status(
            now=now,
            scheduled_for=scheduled_for,
            deadline_at=deadline_at,
            has_older_evidence=bool(publication_ids),
        )
        status = (
            JobStatus.DEGRADED
            if timing != JobStatus.SCHEDULED
            and any(value == JobStatus.SUCCESS for value in statuses)
            else timing
        )
    aggregate_source = f"{expected}:" + ":".join(
        f"{key}={publication_ids.get(key, 'missing')}" for key in universes
    )
    aggregate_id = stable_id("run_", job.job_id, "aggregate", aggregate_source)
    if publication_ids:
        result.runs.append(OperationRun(
            run_id=aggregate_id,
            source_run_id=aggregate_source,
            job_id=job.job_id,
            status=status,
            source="factor_research_aggregate",
            observed_at=observed_at,
            target_session=expected,
            stage="RESEARCH_PUBLICATION",
            completed_at=max(published_times) if published_times else None,
            rows_processed=factor_total,
            output_versions=publication_ids,
            stages=tuple(stages),
        ))
    result.snapshots.append(JobSnapshot(
        job_id=job.job_id,
        status=status,
        observed_at=observed_at,
        target_session=expected,
        run_id=aggregate_id if publication_ids else None,
        stage="研究发布",
        status_reason=(
            "三个研究池均已发布目标交易日研究"
            if all_current
            else "存在缺失或落后于目标交易日的研究发布"
        ),
        scheduled_for=scheduled_for,
        deadline_at=deadline_at,
        last_success_at=max(published_times) if published_times else None,
        output_version=", ".join(
            f"{key}:{value[:8]}" for key, value in sorted(publication_ids.items())
        ) or None,
        metrics={
            "universes_current": sum(value == JobStatus.SUCCESS for value in statuses),
            "universes_expected": len(universes),
            "factor_bindings": factor_total,
        },
    ))
    return result


def _group_pointers() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root = PROJECT_ROOT / "outputs" / "universes"
    successes: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    for path in root.glob("*/group_analytics/*/*/eod/latest_success.json"):
        payload = load_json(path)
        if payload:
            payload["_path"] = str(path.relative_to(PROJECT_ROOT))
            successes.append(payload)
    for path in root.glob("*/group_analytics/*/*/eod/last_attempt.json"):
        payload = load_json(path)
        if payload:
            payload["_path"] = str(path.relative_to(PROJECT_ROOT))
            attempts.append(payload)
    return successes, attempts


def _collect_group_analytics(
    job: JobDefinition,
    *,
    now: datetime,
    observed_at: str,
) -> CollectionResult:
    result = CollectionResult()
    expected = expected_target_session(job, now=now)
    scheduled_for, deadline_at = schedule_bounds(
        job,
        now=now,
        target_session=expected,
    )
    successes, attempts = _group_pointers()
    expected_levels = {"sector", "sub_industry"}
    latest_by_level: dict[str, dict[str, Any]] = {}
    for row in successes:
        combination = row.get("combination") or {}
        if str(combination.get("universe") or "").upper() != "SP500":
            continue
        level = str(combination.get("level") or "")
        current = latest_by_level.get(level)
        if current is None or str(row.get("asof") or "") > str(current.get("asof") or ""):
            latest_by_level[level] = row
    statuses: list[JobStatus] = []
    stages: list[RunStage] = []
    for order, level in enumerate(sorted(expected_levels), start=1):
        row = latest_by_level.get(level)
        actual = str((row or {}).get("asof") or "") or None
        status = (
            JobStatus.SUCCESS
            if row and actual == expected else JobStatus.STALE if row else JobStatus.MISSED
        )
        statuses.append(status)
        stages.append(RunStage(
            stage_name=level,
            stage_order=order,
            status=status,
            started_at=iso_utc((row or {}).get("attempt_started_at")),
            completed_at=iso_utc((row or {}).get("published_at")),
            detail=(f"正式产物截止 {actual}" if row else "没有正式发布指针"),
            metadata={"run_id": (row or {}).get("run_id")},
        ))
    all_current = statuses and all(value == JobStatus.SUCCESS for value in statuses)
    if all_current:
        status = JobStatus.SUCCESS
    else:
        timing = time_relative_status(
            now=now,
            scheduled_for=scheduled_for,
            deadline_at=deadline_at,
            has_older_evidence=bool(latest_by_level),
        )
        status = (
            JobStatus.DEGRADED
            if timing != JobStatus.SCHEDULED
            and any(value == JobStatus.SUCCESS for value in statuses)
            else timing
        )
    recent_attempt = max(
        attempts,
        key=lambda item: str(item.get("started_at") or ""),
        default=None,
    )
    if recent_attempt and str(recent_attempt.get("last_attempt_status")) == "FAILED":
        attempt_asof = str(recent_attempt.get("asof") or "")
        if not all_current or attempt_asof == expected:
            status = JobStatus.FAILED
    source_id = ":".join(
        str((latest_by_level.get(level) or {}).get("run_id") or "missing")
        for level in sorted(expected_levels)
    )
    run_id = stable_id("run_", job.job_id, source_id)
    if latest_by_level or recent_attempt:
        result.runs.append(OperationRun(
            run_id=run_id,
            source_run_id=source_id,
            job_id=job.job_id,
            status=status,
            source="group_analytics_pointers",
            observed_at=observed_at,
            target_session=expected,
            stage="PUBLISH",
            started_at=iso_utc((recent_attempt or {}).get("started_at")),
            completed_at=iso_utc((recent_attempt or {}).get("finished_at")),
            error_code=(recent_attempt or {}).get("error_code"),
            error_summary=safe_text((recent_attempt or {}).get("error_summary")),
            output_versions={
                level: str(row.get("run_id") or "")
                for level, row in latest_by_level.items()
            },
            stages=tuple(stages),
        ))
    result.snapshots.append(JobSnapshot(
        job_id=job.job_id,
        status=status,
        observed_at=observed_at,
        target_session=expected,
        run_id=run_id if latest_by_level or recent_attempt else None,
        stage="板块和子行业发布",
        status_reason=(
            "板块和子行业均已发布目标交易日产物"
            if all_current else "板块或子行业产物尚未到目标交易日"
        ),
        scheduled_for=scheduled_for,
        deadline_at=deadline_at,
        metrics={
            "levels_current": sum(value == JobStatus.SUCCESS for value in statuses),
            "levels_expected": len(expected_levels),
        },
    ))
    return result


def collect_research_evidence(
    jobs: Iterable[JobDefinition],
    *,
    now: datetime,
    observed_at: str,
) -> CollectionResult:
    result = CollectionResult()
    for job in jobs:
        if job.adapter == "factor_research":
            result.extend(_collect_factor_research(job, now=now, observed_at=observed_at))
        elif job.adapter == "group_analytics":
            result.extend(_collect_group_analytics(job, now=now, observed_at=observed_at))
    return result


__all__ = ["collect_research_evidence"]
