"""Evidence adapters for the application SQLite queue and paper accounts."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any, Iterable

from src.config import CONFIG, PROJECT_ROOT
from src.operations.evidence import (
    expected_target_session,
    iso_utc,
    parse_datetime,
    safe_text,
    schedule_bounds,
    sqlite_rows,
    sqlite_tables,
    stable_id,
    time_relative_status,
)
from src.operations.models import (
    CollectionResult,
    IncidentCandidate,
    IncidentSeverity,
    JobDefinition,
    JobSnapshot,
    JobStatus,
    OperationRun,
    RunStage,
)


def _app_db_path() -> Path:
    configured = Path(str(CONFIG.storage.sqlite_path))
    return configured if configured.is_absolute() else PROJECT_ROOT / configured


def _decode(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _collect_data_requests(
    job: JobDefinition,
    *,
    now: datetime,
    observed_at: str,
) -> CollectionResult:
    result = CollectionResult()
    path = _app_db_path()
    rows: list[dict[str, Any]] = []
    if "data_requests" in sqlite_tables(path):
        rows = sqlite_rows(
            path,
            "SELECT * FROM data_requests ORDER BY created_at DESC LIMIT 500",
        )
    counts: dict[str, int] = {}
    for row in rows:
        state = str(row.get("status") or "UNKNOWN").upper()
        counts[state] = counts.get(state, 0) + 1
    pending_limit = int(job.schedule.get("oldest_pending_minutes") or 20)
    running_limit = int(job.schedule.get("stale_running_minutes") or 30)
    old_pending: list[dict[str, Any]] = []
    stale_running: list[dict[str, Any]] = []
    for row in rows:
        state = str(row.get("status") or "").upper()
        reference = parse_datetime(row.get("started_at") or row.get("created_at"))
        age = (now - reference).total_seconds() / 60 if reference else 0
        if state in {"PENDING", "WAITING_FOR_DATA"} and age > pending_limit:
            old_pending.append(row)
        if state == "RUNNING" and age > running_limit:
            stale_running.append(row)
    if stale_running:
        status = JobStatus.STALE
        reason = f"{len(stale_running)} 个运行中请求超过 {running_limit} 分钟"
    elif old_pending:
        status = JobStatus.DEGRADED
        reason = f"{len(old_pending)} 个待处理请求超过 {pending_limit} 分钟"
    elif counts.get("RUNNING", 0):
        status = JobStatus.RUNNING
        reason = f"正在处理 {counts['RUNNING']} 个请求"
    elif counts.get("FAILED", 0):
        status = JobStatus.DEGRADED
        reason = f"队列空闲，历史仍有 {counts['FAILED']} 个失败请求"
    else:
        status = JobStatus.SUCCESS
        reason = "队列空闲且没有超时请求"
    if stale_running or old_pending:
        result.incidents.append(IncidentCandidate(
            fingerprint="data_requests:queue_lag",
            severity=(
                IncidentSeverity.CRITICAL if stale_running else IncidentSeverity.WARNING
            ),
            code="DATA_REQUEST_QUEUE_LAG",
            title="缺数队列处理延迟",
            detail=reason,
            job_id=job.job_id,
            metadata={
                "stale_running": len(stale_running),
                "old_pending": len(old_pending),
            },
        ))
    for row in rows:
        source_id = str(row.get("request_id") or "")
        state = str(row.get("status") or "UNKNOWN").upper()
        run_status = {
            "PENDING": JobStatus.SCHEDULED,
            "WAITING_FOR_DATA": JobStatus.BLOCKED,
            "RUNNING": JobStatus.RUNNING,
            "SUCCESS": JobStatus.SUCCESS,
            "FAILED": JobStatus.FAILED,
        }.get(state, JobStatus.UNKNOWN)
        result.runs.append(OperationRun(
            run_id=stable_id("run_", job.job_id, source_id),
            source_run_id=source_id,
            job_id=job.job_id,
            status=run_status,
            source="quant_app.data_requests",
            observed_at=observed_at,
            target_session=str((_decode(row.get("payload_json"))).get("target_session") or "") or None,
            stage=state,
            attempt=int(row.get("attempts") or 0) + 1,
            started_at=iso_utc(row.get("started_at") or row.get("created_at")),
            completed_at=iso_utc(row.get("finished_at")),
            error_summary=safe_text(row.get("error")),
            output_versions=_decode(row.get("result_json")),
            metadata={
                "data_universe": row.get("data_universe"),
                "request_key": row.get("request_key"),
                "updated_at": row.get("updated_at"),
            },
        ))
    latest = rows[0] if rows else None
    result.snapshots.append(JobSnapshot(
        job_id=job.job_id,
        status=status,
        observed_at=observed_at,
        run_id=(
            stable_id("run_", job.job_id, latest.get("request_id"))
            if latest else None
        ),
        stage="处理缺数队列" if counts.get("RUNNING") else "等待新请求",
        status_reason=reason,
        last_success_at=max(
            filter(None, [
                iso_utc(row.get("finished_at"))
                for row in rows
                if str(row.get("status") or "").upper() == "SUCCESS"
            ]),
            default=None,
        ),
        metrics={
            "status_counts": counts,
            "oldest_pending_count": len(old_pending),
            "stale_running_count": len(stale_running),
        },
    ))
    return result


def _paper_accounts(path: Path) -> list[dict[str, Any]]:
    if "app_records" not in sqlite_tables(path):
        return []
    rows = sqlite_rows(
        path,
        "SELECT record_id, payload_json, updated_at FROM app_records WHERE kind='paper_account' ORDER BY updated_at DESC",
    )
    accounts: list[dict[str, Any]] = []
    for row in rows:
        payload = _decode(row.get("payload_json"))
        payload["_record_id"] = row.get("record_id")
        payload["_updated_at"] = row.get("updated_at")
        accounts.append(payload)
    return accounts


def _paper_run_rows(path: Path) -> list[dict[str, Any]]:
    if "app_frame_rows" not in sqlite_tables(path):
        return []
    rows = sqlite_rows(
        path,
        "SELECT owner_id, ordinal, row_json FROM app_frame_rows WHERE owner_kind='paper_account' AND frame_name='runs' ORDER BY owner_id, ordinal DESC",
    )
    output: list[dict[str, Any]] = []
    for row in rows:
        payload = _decode(row.get("row_json"))
        payload["_owner_id"] = row.get("owner_id")
        output.append(payload)
    return output


def _collect_paper(
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
    path = _app_db_path()
    accounts = _paper_accounts(path)
    active = [
        account for account in accounts
        if str(account.get("status") or "").lower() == "active"
    ]
    run_rows = _paper_run_rows(path)
    latest_by_account: dict[str, dict[str, Any]] = {}
    for row in run_rows:
        account_id = str(row.get("account_id") or row.get("_owner_id") or "")
        current = latest_by_account.get(account_id)
        if current is None or str(row.get("run_at") or "") > str(current.get("run_at") or ""):
            latest_by_account[account_id] = row
        source_id = str(row.get("run_id") or f"{account_id}:{row.get('run_at')}")
        failed = bool(row.get("error"))
        result.runs.append(OperationRun(
            run_id=stable_id("run_", job.job_id, source_id),
            source_run_id=source_id,
            job_id=job.job_id,
            status=JobStatus.FAILED if failed else JobStatus.SUCCESS,
            source="quant_app.paper_runs",
            observed_at=observed_at,
            target_session=str(row.get("mark_date") or row.get("decision_date") or "") or None,
            stage="ACCOUNT_RUN",
            started_at=iso_utc(row.get("run_at")),
            completed_at=iso_utc(row.get("run_at")),
            rows_processed=int(row.get("fills_count") or 0),
            error_summary=safe_text(row.get("error")),
            input_versions={
                "dataset_version_id": row.get("dataset_version_id"),
                "research_publication_id": row.get("research_publication_id"),
            },
            metadata={
                "account_id": account_id,
                "decision_date": row.get("decision_date"),
                "mark_date": row.get("mark_date"),
                "fills_count": row.get("fills_count"),
                "orders_created": row.get("orders_created"),
                "pending_orders": row.get("pending_orders"),
                "equity": row.get("equity"),
            },
        ))
    stages: list[RunStage] = []
    current_count = 0
    failed_count = 0
    for order, account in enumerate(active, start=1):
        account_id = str(account.get("id") or account.get("_record_id") or "")
        run = latest_by_account.get(account_id)
        actual = str(account.get("last_mark_date") or (run or {}).get("mark_date") or "")
        failed = bool(account.get("last_error") or (run or {}).get("error"))
        if failed:
            stage_status = JobStatus.FAILED
            failed_count += 1
        elif actual == expected:
            stage_status = JobStatus.SUCCESS
            current_count += 1
        else:
            stage_status = JobStatus.STALE
        stages.append(RunStage(
            stage_name=str(account.get("name") or account_id),
            stage_order=order,
            status=stage_status,
            completed_at=iso_utc(account.get("last_run_at")),
            detail=f"最新净值日 {actual or '未知'}",
            metadata={
                "account_id": account_id,
                "last_equity": account.get("last_equity"),
                "universe": account.get("universe"),
            },
        ))
    if not active:
        status = JobStatus.SKIPPED
        reason = "没有启用中的模拟盘账户"
    elif failed_count:
        status = JobStatus.FAILED
        reason = f"{failed_count} 个启用账户最新运行失败"
    elif current_count == len(active):
        status = JobStatus.SUCCESS
        reason = f"{current_count} 个启用账户均已更新到 {expected}"
    else:
        timing = time_relative_status(
            now=now,
            scheduled_for=scheduled_for,
            deadline_at=deadline_at,
            has_older_evidence=bool(latest_by_account),
        )
        status = timing
        reason = f"{current_count}/{len(active)} 个启用账户已更新到目标交易日"
    aggregate_source = f"{expected}:" + ":".join(
        f"{account.get('id')}={account.get('last_mark_date')}"
        for account in active
    )
    aggregate_run_id = stable_id("run_", job.job_id, "aggregate", aggregate_source)
    if active:
        result.runs.append(OperationRun(
            run_id=aggregate_run_id,
            source_run_id=aggregate_source,
            job_id=job.job_id,
            status=status,
            source="quant_app.paper_accounts",
            observed_at=observed_at,
            target_session=expected,
            stage="ACCOUNT_AGGREGATE",
            completed_at=max(
                filter(None, [iso_utc(account.get("last_run_at")) for account in active]),
                default=None,
            ),
            progress_current=current_count,
            progress_total=len(active),
            error_summary=next(
                (safe_text(account.get("last_error")) for account in active if account.get("last_error")),
                None,
            ),
            stages=tuple(stages),
        ))
    result.snapshots.append(JobSnapshot(
        job_id=job.job_id,
        status=status,
        observed_at=observed_at,
        target_session=expected,
        run_id=aggregate_run_id if active else None,
        stage="逐账户模拟交易与记账",
        status_reason=reason,
        scheduled_for=scheduled_for,
        deadline_at=deadline_at,
        last_success_at=max(
            filter(None, [iso_utc(account.get("last_run_at")) for account in active]),
            default=None,
        ),
        progress_current=float(current_count),
        progress_total=float(len(active)),
        metrics={
            "accounts_total": len(accounts),
            "accounts_active": len(active),
            "accounts_current": current_count,
            "accounts_failed": failed_count,
        },
    ))
    return result


def collect_application_evidence(
    jobs: Iterable[JobDefinition],
    *,
    now: datetime,
    observed_at: str,
) -> CollectionResult:
    result = CollectionResult()
    for job in jobs:
        if job.adapter == "data_requests":
            result.extend(_collect_data_requests(job, now=now, observed_at=observed_at))
        elif job.adapter == "paper_trading":
            result.extend(_collect_paper(job, now=now, observed_at=observed_at))
    return result


__all__ = ["collect_application_evidence"]
