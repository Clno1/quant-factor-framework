"""Evidence adapters for premarket, hourly and continuous intraday jobs."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from statistics import mean
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from src.config import PROJECT_ROOT
from src.operations.evidence import (
    duration_seconds,
    expected_target_session,
    iso_utc,
    parse_datetime,
    safe_text,
    schedule_bounds,
    sqlite_rows,
    sqlite_tables,
    stable_id,
    status_from_source,
    time_relative_status,
)
from src.operations.models import (
    CollectionResult,
    DeliveryObservation,
    IncidentCandidate,
    IncidentSeverity,
    JobDefinition,
    JobSnapshot,
    JobStatus,
    OperationRun,
    RunStage,
)
from src.utils.market_calendar import is_xnys_session
from src.breakouts.live.session import previous_xnys_sessions


PREMARKET_DB = PROJECT_ROOT / "outputs" / "premarket_digest" / "state.sqlite3"
HOURLY_DB = PROJECT_ROOT / "outputs" / "momentum_alerts" / "state.sqlite3"
INTRADAY_DB = PROJECT_ROOT / "outputs" / "intraday_momentum_monitor" / "state.sqlite3"

_CHANNEL_LABELS = {
    "momentum": "盘前动量",
    "sector-rotation": "盘前板块轮动",
    "hourly-momentum": "每小时动量摘要",
    "intraday-breakout": "盘中动量突破",
    "cup-handle-breakout": "盘中茶杯柄突破",
}


def _p95(values: Iterable[float]) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * 0.95
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _delivery_state(status: JobStatus) -> str:
    return {
        JobStatus.SUCCESS: "SENT",
        JobStatus.SCHEDULED: "SCHEDULED",
        JobStatus.RUNNING: "PENDING",
        JobStatus.SKIPPED: "NO_SIGNAL",
        JobStatus.DISABLED: "DISABLED",
        JobStatus.MISSED: "MISSED",
        JobStatus.STALE: "STALE",
        JobStatus.FAILED: "FAILED",
        JobStatus.DEGRADED: "DEGRADED",
        JobStatus.BLOCKED: "BLOCKED",
    }.get(status, "UNKNOWN")


def _collect_premarket(
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
    rows: list[dict[str, Any]] = []
    if "deliveries" in sqlite_tables(PREMARKET_DB):
        rows = sqlite_rows(
            PREMARKET_DB,
            "SELECT * FROM deliveries ORDER BY target_session DESC, channel",
        )
    target_rows = [row for row in rows if str(row.get("target_session")) == expected]
    required = [str(value) for value in job.evidence.get("required_channels", [])]
    by_channel = {str(row.get("channel")): row for row in target_rows}
    deadline = parse_datetime(deadline_at)
    past_deadline = bool(deadline and now > deadline)
    statuses: list[JobStatus] = []
    stages: list[RunStage] = []
    sent_times: list[str] = []
    for order, channel in enumerate(required, start=1):
        row = by_channel.get(channel)
        source_status = str((row or {}).get("status") or "")
        status = status_from_source(source_status)
        if row is None:
            status = JobStatus.MISSED
        elif source_status == "FAILED":
            status = JobStatus.FAILED
        elif source_status in {"PENDING", "SENDING"}:
            status = JobStatus.MISSED if past_deadline else JobStatus.RUNNING
        statuses.append(status)
        sent_at = iso_utc((row or {}).get("sent_at"))
        if sent_at:
            sent_times.append(sent_at)
        stages.append(RunStage(
            stage_name=channel,
            stage_order=order,
            status=status,
            started_at=iso_utc((row or {}).get("created_at")),
            completed_at=sent_at,
            detail=(
                (
                    f"已超过投递截止时间，源状态仍为 {source_status}，"
                    f"尝试 {int(row.get('attempts') or 0)} 次"
                )
                if row and status == JobStatus.MISSED
                else (
                    f"投递状态 {source_status}，尝试 {int(row.get('attempts') or 0)} 次"
                    if row else "未找到当日投递记录"
                )
            ),
            metadata={
                "message_id": (row or {}).get("message_id"),
                "destination": (row or {}).get("destination"),
                "source_status": source_status or None,
                "past_deadline": past_deadline,
            },
        ))
    all_sent = bool(statuses) and all(value == JobStatus.SUCCESS for value in statuses)
    if all_sent:
        status = JobStatus.SUCCESS
    elif any(value == JobStatus.FAILED for value in statuses):
        status = JobStatus.FAILED
    elif past_deadline and any(value == JobStatus.MISSED for value in statuses):
        status = JobStatus.MISSED
    elif any(value == JobStatus.RUNNING for value in statuses):
        status = JobStatus.RUNNING
    else:
        timing = time_relative_status(
            now=now,
            scheduled_for=scheduled_for,
            deadline_at=deadline_at,
            has_older_evidence=bool(rows),
        )
        status = (
            JobStatus.DEGRADED
            if timing != JobStatus.SCHEDULED
            and any(value == JobStatus.SUCCESS for value in statuses)
            else timing
        )
    run_id = stable_id("run_", job.job_id, expected)
    for channel, stage in zip(required, stages, strict=True):
        row = by_channel.get(channel)
        result.deliveries.append(DeliveryObservation(
            delivery_id=stable_id("delivery_", job.job_id, expected, channel),
            job_id=job.job_id,
            channel=_CHANNEL_LABELS.get(channel, channel),
            status=(
                _delivery_state(stage.status)
                if row and stage.status == JobStatus.MISSED
                else str(row.get("status"))
                if row
                else (
                    "SCHEDULED"
                    if status == JobStatus.SCHEDULED else "MISSED"
                )
            ),
            observed_at=observed_at,
            target_session=expected,
            run_id=run_id if target_rows else None,
            attempts=int((row or {}).get("attempts") or 0),
            sent_at=iso_utc((row or {}).get("sent_at")),
            message_id=(row or {}).get("message_id"),
            error_code=(row or {}).get("last_error_code"),
            error_summary=(
                safe_text((row or {}).get("last_error"))
                or (stage.detail if row is None and status not in {JobStatus.SCHEDULED} else None)
            ),
            metadata={
                "channel_id": channel,
                "destination": (row or {}).get("destination"),
                "source_session": (row or {}).get("source_session"),
                "source_status": str((row or {}).get("status") or "") or None,
                "past_deadline": past_deadline,
                "expected": True,
            },
        ))
    if target_rows:
        result.runs.append(OperationRun(
            run_id=run_id,
            source_run_id=expected or "unknown",
            job_id=job.job_id,
            status=status,
            source="premarket_digest.deliveries",
            observed_at=observed_at,
            target_session=expected,
            stage="DELIVERY",
            started_at=min(
                filter(None, [iso_utc(row.get("created_at")) for row in target_rows]),
                default=None,
            ),
            completed_at=max(sent_times) if sent_times else None,
            delivery_status=("SENT" if all_sent else status.value),
            error_code=next(
                (str(row.get("last_error_code")) for row in target_rows if row.get("last_error_code")),
                None,
            ),
            error_summary=next(
                (safe_text(row.get("last_error")) for row in target_rows if row.get("last_error")),
                "投递窗口结束后仍有业务频道未发送"
                if status == JobStatus.MISSED else None,
            ),
            metadata={
                "channels": required,
                "past_deadline": past_deadline,
                "source_statuses": {
                    channel: str((by_channel.get(channel) or {}).get("status") or "")
                    for channel in required
                },
            },
            stages=tuple(stages),
        ))
    result.snapshots.append(JobSnapshot(
        job_id=job.job_id,
        status=status,
        observed_at=observed_at,
        target_session=expected,
        run_id=run_id if target_rows else None,
        stage="盘前生成与投递",
        status_reason=(
            "动量与板块轮动两个频道均已发送"
            if all_sent
            else (
                "投递窗口已结束，存在未发送的业务频道"
                if status == JobStatus.MISSED
                else "当日两个业务频道尚未全部完成投递"
            )
        ),
        scheduled_for=scheduled_for,
        deadline_at=deadline_at,
        last_success_at=max(sent_times) if sent_times else None,
        metrics={
            "channels_required": len(required),
            "channels_sent": sum(value == JobStatus.SUCCESS for value in statuses),
            "past_deadline": past_deadline,
        },
    ))
    return result


def _collect_premarket_prepare(
    job: JobDefinition,
    *,
    now: datetime,
    observed_at: str,
) -> CollectionResult:
    """Report payload preparation independently from the later delivery state."""
    result = CollectionResult()
    expected = expected_target_session(job, now=now)
    scheduled_for, deadline_at = schedule_bounds(
        job,
        now=now,
        target_session=expected,
    )
    rows = (
        sqlite_rows(
            PREMARKET_DB,
            "SELECT * FROM deliveries ORDER BY target_session DESC, channel",
        )
        if "deliveries" in sqlite_tables(PREMARKET_DB)
        else []
    )
    target_rows = [row for row in rows if str(row.get("target_session")) == expected]
    required = [str(value) for value in job.evidence.get("required_channels", [])]
    by_channel = {str(row.get("channel")): row for row in target_rows}
    prepared = [channel for channel in required if by_channel.get(channel) is not None]
    completed_at = max(
        filter(None, [iso_utc(row.get("created_at")) for row in target_rows]),
        default=None,
    )
    completed = parse_datetime(completed_at)
    deadline = parse_datetime(deadline_at)
    prepared_late = bool(completed and deadline and completed > deadline)
    if required and len(prepared) == len(required):
        if prepared_late:
            status = JobStatus.DEGRADED
            reason = "两个盘前频道的 payload 已生成，但晚于预计算截止时间"
        else:
            status = JobStatus.SUCCESS
            reason = "两个盘前频道的不可变 payload 均已提前冻结"
    else:
        status = time_relative_status(
            now=now,
            scheduled_for=scheduled_for,
            deadline_at=deadline_at,
            has_older_evidence=bool(rows),
        )
        reason = (
            "等待盘前摘要预计算时间"
            if status == JobStatus.SCHEDULED
            else f"当日仅准备 {len(prepared)}/{len(required)} 个频道"
        )
    run_id = stable_id("run_", job.job_id, expected)
    if target_rows:
        result.runs.append(OperationRun(
            run_id=run_id,
            source_run_id=expected,
            job_id=job.job_id,
            status=status,
            source="premarket_digest.deliveries",
            observed_at=observed_at,
            target_session=expected,
            stage="PAYLOADS_PREPARED",
            started_at=min(
                filter(None, [iso_utc(row.get("created_at")) for row in target_rows]),
                default=None,
            ),
            completed_at=completed_at,
            rows_processed=len(prepared),
            metadata={
                "required_channels": required,
                "prepared_channels": prepared,
                "delivery_states": {
                    channel: str((by_channel.get(channel) or {}).get("status") or "")
                    for channel in required
                },
                "prepared_late": prepared_late,
            },
        ))
    result.snapshots.append(JobSnapshot(
        job_id=job.job_id,
        status=status,
        observed_at=observed_at,
        target_session=expected,
        run_id=run_id if target_rows else None,
        stage="盘前摘要预计算",
        status_reason=reason,
        scheduled_for=scheduled_for,
        deadline_at=deadline_at,
        last_success_at=completed_at if len(prepared) == len(required) else None,
        progress_current=float(len(prepared)),
        progress_total=float(len(required)),
        metrics={
            "channels_required": len(required),
            "channels_prepared": len(prepared),
            "prepared_late": prepared_late,
        },
    ))
    return result


def _xnys_phase(now: datetime) -> tuple[str, datetime | None, datetime | None]:
    local = now.astimezone(ZoneInfo("America/New_York"))
    session = local.date().isoformat()
    if local.weekday() >= 5 or not is_xnys_session(session):
        return "CLOSED", None, None
    import exchange_calendars as xcals
    import pandas as pd

    calendar = xcals.get_calendar(
        "XNYS",
        start=(local.date() - timedelta(days=1)).isoformat(),
        end=(local.date() + timedelta(days=1)).isoformat(),
    )
    label = pd.Timestamp(session)
    opened = pd.Timestamp(calendar.session_open(label)).tz_convert(
        "America/New_York"
    ).to_pydatetime()
    closed = pd.Timestamp(calendar.session_close(label)).tz_convert(
        "America/New_York"
    ).to_pydatetime()
    monitor_start = opened - timedelta(minutes=10)
    monitor_end = closed + timedelta(minutes=5)
    if local < monitor_start:
        return "BEFORE", opened, closed
    if local <= monitor_end:
        return "OPEN", opened, closed
    return "AFTER", opened, closed


def _hourly_expected_hours(now: datetime) -> set[int]:
    local = now.astimezone(ZoneInfo("America/New_York"))
    phase, _, closed = _xnys_phase(now)
    if phase == "CLOSED" or closed is None:
        return set()
    triggers = [
        local.replace(hour=hour, minute=35, second=0, microsecond=0)
        for hour in range(10, 16)
    ]
    return {
        trigger.hour
        for trigger in triggers
        if trigger <= local and trigger <= closed
    }


def _hourly_slot(row: dict[str, Any], *, expected: str | None) -> int | None:
    started = parse_datetime(row.get("started_at"))
    if started is None:
        return None
    local = started.astimezone(ZoneInfo("America/New_York"))
    if local.date().isoformat() != expected or not 10 <= local.hour <= 15:
        return None
    return local.hour


def _collect_hourly(
    job: JobDefinition,
    *,
    now: datetime,
    observed_at: str,
) -> CollectionResult:
    result = CollectionResult()
    expected = expected_target_session(job, now=now)
    rows: list[dict[str, Any]] = []
    if "alert_runs" in sqlite_tables(HOURLY_DB):
        rows = sqlite_rows(
            HOURLY_DB,
            "SELECT * FROM alert_runs ORDER BY started_at DESC LIMIT 100",
        )
    today = [
        row for row in rows
        if str(row.get("session_date") or "") == expected
        or (
            not row.get("session_date")
            and _hourly_slot(row, expected=expected) is not None
        )
    ]
    expected_hours = _hourly_expected_hours(now)
    expected_count = len(expected_hours)
    completed = [row for row in today if str(row.get("status") or "").lower() in {"completed", "success"}]
    failed = [row for row in today if str(row.get("status") or "").lower() in {"failed", "error"}]
    running = [row for row in today if str(row.get("status") or "").lower() == "running"]
    completed_hours = {
        slot for row in completed
        if (slot := _hourly_slot(row, expected=expected)) in expected_hours
    }
    failed_hours = {
        slot for row in failed
        if (slot := _hourly_slot(row, expected=expected)) in expected_hours
    }
    uncovered_failed_hours = failed_hours - completed_hours
    if running:
        status = JobStatus.RUNNING
    elif uncovered_failed_hours and len(completed_hours) < expected_count:
        status = JobStatus.FAILED
    elif len(completed_hours) >= expected_count and expected_count > 0:
        status = JobStatus.SUCCESS
    elif expected_count == 0:
        status = JobStatus.SCHEDULED
    elif completed_hours:
        status = JobStatus.DEGRADED
    else:
        status = JobStatus.MISSED
    latest = today[0] if today else None
    for row in rows:
        source_id = str(row.get("id") or "")
        source_status = str(row.get("status") or "").lower()
        run_status = (
            JobStatus.RUNNING if source_status == "running"
            else JobStatus.FAILED if source_status in {"failed", "error"}
            else JobStatus.SUCCESS if source_status in {"completed", "success"}
            else status_from_source(source_status)
        )
        result.runs.append(OperationRun(
            run_id=stable_id("run_", job.job_id, source_id),
            source_run_id=source_id,
            job_id=job.job_id,
            status=run_status,
            source="momentum_alerts.alert_runs",
            observed_at=observed_at,
            target_session=str(row.get("session_date") or "") or None,
            stage="SCAN_AND_DELIVER",
            started_at=iso_utc(row.get("started_at")),
            completed_at=iso_utc(row.get("completed_at")),
            duration_seconds=duration_seconds(row.get("started_at"), row.get("completed_at")),
            progress_current=row.get("strict_count"),
            progress_total=row.get("broad_count"),
            delivery_status=row.get("delivery_status"),
            error_summary=safe_text(row.get("error")),
            metadata={
                "mode": row.get("mode"),
                "market_open": bool(row.get("market_open")) if row.get("market_open") is not None else None,
                "broad_count": row.get("broad_count"),
                "strict_count": row.get("strict_count"),
                "pending_count": row.get("pending_count"),
            },
        ))
    last_success_at = max(
        filter(None, [iso_utc(row.get("completed_at")) for row in completed]),
        default=None,
    )
    latest_delivery = str((latest or {}).get("delivery_status") or "")
    if not today:
        delivery_status = _delivery_state(status)
        delivery_detail = (
            "等待当日第一个小时扫描"
            if status == JobStatus.SCHEDULED else "当日没有小时扫描运行证据"
        )
    elif latest_delivery.startswith("discord_http_2"):
        delivery_status = "SENT"
        delivery_detail = latest_delivery
    elif latest_delivery == "skipped_empty":
        delivery_status = "NO_SIGNAL"
        delivery_detail = "扫描成功，但本次没有需要发送的候选"
    elif latest_delivery == "dry_run":
        delivery_status = "DRY_RUN"
        delivery_detail = "扫描成功，发送开关未开启"
    elif latest_delivery in {"failed", "UNKNOWN"}:
        delivery_status = "FAILED"
        delivery_detail = safe_text((latest or {}).get("error")) or latest_delivery
    elif latest_delivery:
        delivery_status = latest_delivery.upper()
        delivery_detail = latest_delivery
    else:
        delivery_status = _delivery_state(status)
        delivery_detail = "已有扫描记录，但没有结构化发送结论"
    result.deliveries.append(DeliveryObservation(
        delivery_id=stable_id("delivery_", job.job_id, expected, "hourly-momentum"),
        job_id=job.job_id,
        channel=_CHANNEL_LABELS["hourly-momentum"],
        status=delivery_status,
        observed_at=observed_at,
        target_session=expected,
        run_id=(
            stable_id("run_", job.job_id, latest.get("id"))
            if today and latest else None
        ),
        attempts=len(today),
        sent_at=(
            iso_utc((latest or {}).get("completed_at"))
            if delivery_status == "SENT" else None
        ),
        error_summary=delivery_detail,
        metadata={
            "expected_runs_so_far": expected_count,
            "completed_runs": len(completed),
            "latest_candidates": (latest or {}).get("strict_count"),
        },
    ))
    result.snapshots.append(JobSnapshot(
        job_id=job.job_id,
        status=status,
        observed_at=observed_at,
        target_session=expected,
        run_id=(stable_id("run_", job.job_id, latest.get("id")) if latest else None),
        stage=("持续小时扫描" if expected_count < 6 else "当日扫描结束"),
        status_reason=(
            f"当日应完成 {expected_count} 个小时，已完成 {len(completed_hours)} 个，失败 {len(uncovered_failed_hours)} 个"
        ),
        last_success_at=last_success_at,
        metrics={
            "runs_expected_so_far": expected_count,
            "runs_completed": len(completed_hours),
            "run_attempts_completed": len(completed),
            "runs_failed": len(uncovered_failed_hours),
            "latest_candidates": (latest or {}).get("strict_count"),
            "latest_delivery": (latest or {}).get("delivery_status"),
        },
    ))
    return result


def _parse_payload(row: dict[str, Any] | None) -> dict[str, Any]:
    try:
        value = json.loads(str((row or {}).get("payload_json") or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _collect_intraday_candidate_prepare(
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
    rows = (
        sqlite_rows(
            INTRADAY_DB,
            """SELECT * FROM candidate_snapshots
               ORDER BY session_date DESC, created_at DESC LIMIT 20""",
        )
        if "candidate_snapshots" in sqlite_tables(INTRADAY_DB)
        else []
    )
    row = next(
        (item for item in rows if str(item.get("session_date") or "") == expected),
        None,
    )
    payload = _parse_payload(row)
    completed_at = iso_utc((row or {}).get("created_at"))
    if row is not None:
        status = JobStatus.SUCCESS
        reason = "当日宽基动量候选快照已提前生成"
    else:
        status = time_relative_status(
            now=now,
            scheduled_for=scheduled_for,
            deadline_at=deadline_at,
            has_older_evidence=bool(rows),
        )
        reason = (
            "等待盘前候选预计算时间"
            if status == JobStatus.SCHEDULED
            else "当前交易日尚未生成候选快照"
        )
    run_id = stable_id("run_", job.job_id, expected)
    contract = payload.get("data_contract") or {}
    if row is not None:
        result.runs.append(OperationRun(
            run_id=run_id,
            source_run_id=expected,
            job_id=job.job_id,
            status=status,
            source="intraday_momentum_monitor.candidate_snapshots",
            observed_at=observed_at,
            target_session=expected,
            stage="CANDIDATES_PREPARED",
            started_at=completed_at,
            completed_at=completed_at,
            rows_processed=int(payload.get("candidate_count") or 0),
            input_versions={
                "data_universe": contract.get("data_universe"),
                "dataset_version_id": contract.get("dataset_version_id"),
                "bars_sha256": contract.get("bars_sha256"),
            },
            metadata={
                "source_data_date": payload.get("source_data_date"),
                "candidate_count": payload.get("candidate_count"),
            },
        ))
    result.snapshots.append(JobSnapshot(
        job_id=job.job_id,
        status=status,
        observed_at=observed_at,
        target_session=expected,
        run_id=run_id if row is not None else None,
        stage="盘中动量候选预计算",
        status_reason=reason,
        scheduled_for=scheduled_for,
        deadline_at=deadline_at,
        last_success_at=completed_at,
        progress_current=(1.0 if row is not None else 0.0),
        progress_total=1.0,
        metrics={
            "source_data_date": payload.get("source_data_date"),
            "candidate_count": payload.get("candidate_count"),
            "data_universe": contract.get("data_universe"),
            "dataset_version_id": contract.get("dataset_version_id"),
        },
    ))
    return result


def _collect_intraday(
    job: JobDefinition,
    *,
    now: datetime,
    observed_at: str,
) -> CollectionResult:
    result = CollectionResult()
    expected = expected_target_session(job, now=now)
    tables = sqlite_tables(INTRADAY_DB)
    heartbeat_rows = (
        sqlite_rows(INTRADAY_DB, "SELECT * FROM heartbeat WHERE singleton=1")
        if "heartbeat" in tables else []
    )
    heartbeat_row = heartbeat_rows[0] if heartbeat_rows else None
    heartbeat = _parse_payload(heartbeat_row)
    heartbeat_at = iso_utc(
        heartbeat.get("updated_at") or (heartbeat_row or {}).get("updated_at")
    )
    heartbeat_time = parse_datetime(heartbeat_at)
    age_seconds = (
        max(0.0, (now - heartbeat_time).total_seconds())
        if heartbeat_time else None
    )
    market_phase, _, _ = _xnys_phase(now)
    in_window = market_phase == "OPEN"
    heartbeat_session = str(heartbeat.get("session_date") or "")
    observations: list[dict[str, Any]] = []
    if "session_observations" in tables:
        observations = sqlite_rows(
            INTRADAY_DB,
            "SELECT * FROM session_observations ORDER BY session_date DESC LIMIT 20",
        )
    current_observation = next(
        (
            row for row in observations
            if str(row.get("session_date") or "") == expected
        ),
        None,
    )
    cup_observations: list[dict[str, Any]] = []
    if "cup_handle_session_observations" in tables:
        cup_observations = sqlite_rows(
            INTRADAY_DB,
            """SELECT * FROM cup_handle_session_observations
               ORDER BY session_date DESC LIMIT 20""",
        )
    current_cup_observation = next(
        (
            row for row in cup_observations
            if str(row.get("session_date") or "") == expected
        ),
        None,
    )
    timeout = int(job.schedule.get("heartbeat_timeout_seconds") or 120)
    if not heartbeat:
        status = (
            JobStatus.MISSED
            if market_phase in {"OPEN", "AFTER"} else JobStatus.SCHEDULED
        )
        reason = (
            "当前交易日没有心跳记录"
            if status == JobStatus.MISSED else "等待下一个交易时段启动"
        )
    elif heartbeat_session != expected:
        status = (
            JobStatus.STALE
            if market_phase in {"OPEN", "AFTER"} else JobStatus.SCHEDULED
        )
        reason = (
            f"最新心跳属于 {heartbeat_session or '未知交易日'}，当前应为 {expected}"
            if status == JobStatus.STALE else "等待下一个交易时段启动"
        )
    elif in_window and (age_seconds is None or age_seconds > timeout):
        status = JobStatus.STALE
        reason = f"盘中心跳已超过 {timeout} 秒"
        result.incidents.append(IncidentCandidate(
            fingerprint="intraday_momentum:stale_heartbeat",
            severity=IncidentSeverity.CRITICAL,
            code="STALE_HEARTBEAT",
            title="盘中动量监控心跳中断",
            detail=reason,
            job_id=job.job_id,
            target_session=expected,
            metadata={"heartbeat_age_seconds": age_seconds, "timeout_seconds": timeout},
        ))
    elif heartbeat.get("errors"):
        status = JobStatus.DEGRADED
        reason = f"最近周期记录 {len(heartbeat.get('errors') or [])} 个错误"
    elif in_window:
        status = JobStatus.RUNNING
        reason = "服务心跳正常，正在盘中持续计算"
    elif market_phase == "AFTER":
        observation_status = str((current_observation or {}).get("status") or "")
        if observation_status == "PASS":
            status = JobStatus.SUCCESS
            reason = (
                "当日盘中监控完整性检查通过，"
                f"覆盖率 {float(current_observation.get('cycle_coverage') or 0):.1%}"
            )
        elif current_observation:
            status = JobStatus.DEGRADED
            reason = "当日盘中监控完整性检查未通过"
        else:
            status = JobStatus.STALE
            reason = "收盘后仍没有当日盘中监控完整性记录"
    else:
        status = JobStatus.SCHEDULED
        reason = "等待下一个交易时段启动"

    cup_heartbeat = heartbeat.get("cup_handle") or {}
    if (
        int(cup_heartbeat.get("error_count") or 0) > 0
        and status in {JobStatus.RUNNING, JobStatus.SUCCESS}
    ):
        status = JobStatus.DEGRADED
        reason = "茶杯柄检测最近周期存在错误"
    if (
        market_phase == "AFTER"
        and current_cup_observation
        and str(current_cup_observation.get("status") or "") != "PASS"
        and status == JobStatus.SUCCESS
    ):
        status = JobStatus.DEGRADED
        reason = "盘中动量通过，但茶杯柄独立影子验收未通过"

    cycles: list[dict[str, Any]] = []
    if "monitor_cycles" in tables:
        cycles = sqlite_rows(
            INTRADAY_DB,
            "SELECT * FROM monitor_cycles WHERE session_date=? ORDER BY observed_at DESC LIMIT 500",
            (expected,),
        )
    cup_cycles: list[dict[str, Any]] = []
    cup_evaluations: list[dict[str, Any]] = []
    if "cup_handle_cycles" in tables:
        cup_cycles = sqlite_rows(
            INTRADAY_DB,
            """SELECT * FROM cup_handle_cycles
               WHERE session_date=? ORDER BY observed_at DESC LIMIT 500""",
            (expected,),
        )
    if "cup_handle_evaluations" in tables:
        cup_evaluations = sqlite_rows(
            INTRADAY_DB,
            """SELECT outcome, rejection_reason, latency_ms, bar_count
               FROM cup_handle_evaluations WHERE session_date=?""",
            (expected,),
        )
    cup_outcome_counts: dict[str, int] = {}
    cup_rejection_counts: dict[str, int] = {}
    for row in cup_evaluations:
        outcome = str(row.get("outcome") or "ERROR")
        cup_outcome_counts[outcome] = cup_outcome_counts.get(outcome, 0) + 1
        if outcome != "MATCH":
            rejection = str(row.get("rejection_reason") or "UNKNOWN")
            cup_rejection_counts[rejection] = cup_rejection_counts.get(rejection, 0) + 1
    cup_latency_p95 = _p95(
        float(row.get("latency_ms") or 0.0) for row in cup_evaluations
    )
    cup_algorithm_version = str(cup_heartbeat.get("algorithm_version") or "")
    cup_observations_for_version = [
        row for row in cup_observations
        if not cup_algorithm_version
        or str(row.get("algorithm_version") or "") == cup_algorithm_version
    ]
    cup_expected_shadow = previous_xnys_sessions(expected, 5)
    cup_by_session = {
        str(row.get("session_date") or ""): row
        for row in cup_observations_for_version
    }
    cup_shadow_passed = sum(
        str((cup_by_session.get(session) or {}).get("status") or "") == "PASS"
        for session in cup_expected_shadow
    )
    sent_count = 0
    failed_count = 0
    outbox_rows: list[dict[str, Any]] = []
    if "signal_outbox" in tables:
        counts = sqlite_rows(
            INTRADAY_DB,
            "SELECT status, COUNT(*) AS count FROM signal_outbox WHERE session_date=? GROUP BY status",
            (expected,),
        )
        by_state = {str(row["status"]): int(row["count"]) for row in counts}
        sent_count = by_state.get("SENT", 0)
        failed_count = by_state.get("FAILED", 0)
        outbox_rows = sqlite_rows(
            INTRADAY_DB,
            """SELECT session_date, ticker, trigger_family, status, attempts,
                      sent_at, message_id, last_error_code, last_error
               FROM signal_outbox
               WHERE session_date=?
               ORDER BY COALESCE(sent_at, updated_at, created_at) DESC
               LIMIT 200""",
            (expected,),
        )
    cycle_durations = [float(row["cycle_seconds"]) for row in cycles if row.get("cycle_seconds") is not None]
    source_id = f"{expected}:{heartbeat_at or 'missing'}"
    run_id = stable_id("run_", job.job_id, source_id)
    if heartbeat:
        result.runs.append(OperationRun(
            run_id=run_id,
            source_run_id=source_id,
            job_id=job.job_id,
            status=status,
            source="intraday_momentum_monitor.state",
            observed_at=observed_at,
            target_session=str(heartbeat.get("session_date") or expected),
            stage=str(heartbeat.get("phase") or "UNKNOWN").upper(),
            started_at=iso_utc(heartbeat.get("started_at")),
            heartbeat_at=heartbeat_at,
            progress_current=len(cycles),
            rows_processed=len(cycles),
            delivery_status=("FAILED" if failed_count else "SENT" if sent_count else "NO_SIGNAL"),
            error_summary=safe_text("; ".join(map(str, heartbeat.get("errors") or []))),
            input_versions={
                "data_universe": heartbeat.get("data_universe"),
                "dataset_version_id": heartbeat.get("dataset_version_id"),
                "bars_sha256": heartbeat.get("bars_sha256"),
            },
            metadata={
                "mode": heartbeat.get("mode"),
                "feed": heartbeat.get("feed"),
                "candidate_count": heartbeat.get("candidate_count"),
                "active_count": heartbeat.get("active_count"),
                "cycle_count": len(cycles),
                "signal_sent_count": sent_count,
                "cup_handle": {
                    "algorithm_version": cup_algorithm_version,
                    "mode": cup_heartbeat.get("mode"),
                    "outcome_counts": cup_outcome_counts,
                    "detection_p95_ms": cup_latency_p95,
                },
            },
        ))
    for row in outbox_rows:
        result.deliveries.append(DeliveryObservation(
            delivery_id=stable_id(
                "delivery_",
                job.job_id,
                expected,
                row.get("ticker"),
                row.get("trigger_family"),
            ),
            job_id=job.job_id,
            channel=(
                f"{_CHANNEL_LABELS['cup-handle-breakout']} · {row.get('ticker')}"
                if str(row.get("trigger_family") or "") == "CUP_HANDLE_BREAKOUT"
                else f"{_CHANNEL_LABELS['intraday-breakout']} · {row.get('ticker')}"
            ),
            status=str(row.get("status") or "UNKNOWN"),
            observed_at=observed_at,
            target_session=expected,
            run_id=run_id,
            attempts=int(row.get("attempts") or 0),
            sent_at=iso_utc(row.get("sent_at")),
            message_id=row.get("message_id"),
            error_code=row.get("last_error_code"),
            error_summary=safe_text(row.get("last_error")),
            metadata={
                "ticker": row.get("ticker"),
                "trigger_family": row.get("trigger_family"),
            },
        ))
    if not outbox_rows:
        if not heartbeat:
            delivery_status = _delivery_state(status)
            delivery_detail = "尚未发现盘中监控心跳"
        elif str(heartbeat.get("mode") or "").lower() != "live":
            delivery_status = "SHADOW"
            delivery_detail = "监控正在影子模式运行，不发送业务消息"
        elif not in_window:
            delivery_status = "NO_SIGNAL"
            delivery_detail = "当日尚无需要投递的盘中突破信号"
        else:
            delivery_status = "NO_SIGNAL"
            delivery_detail = "当前尚无需要投递的盘中突破信号"
        result.deliveries.append(DeliveryObservation(
            delivery_id=stable_id(
                "delivery_", job.job_id, expected, "intraday-breakout-summary"
            ),
            job_id=job.job_id,
            channel=_CHANNEL_LABELS["intraday-breakout"],
            status=delivery_status,
            observed_at=observed_at,
            target_session=expected,
            run_id=run_id if heartbeat else None,
            attempts=0,
            error_summary=delivery_detail,
            metadata={
                "expected": True,
                "monitor_mode": heartbeat.get("mode"),
            },
        ))
    result.snapshots.append(JobSnapshot(
        job_id=job.job_id,
        status=status,
        observed_at=observed_at,
        target_session=expected,
        run_id=run_id if heartbeat else None,
        stage=str(heartbeat.get("phase") or "等待服务心跳"),
        status_reason=reason,
        heartbeat_at=heartbeat_at,
        progress_current=float(len(cycles)),
        metrics={
            "heartbeat_age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "cycle_count": len(cycles),
            "cycle_error_count": sum(int(row.get("error_count") or 0) > 0 for row in cycles),
            "average_cycle_seconds": round(mean(cycle_durations), 3) if cycle_durations else None,
            "candidate_count": heartbeat.get("candidate_count"),
            "active_count": heartbeat.get("active_count"),
            "signals_sent": sent_count,
            "signals_failed": failed_count,
            "mode": heartbeat.get("mode"),
            "latest_observation": observations[0] if observations else None,
            "茶杯柄算法版本": cup_heartbeat.get("algorithm_version"),
            "茶杯柄模式": cup_heartbeat.get("mode"),
            "茶杯柄评估周期": len(cup_cycles),
            "茶杯柄命中数": cup_outcome_counts.get("MATCH", 0),
            "茶杯柄拒绝数": cup_outcome_counts.get("REJECTED", 0),
            "茶杯柄等待数": cup_outcome_counts.get("NOT_READY", 0),
            "茶杯柄错误数": cup_outcome_counts.get("ERROR", 0),
            "茶杯柄检测延迟P95毫秒": (
                round(cup_latency_p95, 3) if cup_latency_p95 is not None else None
            ),
            "茶杯柄最大序列长度": max(
                (int(row.get("bar_count") or 0) for row in cup_evaluations),
                default=0,
            ),
            "茶杯柄主要拒绝原因": dict(
                sorted(
                    cup_rejection_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )[:8]
            ),
            "茶杯柄影子验收进度": f"{cup_shadow_passed}/5",
            "茶杯柄影子缺失交易日": [
                session for session in cup_expected_shadow
                if session not in cup_by_session
            ],
            "茶杯柄影子失败交易日": [
                session for session in cup_expected_shadow
                if session in cup_by_session
                and str(cup_by_session[session].get("status") or "") != "PASS"
            ],
            "茶杯柄独立影子验收": (
                current_cup_observation
                or (cup_observations[0] if cup_observations else None)
            ),
        },
    ))
    return result


def collect_delivery_evidence(
    jobs: Iterable[JobDefinition],
    *,
    now: datetime,
    observed_at: str,
) -> CollectionResult:
    result = CollectionResult()
    for job in jobs:
        if job.adapter == "premarket":
            result.extend(_collect_premarket(job, now=now, observed_at=observed_at))
        elif job.adapter == "premarket_prepare":
            result.extend(_collect_premarket_prepare(
                job,
                now=now,
                observed_at=observed_at,
            ))
        elif job.adapter == "hourly_momentum":
            result.extend(_collect_hourly(job, now=now, observed_at=observed_at))
        elif job.adapter == "intraday_candidate_prepare":
            result.extend(_collect_intraday_candidate_prepare(
                job,
                now=now,
                observed_at=observed_at,
            ))
        elif job.adapter == "intraday_momentum":
            result.extend(_collect_intraday(job, now=now, observed_at=observed_at))
    return result


__all__ = ["collect_delivery_evidence"]
