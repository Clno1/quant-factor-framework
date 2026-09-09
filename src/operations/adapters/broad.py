"""Evidence adapter for the staged US broad-universe rollout."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import subprocess
from typing import Any, Iterable

from src.config import CONFIG, PROJECT_ROOT
from src.operations.evidence import (
    duration_seconds,
    expected_target_session,
    iso_utc,
    load_json,
    safe_text,
    schedule_bounds,
    session_delay,
    stable_id,
    status_from_source,
)
from src.operations.models import (
    CollectionResult,
    FreshnessObservation,
    JobDefinition,
    JobSnapshot,
    JobStatus,
    OperationRun,
    ProjectObservation,
    RunStage,
)


def _integer_metric(
    payload: dict[str, Any] | None,
    key: str,
    *,
    default: int,
) -> int:
    """Preserve meaningful zero values while defaulting absent metrics."""
    value = (payload or {}).get(key)
    return int(default if value is None else value)


def _latest_pipeline_report() -> dict[str, Any] | None:
    root = PROJECT_ROOT / "outputs" / "data_audits" / "broad_daily_pipeline"
    candidates = sorted(root.glob("target=*/run=*.json"), key=lambda path: path.stat().st_mtime)
    if not candidates:
        return None
    payload = load_json(candidates[-1])
    if payload is not None:
        payload["_report_path"] = str(candidates[-1])
    return payload


def _latest_initial_rollout_report() -> dict[str, Any] | None:
    root = PROJECT_ROOT / "outputs" / "data_audits" / "broad_initial_rollout"
    candidates = sorted(
        root.glob("target=*/run=*.json"),
        key=lambda path: path.stat().st_mtime,
    )
    if not candidates:
        return None
    payload = load_json(candidates[-1])
    if payload is not None:
        payload["_report_path"] = str(candidates[-1])
    return payload


def _latest_checkpoint(
    paths: Iterable[Path],
    *,
    target_session: str | None = None,
) -> dict[str, Any] | None:
    candidates = sorted(paths, key=lambda path: path.stat().st_mtime)
    for path in reversed(candidates):
        payload = load_json(path)
        if payload is None:
            continue
        if target_session and str(payload.get("target_session") or "") != target_session:
            continue
        payload["_checkpoint_path"] = str(path)
        return payload
    return None


def _newest_report(
    *payloads: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    candidates: list[tuple[int, dict[str, Any], str]] = []
    for payload in payloads:
        if not payload:
            continue
        path = Path(str(payload.get("_report_path") or ""))
        try:
            modified = path.stat().st_mtime_ns
        except OSError:
            modified = 0
        source = (
            "broad_initial_rollout.report"
            if "broad_initial_rollout" in str(path)
            else "broad_daily_pipeline.report"
        )
        candidates.append((modified, payload, source))
    if not candidates:
        return None, None
    _modified, payload, source = max(candidates, key=lambda item: item[0])
    return payload, source


def _latest_security_master_audit() -> dict[str, Any] | None:
    root = PROJECT_ROOT / "outputs" / "data_audits" / "security_master_candidates"
    candidates = sorted(
        root.glob("asof=*/run=*/audit.json"),
        key=lambda path: path.stat().st_mtime,
    )
    if not candidates:
        return None
    payload = load_json(candidates[-1])
    if payload is not None:
        payload["_report_path"] = str(candidates[-1])
    return payload


def _publication(path: Path) -> dict[str, Any] | None:
    return load_json(path)


_UPSTREAM_RUNTIME_UNITS = (
    "quant-market-data.service",
    "quant-factor-research.service",
    "quant-paper-trading.service",
)


_ROLLOUT_RUNTIME_UNITS = (
    *_UPSTREAM_RUNTIME_UNITS,
    "quant-broad-provider-retry.service",
    "quant-broad-initial-rollout.service",
    "quant-us-equity-coverage.service",
    "quant-broad-factor-data.service",
    "quant-broad-research-readiness.service",
    "quant-broad-shadow-observation.service",
)


def _rollout_project_status(
    *,
    web_default_enabled: bool,
    shadow_ready: bool,
    required_ready: bool,
    job_status: JobStatus,
) -> JobStatus:
    if web_default_enabled and shadow_ready and required_ready:
        return JobStatus.SUCCESS
    if job_status == JobStatus.RUNNING:
        return JobStatus.RUNNING
    if job_status == JobStatus.FAILED:
        return JobStatus.FAILED
    if web_default_enabled and shadow_ready:
        return JobStatus.DEGRADED
    return JobStatus.BLOCKED


def _active_rollout_runtime() -> dict[str, str] | None:
    """Return a live rollout worker, including controlled transient recovery units."""
    if not shutil.which("systemctl"):
        return None
    try:
        completed = subprocess.run(
            [
                "systemctl",
                "show",
                *_ROLLOUT_RUNTIME_UNITS,
                "--no-pager",
                "--property=Id,LoadState,ActiveState,SubState,Result,ExecMainStartTimestamp",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    current: dict[str, str] = {}
    rows: list[dict[str, str]] = []
    for line in [*completed.stdout.splitlines(), ""]:
        if not line.strip():
            if current:
                rows.append(current)
            current = {}
            continue
        key, separator, value = line.partition("=")
        if separator:
            current[key] = value
    return next(
        (
            row
            for row in rows
            if row.get("LoadState") == "loaded"
            and row.get("ActiveState") in {"active", "activating", "reloading"}
            and row.get("SubState") in {"running", "start", "exited"}
        ),
        None,
    )


def _broad_catalog() -> dict[str, dict[str, Any]]:
    path = CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path))
    if not path.is_file():
        return {}
    try:
        import duckdb

        connection = duckdb.connect(str(path), read_only=True)
        try:
            tables = {str(row[0]) for row in connection.execute("SHOW TABLES").fetchall()}
            output: dict[str, dict[str, Any]] = {}
            if "published_security_master" in tables:
                row = connection.execute("""
                    SELECT g.* FROM published_security_master p
                    JOIN security_master_generations g USING(generation_id)
                    WHERE p.singleton=TRUE
                """).fetchone()
                if row:
                    output["security_master"] = dict(zip(
                        [column[0] for column in connection.description], row
                    ))
            if {"published_versions", "dataset_versions"} <= tables:
                row = connection.execute("""
                    SELECT d.* FROM published_versions p
                    JOIN dataset_versions d USING(version_id)
                    WHERE p.universe='US_EQUITY_COVERAGE'
                """).fetchone()
                if row:
                    output["coverage"] = dict(zip(
                        [column[0] for column in connection.description], row
                    ))
            if {"published_universe_versions", "derived_universe_versions"} <= tables:
                row = connection.execute("""
                    SELECT d.* FROM published_universe_versions p
                    JOIN derived_universe_versions d USING(universe_version_id)
                    WHERE p.universe='US_LIQUID_5M'
                """).fetchone()
                if row:
                    output["pit"] = dict(zip(
                        [column[0] for column in connection.description], row
                    ))
            return output
        finally:
            connection.close()
    except Exception as exc:
        raise RuntimeError("broad-data catalog evidence is temporarily unreadable") from exc


def _stage(
    name: str,
    order: int,
    status: JobStatus,
    detail: str,
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "name": name,
        "order": order,
        "status": status.value,
        "detail": detail,
        "metadata": metadata,
    }


def collect_broad_evidence(
    jobs: Iterable[JobDefinition],
    *,
    now: datetime,
    observed_at: str,
) -> CollectionResult:
    result = CollectionResult()
    job = next((item for item in jobs if item.adapter == "broad_pipeline"), None)
    if job is None:
        return result
    expected = expected_target_session(job, now=now)
    scheduled_for, deadline_at = schedule_bounds(
        job,
        now=now,
        target_session=expected,
    )
    catalog = _broad_catalog()
    factor_path = CONFIG.abs_path(str(CONFIG.data.broad_factor_data.output_dir)) / "factor_data_publication.json"
    factor_publication = _publication(factor_path)
    readiness_path = CONFIG.abs_path(str(CONFIG.data.broad_factor_research.output_dir)) / "broad_research_readiness.json"
    readiness = _publication(readiness_path)
    ledger = _publication(
        PROJECT_ROOT / "outputs" / "data_audits" / "broad_shadow_observation.json"
    )
    pipeline = _latest_pipeline_report()
    initial_rollout = _latest_initial_rollout_report()
    rollout_runtime = _active_rollout_runtime()
    security_candidate = _latest_security_master_audit()
    security = catalog.get("security_master")
    coverage = catalog.get("coverage")
    pit = catalog.get("pit")
    absent_status = (
        JobStatus.SCHEDULED if not job.enabled_expected else JobStatus.MISSED
    )

    def publication_status(payload: dict[str, Any] | None) -> JobStatus:
        if not payload:
            return absent_status
        target = str(payload.get("target_session") or "")
        source_status = str(payload.get("status") or "PUBLISHED").upper()
        if target == expected and source_status in {"PUBLISHED", "PASS", "READY"}:
            return JobStatus.SUCCESS
        return JobStatus.STALE

    security_status = publication_status(security)
    candidate_quality = (security_candidate or {}).get("quality") or {}
    candidate_target = str(
        (security_candidate or {}).get("target_session") or ""
    )
    candidate_quality_status = str(candidate_quality.get("status") or "").upper()
    candidate_failures = [
        str(value) for value in candidate_quality.get("failures") or []
        if str(value).strip()
    ]
    candidate_failure_detail = safe_text(
        "; ".join(candidate_failures),
        limit=360,
    )
    if candidate_target == expected:
        if candidate_quality_status == "FAIL":
            # The newest audit for a target session supersedes an older formal
            # publication for rollout-readiness purposes.  Keep the published
            # generation as evidence, but do not let it mask a newly discovered
            # identity or interval-contract failure.
            security_status = JobStatus.BLOCKED
        elif (
            candidate_quality_status == "PASS"
            and security_status != JobStatus.SUCCESS
        ):
            security_status = JobStatus.RUNNING
    coverage_status = publication_status(coverage)
    pit_status = publication_status(pit)
    factor_status = publication_status(factor_publication)
    pipeline_target = str((pipeline or {}).get("target_session") or "")
    pipeline_failed = (
        pipeline_target == expected
        and str((pipeline or {}).get("status") or "").upper() == "FAILED"
    )
    pipeline_coverage_stage = next(
        (
            item
            for item in (pipeline or {}).get("stages") or []
            if isinstance(item, dict)
            and str(item.get("name") or "") == "US_EQUITY_COVERAGE"
            and str(item.get("status") or "").upper() == "FAILED"
        ),
        None,
    )
    pipeline_coverage_error = safe_text(
        ((pipeline_coverage_stage or {}).get("result") or {}).get("error")
        or (pipeline_coverage_stage or {}).get("stderr_tail")
    )
    # A current immutable publication supersedes an older failed attempt for
    # the same target. Keep the failed run in history, but do not report the
    # published coverage itself as failed.
    pipeline_coverage_failed = bool(
        pipeline_failed
        and pipeline_coverage_stage
        and coverage_status != JobStatus.SUCCESS
    )
    if pipeline_coverage_failed:
        coverage_status = JobStatus.FAILED
    coverage_checkpoint = _latest_checkpoint(
        (
            PROJECT_ROOT
            / "data"
            / "lake"
            / "staging"
            / "us_equity_coverage"
            / f"asof={expected}"
        ).glob("run=*/checkpoint.json")
    )
    factor_checkpoint = _latest_checkpoint(
        (
            CONFIG.abs_path(str(CONFIG.data.broad_factor_data.output_dir))
        ).glob(".staging_*/checkpoint.json"),
        target_session=expected,
    )
    if coverage_status != JobStatus.SUCCESS and coverage_checkpoint:
        checkpoint_status = str(coverage_checkpoint.get("status") or "").upper()
        if str(coverage_checkpoint.get("target_session") or "") == expected:
            coverage_status = (
                JobStatus.RUNNING
                if checkpoint_status == "RUNNING"
                else JobStatus.BLOCKED
                if checkpoint_status in {"FAIL", "FAILED"}
                else coverage_status
            )
    if factor_status != JobStatus.SUCCESS and factor_checkpoint:
        checkpoint_status = str(factor_checkpoint.get("status") or "").upper()
        if str(factor_checkpoint.get("target_session") or "") == expected:
            factor_status = (
                JobStatus.RUNNING
                if checkpoint_status == "RUNNING"
                else JobStatus.BLOCKED
                if checkpoint_status in {"FAIL", "FAILED"}
                else factor_status
            )

    security_gate_blocked = (
        security_status == JobStatus.BLOCKED
        and candidate_target == expected
        and candidate_quality_status == "FAIL"
    )
    if security_gate_blocked:
        # A checkpoint records resumable work; it is not proof that a process is
        # still alive.  The failed Security Master gate takes precedence over
        # stale RUNNING checkpoints and reports from an interrupted rollout.
        if coverage_status != JobStatus.SUCCESS:
            coverage_status = JobStatus.BLOCKED
        if pit_status != JobStatus.SUCCESS:
            pit_status = JobStatus.BLOCKED
        if factor_status != JobStatus.SUCCESS:
            factor_status = JobStatus.BLOCKED

    runtime_stage: str | None = None
    runtime_unit = str((rollout_runtime or {}).get("Id") or "")
    upstream_runtime = runtime_unit in _UPSTREAM_RUNTIME_UNITS
    if rollout_runtime and not security_gate_blocked:
        if upstream_runtime:
            upstream_name = {
                "quant-market-data.service": "核心行情日更",
                "quant-factor-research.service": "核心因子研究",
                "quant-paper-trading.service": "模拟盘结算",
            }[runtime_unit]
            runtime_stage = f"等待{upstream_name}完成"
            security_status = (
                JobStatus.SUCCESS
                if security_status == JobStatus.SUCCESS
                else JobStatus.SCHEDULED
            )
            coverage_status = (
                JobStatus.SUCCESS
                if coverage_status == JobStatus.SUCCESS
                else JobStatus.SCHEDULED
            )
            pit_status = (
                JobStatus.SUCCESS
                if pit_status == JobStatus.SUCCESS
                else JobStatus.SCHEDULED
            )
            factor_status = (
                JobStatus.SUCCESS
                if factor_status == JobStatus.SUCCESS
                else JobStatus.SCHEDULED
            )
        elif security_status != JobStatus.SUCCESS:
            security_status = JobStatus.RUNNING
            runtime_stage = "证券主表"
        elif coverage_status != JobStatus.SUCCESS:
            coverage_status = JobStatus.RUNNING
            runtime_stage = "全美行情覆盖"
        elif pit_status != JobStatus.SUCCESS:
            pit_status = JobStatus.RUNNING
            runtime_stage = "PIT 宽基股票池"
        elif factor_status != JobStatus.SUCCESS:
            factor_status = JobStatus.RUNNING
            runtime_stage = "八因子数据"
        else:
            runtime_stage = "生产链收尾校验"

    coverage_batches = (coverage_checkpoint or {}).get("batches") or {}
    coverage_completed = sum(
        str(item.get("status") or "").upper() == "SUCCESS"
        for item in coverage_batches.values()
        if isinstance(item, dict)
    )
    coverage_total = 0
    if coverage_checkpoint:
        selected_count = int(
            coverage_checkpoint.get("selected_security_count") or 0
        )
        batch_size = max(1, int(coverage_checkpoint.get("batch_size") or 1))
        coverage_total = (selected_count + batch_size - 1) // batch_size
    factor_completed = int(
        (factor_checkpoint or {}).get("completed_partition_count")
        or len((factor_checkpoint or {}).get("completed") or {})
    )
    factor_total = int(
        (factor_checkpoint or {}).get("expected_partition_count") or 0
    )

    stages = [
        _stage(
            "证券主表",
            1,
            security_status,
            (
                "最新候选未通过质量门禁："
                f"{candidate_failure_detail or '请查看候选审计报告'}"
                if security_status == JobStatus.BLOCKED
                and candidate_target == expected
                else (
                    "最新候选已通过质量门禁，等待正式发布"
                    if security_status == JobStatus.RUNNING
                    and candidate_target == expected
                    else (
                        f"正式代次 {str(security.get('generation_id'))[:12]}，活跃证券 {int(security.get('active_count') or 0)}"
                        if security else "尚无正式 Security Master"
                    )
                )
            ),
            generation_id=(security or {}).get("generation_id"),
            target_session=str((security or {}).get("target_session") or "") or None,
            candidate_target_session=candidate_target or None,
            candidate_quality_status=candidate_quality_status or None,
            candidate_identity_security_coverage=candidate_quality.get(
                "identity_security_coverage"
            ),
            candidate_failures=candidate_failures,
            candidate_report_path=(security_candidate or {}).get("_report_path"),
        ),
        _stage(
            "全美行情覆盖",
            2,
            coverage_status,
            (
                "最新日更失败："
                f"{pipeline_coverage_error or '请查看宽基每日生产报告'}；"
                + (
                    f"正式版本仍停留在 {str(coverage.get('version_id'))[:12]}"
                    if coverage else "尚无正式 coverage 版本"
                )
                if pipeline_coverage_failed and not security_gate_blocked
                else f"版本 {str(coverage.get('version_id'))[:12]}，{int(coverage.get('ticker_count') or 0)} 只证券"
                if coverage
                else (
                    "被证券主表质量门禁阻断；"
                    f"旧检查点保留在 {coverage_completed}/{coverage_total} 批，不代表任务仍在运行"
                    if security_gate_blocked and coverage_checkpoint and coverage_total
                    else "被证券主表质量门禁阻断"
                    if security_gate_blocked
                    else (
                        f"首次回填 {coverage_completed}/{coverage_total} 批"
                        if coverage_checkpoint and coverage_total
                        else "2019 至今行情回填或正式发布尚未完成"
                    )
                )
            ),
            version_id=(coverage or {}).get("version_id"),
            target_session=str((coverage or {}).get("target_session") or "") or None,
            checkpoint_path=(coverage_checkpoint or {}).get("_checkpoint_path"),
            completed_batches=coverage_completed,
            total_batches=coverage_total,
            alias_failure_count=len(
                (coverage_checkpoint or {}).get("alias_failures") or []
            ),
            latest_pipeline_report=(pipeline or {}).get("_report_path"),
            latest_pipeline_error=(
                pipeline_coverage_error if pipeline_coverage_failed else None
            ),
        ),
        _stage(
            "PIT 宽基股票池",
            3,
            pit_status,
            (
                f"正在基于 coverage {str((coverage or {}).get('version_id') or '')[:12]} 构建 target {expected} PIT"
                if pit_status == JobStatus.RUNNING and runtime_stage == "PIT 宽基股票池"
                else f"版本 {str(pit.get('universe_version_id'))[:12]}，当前成员 {int(pit.get('current_member_count') or 0)}"
                if pit
                else "被证券主表质量门禁阻断"
                if security_gate_blocked
                else "US_LIQUID_5M PIT 尚未发布"
            ),
            universe_version_id=(pit or {}).get("universe_version_id"),
            target_session=str((pit or {}).get("target_session") or "") or None,
            runtime_unit=(rollout_runtime or {}).get("Id"),
        ),
        _stage(
            "八因子数据",
            4,
            factor_status,
            (
                f"正在为 target {expected} 计算八因子 raw/clean/排名"
                if factor_status == JobStatus.RUNNING and runtime_stage == "八因子数据"
                else f"代次 {str((factor_publication or {}).get('generation_id') or '')[:12]}，{len((factor_publication or {}).get('factors') or {})} 个因子"
                if factor_publication
                else "被证券主表质量门禁阻断"
                if security_gate_blocked
                else (
                    f"历史因子分片 {factor_completed}/{factor_total}"
                    if factor_checkpoint and factor_total
                    else "八因子 raw/clean/排名发布尚未完成"
                )
            ),
            generation_id=(factor_publication or {}).get("generation_id"),
            target_session=(factor_publication or {}).get("target_session"),
            checkpoint_path=(factor_checkpoint or {}).get("_checkpoint_path"),
            completed_partitions=factor_completed,
            total_partitions=factor_total,
        ),
        _stage(
            "五交易日影子验收",
            5,
            (
                JobStatus.SUCCESS
                if (ledger or {}).get("ready_for_web_default")
                else JobStatus.RUNNING if ledger else absent_status
            ),
            (
                f"连续通过 {int((ledger or {}).get('consecutive_passed_sessions') or 0)}/{int((ledger or {}).get('required_sessions') or 5)}"
                if ledger else "影子观察台账尚未建立"
            ),
            remaining_sessions=(ledger or {}).get("remaining_sessions"),
            consecutive_dates=(ledger or {}).get("consecutive_dates") or [],
        ),
        _stage(
            "宽基正式置信研究",
            6,
            (
                JobStatus.SUCCESS
                if (readiness or {}).get("status") == "READY"
                else JobStatus.BLOCKED
            ),
            (
                "PIT 行业数据已满足正式 IC/ICIR 研究门槛"
                if (readiness or {}).get("status") == "READY"
                else "仍被 PIT 行业分类历史门槛阻断"
            ),
            blockers=(readiness or {}).get("blockers") or ["PIT_INDUSTRY_HISTORY_NOT_READY"],
        ),
    ]
    required_ready = all(
        stage["status"] == JobStatus.SUCCESS.value for stage in stages[:4]
    )
    observing = stages[4]["status"] == JobStatus.RUNNING.value
    initial_target = str((initial_rollout or {}).get("target_session") or "")
    initial_status = str((initial_rollout or {}).get("status") or "").upper()
    run_source, run_source_name = _newest_report(initial_rollout, pipeline)
    if security_gate_blocked:
        job_status = JobStatus.BLOCKED
        reason = (
            "最新证券主表候选未通过质量门禁："
            f"{candidate_failure_detail or '请查看候选审计报告'}；"
            "旧回填检查点已保留，但任务没有继续运行"
        )
    elif rollout_runtime:
        job_status = JobStatus.RUNNING
        reason = (
            f"受控恢复任务正在运行：{runtime_stage or '生产链执行中'}"
            f"（{rollout_runtime.get('Id') or 'systemd service'}）"
        )
    elif initial_target == expected and initial_status == "RUNNING":
        job_status = JobStatus.RUNNING
        reason = (
            "首次上线链正在运行："
            + str(initial_rollout.get("current_stage") or "准备阶段")
        )
    elif initial_target == expected and initial_status == "FAILED":
        job_status = JobStatus.FAILED
        reason = "首次上线链失败：" + str(
            initial_rollout.get("error") or "请查看运行报告"
        )
    elif (
        run_source
        and str(run_source.get("target_session") or "") == expected
        and str(run_source.get("status") or "").upper() == "FAILED"
    ):
        job_status = JobStatus.FAILED
        reason = "最近一次宽基每日生产链失败：" + str(
            pipeline_coverage_error or "请查看运行报告"
        )
    elif not job.enabled_expected:
        job_status = JobStatus.DISABLED
        if security_status == JobStatus.BLOCKED and candidate_target == expected:
            reason = (
                "定时任务保持关闭；最新证券主表候选未通过质量门禁："
                f"{candidate_failure_detail or '请查看候选审计报告'}"
            )
        elif security_status == JobStatus.RUNNING and candidate_target == expected:
            reason = "定时任务保持关闭；证券主表候选已通过，等待正式发布"
        else:
            reason = "定时任务按上线计划保持关闭，等待首次回填与影子验收"
    elif required_ready and observing:
        job_status = JobStatus.RUNNING
        reason = "正式数据已就绪，正在累计五个交易日影子观察"
    elif required_ready:
        job_status = JobStatus.SUCCESS
        reason = "宽基因子数据链已发布"
    elif bool(CONFIG.data.broad_factor_data.web_default_enabled) and (
        ledger or {}
    ).get("ready_for_web_default"):
        job_status = JobStatus.DEGRADED
        reason = "宽基网页已启用，但当前交易日的数据链尚未完整发布"
    else:
        job_status = JobStatus.BLOCKED
        reason = "首次上线的前置数据链尚未完整发布"

    if run_source:
        pipeline_stages: list[RunStage] = []
        for order, source in enumerate(run_source.get("stages") or [], start=1):
            pipeline_stages.append(RunStage(
                stage_name=str(source.get("name") or f"stage_{order}"),
                stage_order=order,
                status=status_from_source(source.get("status")),
                duration_seconds=(
                    float(source.get("duration_seconds"))
                    if source.get("duration_seconds") is not None else None
                ),
                detail=safe_text(source.get("stderr_tail")),
                metadata={
                    "returncode": source.get("returncode"),
                    "result": source.get("result"),
                },
            ))
        source_id = str(run_source.get("run_id") or "")
        result.runs.append(OperationRun(
            run_id=stable_id("run_", job.job_id, source_id),
            source_run_id=source_id,
            job_id=job.job_id,
            status=status_from_source(run_source.get("status")),
            source=str(run_source_name),
            observed_at=observed_at,
            target_session=str(run_source.get("target_session") or "") or None,
            stage=(pipeline_stages[-1].stage_name if pipeline_stages else None),
            started_at=iso_utc(run_source.get("started_at")),
            completed_at=iso_utc(run_source.get("completed_at")),
            duration_seconds=(
                float(run_source.get("duration_seconds"))
                if run_source.get("duration_seconds") is not None
                else duration_seconds(run_source.get("started_at"), run_source.get("completed_at"))
            ),
            error_summary=next(
                (safe_text(stage.detail) for stage in reversed(pipeline_stages) if stage.status == JobStatus.FAILED),
                None,
            ),
            metadata={"peak_rss_mb": run_source.get("peak_rss_mb")},
            stages=tuple(pipeline_stages),
        ))
    runtime_run_id = (
        stable_id(
            "run_",
            job.job_id,
            rollout_runtime.get("Id"),
            rollout_runtime.get("ExecMainStartTimestamp"),
        )
        if rollout_runtime else None
    )
    if rollout_runtime and runtime_run_id:
        result.runs.append(OperationRun(
            run_id=runtime_run_id,
            source_run_id=(
                f"{rollout_runtime.get('Id')}:"
                f"{rollout_runtime.get('ExecMainStartTimestamp')}"
            ),
            job_id=job.job_id,
            status=JobStatus.RUNNING,
            source="systemd.rollout_runtime",
            observed_at=observed_at,
            target_session=expected,
            stage=runtime_stage,
            heartbeat_at=observed_at,
            metadata={
                "unit": rollout_runtime.get("Id"),
                "active_state": rollout_runtime.get("ActiveState"),
                "sub_state": rollout_runtime.get("SubState"),
            },
        ))
    latest_run_id = runtime_run_id or (
        stable_id("run_", job.job_id, run_source.get("run_id"))
        if run_source else None
    )
    result.snapshots.append(JobSnapshot(
        job_id=job.job_id,
        status=job_status,
        observed_at=observed_at,
        target_session=expected,
        run_id=latest_run_id,
        stage="宽基上线专项",
        status_reason=reason,
        scheduled_for=scheduled_for,
        deadline_at=deadline_at,
        last_success_at=(
            iso_utc(run_source.get("completed_at"))
            if run_source and run_source.get("status") == "SUCCESS" else None
        ),
        progress_current=float(sum(
            stage["status"] == JobStatus.SUCCESS.value for stage in stages[:5]
        )),
        progress_total=5.0,
        output_version=str((factor_publication or {}).get("generation_id") or "") or None,
        metrics={
            "web_default_enabled": bool(CONFIG.data.broad_factor_data.web_default_enabled),
            "shadow_passed": _integer_metric(
                ledger,
                "consecutive_passed_sessions",
                default=0,
            ),
            "shadow_required": _integer_metric(
                ledger,
                "required_sessions",
                default=5,
            ),
            "shadow_remaining": _integer_metric(
                ledger,
                "remaining_sessions",
                default=5,
            ),
            "formal_research_status": (readiness or {}).get("status") or "BLOCKED",
        },
    ))
    rollout_blockers = list((readiness or {}).get("blockers") or [])
    if security_status == JobStatus.BLOCKED and candidate_target == expected:
        rollout_blockers = [
            *(candidate_failures or ["SECURITY_MASTER_CANDIDATE_FAILED"]),
            *rollout_blockers,
        ]
    if pipeline_coverage_failed:
        provider_failure = any(
            token in str(pipeline_coverage_error or "").lower()
            for token in ("fmp", "eod-bulk", "502", "timed out")
        )
        rollout_blockers = [
            "FMP_EOD_BULK_PROVIDER_UNAVAILABLE"
            if provider_failure else "US_EQUITY_COVERAGE_PIPELINE_FAILED",
            *rollout_blockers,
        ]
    web_default_enabled = bool(CONFIG.data.broad_factor_data.web_default_enabled)
    shadow_ready = bool((ledger or {}).get("ready_for_web_default"))
    project_status = _rollout_project_status(
        web_default_enabled=web_default_enabled,
        shadow_ready=shadow_ready,
        required_ready=required_ready,
        job_status=job_status,
    )
    result.projects.append(ProjectObservation(
        project_id="us_broad_factor_rollout",
        display_name="全美宽基因子研究上线",
        status=project_status,
        observed_at=observed_at,
        summary=(
            "宽基页面默认开关已启用"
            if project_status == JobStatus.SUCCESS
            else reason
        ),
        stages=tuple(stages),
        blockers=tuple(dict.fromkeys(rollout_blockers)),
        metrics={
            "target_session": expected,
            "shadow_passed": _integer_metric(
                ledger,
                "consecutive_passed_sessions",
                default=0,
            ),
            "shadow_required": _integer_metric(
                ledger,
                "required_sessions",
                default=5,
            ),
            "web_default_enabled": web_default_enabled,
        },
    ))
    for object_id, name, payload, version_key in (
        ("security_master", "全美证券主表", security, "generation_id"),
        ("us_equity_coverage", "全美行情覆盖", coverage, "version_id"),
        ("us_liquid_5m_pit", "US_LIQUID_5M PIT", pit, "universe_version_id"),
        ("us_liquid_5m_factors", "US_LIQUID_5M 八因子数据", factor_publication, "generation_id"),
    ):
        actual = str((payload or {}).get("target_session") or "") or None
        result.freshness.append(FreshnessObservation(
            object_id=object_id,
            display_name=name,
            category="BROAD_US",
            status=publication_status(payload),
            observed_at=observed_at,
            expected_session=expected,
            actual_session=actual,
            delay_sessions=session_delay(expected, actual),
            version_id=str((payload or {}).get(version_key) or "") or None,
            row_count=(
                int((payload or {}).get("row_count") or 0)
                if payload and payload.get("row_count") is not None else None
            ),
            item_count=(
                int((payload or {}).get("ticker_count") or (payload or {}).get("current_member_count") or 0)
                if payload else None
            ),
            quality={"status": (payload or {}).get("status")},
            source="broad_publication_contract",
        ))
    return result


__all__ = ["collect_broad_evidence"]
