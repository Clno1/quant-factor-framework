"""Operational evidence from the versioned DuckDB market-data catalog."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from src.config import CONFIG
from src.operations.evidence import (
    duration_seconds,
    expected_target_session,
    iso_utc,
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


def _catalog_rows(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not path.is_file():
        return [], []
    try:
        import duckdb

        connection = duckdb.connect(str(path), read_only=True)
        try:
            tables = {
                str(row[0])
                for row in connection.execute("SHOW TABLES").fetchall()
            }
            publications: list[dict[str, Any]] = []
            if {"published_versions", "dataset_versions"} <= tables:
                publications = [dict(zip(
                    [column[0] for column in connection.description],
                    row,
                )) for row in connection.execute("""
                    SELECT p.universe, p.version_id, p.published_at,
                           d.run_id, d.status, d.target_session, d.created_at,
                           d.row_count, d.ticker_count, d.min_date, d.max_date,
                           d.target_coverage
                    FROM published_versions p
                    JOIN dataset_versions d USING(version_id)
                    ORDER BY p.universe
                """).fetchall()]
            runs: list[dict[str, Any]] = []
            if "ingestion_runs" in tables:
                runs = [dict(zip(
                    [column[0] for column in connection.description],
                    row,
                )) for row in connection.execute("""
                    SELECT r.*, d.version_id, d.row_count, d.ticker_count
                    FROM ingestion_runs r
                    LEFT JOIN dataset_versions d USING(run_id)
                    ORDER BY r.started_at DESC
                    LIMIT 120
                """).fetchall()]
            return publications, runs
        finally:
            connection.close()
    except Exception as exc:
        raise RuntimeError("market-data catalog is temporarily unreadable") from exc


def _aggregate_status(
    statuses: list[JobStatus],
    *,
    now: datetime,
    scheduled_for: str | None,
    deadline_at: str | None,
    has_older_evidence: bool,
) -> JobStatus:
    if statuses and all(value == JobStatus.SUCCESS for value in statuses):
        return JobStatus.SUCCESS
    timing = time_relative_status(
        now=now,
        scheduled_for=scheduled_for,
        deadline_at=deadline_at,
        has_older_evidence=has_older_evidence,
    )
    if timing == JobStatus.SCHEDULED:
        return timing
    if any(value == JobStatus.SUCCESS for value in statuses):
        return JobStatus.DEGRADED
    return timing


def collect_market_evidence(
    jobs: Iterable[JobDefinition],
    *,
    now: datetime,
    observed_at: str,
) -> CollectionResult:
    selected = [job for job in jobs if job.adapter == "market_data"]
    result = CollectionResult()
    if not selected:
        return result
    path = CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path))
    publications, source_runs = _catalog_rows(path)
    published_by_universe = {
        str(row.get("universe") or "").upper(): row for row in publications
    }

    for job in selected:
        universes = [str(value).upper() for value in job.evidence.get("universes", [])]
        expected = expected_target_session(job, now=now)
        scheduled_for, deadline_at = schedule_bounds(
            job,
            now=now,
            target_session=expected,
        )
        stages: list[RunStage] = []
        statuses: list[JobStatus] = []
        versions: dict[str, str] = {}
        latest_success_at: list[str] = []
        for order, universe in enumerate(universes, start=1):
            row = published_by_universe.get(universe)
            actual = str(row.get("target_session")) if row else None
            current = bool(row and actual == expected and row.get("status") == "PUBLISHED")
            status = (
                JobStatus.SUCCESS
                if current
                else JobStatus.STALE if row else JobStatus.MISSED
            )
            statuses.append(status)
            if row:
                versions[universe] = str(row.get("version_id") or "")
                published_at = iso_utc(row.get("published_at"))
                if published_at:
                    latest_success_at.append(published_at)
            stages.append(RunStage(
                stage_name=universe,
                stage_order=order,
                status=status,
                completed_at=iso_utc(row.get("published_at")) if row else None,
                rows_processed=int(row.get("row_count") or 0) if row else None,
                detail=(
                    f"已发布 {actual}，版本 {str(row.get('version_id'))[:12]}"
                    if row else "尚无正式发布版本"
                ),
                metadata={
                    "expected_session": expected,
                    "actual_session": actual,
                    "ticker_count": int(row.get("ticker_count") or 0) if row else None,
                    "target_coverage": row.get("target_coverage") if row else None,
                },
            ))
            result.freshness.append(FreshnessObservation(
                object_id=f"market_data:{universe}",
                display_name=f"{universe} 行情版本",
                category="MARKET_DATA",
                status=status,
                observed_at=observed_at,
                expected_session=expected,
                actual_session=actual,
                delay_sessions=session_delay(expected, actual),
                version_id=str(row.get("version_id")) if row else None,
                row_count=int(row.get("row_count") or 0) if row else None,
                item_count=int(row.get("ticker_count") or 0) if row else None,
                quality={
                    "target_coverage": row.get("target_coverage") if row else None,
                    "dataset_status": row.get("status") if row else None,
                },
                source="duckdb.published_versions",
            ))

        status = _aggregate_status(
            statuses,
            now=now,
            scheduled_for=scheduled_for,
            deadline_at=deadline_at,
            has_older_evidence=bool(versions),
        )
        source_id = f"{expected}:" + ":".join(
            f"{key}={versions.get(key, 'missing')}" for key in universes
        )
        aggregate_run_id = stable_id("run_", job.job_id, source_id)
        if versions:
            result.runs.append(OperationRun(
                run_id=aggregate_run_id,
                source_run_id=source_id,
                job_id=job.job_id,
                status=status,
                source="duckdb.published_versions",
                observed_at=observed_at,
                target_session=expected,
                stage="PUBLISH",
                completed_at=max(latest_success_at) if latest_success_at else None,
                output_versions=versions,
                metadata={"universes": universes},
                stages=tuple(stages),
            ))
        result.snapshots.append(JobSnapshot(
            job_id=job.job_id,
            status=status,
            observed_at=observed_at,
            target_session=expected,
            run_id=aggregate_run_id if versions else None,
            stage=("已全部发布" if status == JobStatus.SUCCESS else "等待或核对行情发布"),
            status_reason=(
                "所有股票池均绑定到目标交易日的正式版本"
                if status == JobStatus.SUCCESS
                else "至少一个股票池尚未发布目标交易日版本"
            ),
            scheduled_for=scheduled_for,
            deadline_at=deadline_at,
            last_success_at=max(latest_success_at) if latest_success_at else None,
            output_version=", ".join(
                f"{key}:{value[:8]}" for key, value in sorted(versions.items())
            ) or None,
            metrics={
                "universes_expected": len(universes),
                "universes_current": sum(value == JobStatus.SUCCESS for value in statuses),
                "versions": versions,
            },
        ))

        allowed = set(universes)
        for source in source_runs:
            universe = str(source.get("universe") or "").upper()
            if universe not in allowed:
                continue
            source_run_id = str(source.get("run_id") or "")
            run_status = status_from_source(source.get("status"))
            result.runs.append(OperationRun(
                run_id=stable_id("run_", job.job_id, "ingestion", source_run_id),
                source_run_id=source_run_id,
                job_id=job.job_id,
                status=run_status,
                source="duckdb.ingestion_runs",
                observed_at=observed_at,
                target_session=str(source.get("target_session") or "") or None,
                stage=universe,
                started_at=iso_utc(source.get("started_at")),
                completed_at=iso_utc(source.get("finished_at")),
                duration_seconds=duration_seconds(
                    source.get("started_at"), source.get("finished_at")
                ),
                rows_processed=(
                    int(source.get("row_count"))
                    if source.get("row_count") is not None else None
                ),
                error_summary=safe_text(source.get("error_message")),
                output_versions={
                    universe: str(source.get("version_id") or "")
                } if source.get("version_id") else {},
                metadata={
                    "universe": universe,
                    "ticker_count": source.get("ticker_count"),
                    "provider": source.get("provider"),
                },
            ))
    return result


__all__ = ["collect_market_evidence"]
