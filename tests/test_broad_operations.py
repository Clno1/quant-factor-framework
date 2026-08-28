from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts.check_broad_resources import check_resources
from scripts.check_broad_shadow_observation import summarize_ledger
from scripts.backfill_us_equity_coverage import (
    _authenticated_manifest_or_none,
    _auto_resume_run_dir,
    _prepare_resumed_checkpoint,
)
from scripts.run_broad_daily_pipeline import _decode_json, run
from scripts.run_broad_factor_data import _auto_resume_generation
from scripts.run_broad_initial_rollout import (
    _run_stage as run_initial_rollout_stage,
    run as run_initial_rollout,
)
from scripts.update_us_equity_coverage import (
    _load_or_fetch_eod_bulk_session,
    _load_or_fetch_history_delta,
    _parent_partition_paths,
    _prepare_provider_cache,
    _sessions_after_parent,
)
from src.data.foundation import DataFoundationError
from src.operations.adapters.broad import (
    _ROLLOUT_RUNTIME_UNITS,
    _integer_metric,
    _rollout_project_status,
    collect_broad_evidence,
)
from src.operations.models import JobDefinition, JobStatus
from src.operations.registry import OperationsRegistry


def test_resource_guard_reports_memory_and_disk(tmp_path):
    report = check_resources(
        path=tmp_path,
        minimum_memory_mb=0,
        minimum_disk_gb=0,
    )
    assert report["status"] == "PASS"
    assert report["checks"]["memory"]["observed_mb"] > 0
    assert report["checks"]["disk"]["observed_gb"] > 0


def test_broad_runtime_units_cover_every_post_coverage_stage():
    assert "quant-market-data.service" in _ROLLOUT_RUNTIME_UNITS
    assert "quant-factor-research.service" in _ROLLOUT_RUNTIME_UNITS
    assert "quant-paper-trading.service" in _ROLLOUT_RUNTIME_UNITS
    assert "quant-broad-factor-data.service" in _ROLLOUT_RUNTIME_UNITS
    assert "quant-broad-research-readiness.service" in _ROLLOUT_RUNTIME_UNITS
    assert "quant-broad-shadow-observation.service" in _ROLLOUT_RUNTIME_UNITS


def test_enabled_web_does_not_mask_stale_daily_publications():
    assert _rollout_project_status(
        web_default_enabled=True,
        shadow_ready=True,
        required_ready=False,
        job_status=JobStatus.DEGRADED,
    ) == JobStatus.DEGRADED
    assert _rollout_project_status(
        web_default_enabled=True,
        shadow_ready=True,
        required_ready=False,
        job_status=JobStatus.RUNNING,
    ) == JobStatus.RUNNING


def test_integer_metric_preserves_completed_zero_remaining_sessions():
    assert _integer_metric(
        {"remaining_sessions": 0},
        "remaining_sessions",
        default=5,
    ) == 0
    assert _integer_metric({}, "remaining_sessions", default=5) == 5


def test_initial_rollout_stage_inherits_progress_stderr(capfd):
    stage = run_initial_rollout_stage(
        "TEST",
        [
            sys.executable,
            "-c",
            (
                "import json,sys; "
                "print('batch 1/2', file=sys.stderr, flush=True); "
                "print(json.dumps({'target_session':'2026-08-14'}))"
            ),
        ],
    )

    captured = capfd.readouterr()
    assert "batch 1/2" in captured.err
    assert stage["status"] == "SUCCESS"
    assert stage["result"] == {"target_session": "2026-08-14"}
    assert stage["stderr_tail"] is None


def test_operations_registry_retires_legacy_refresh_and_expects_broad_timer():
    registry = OperationsRegistry("configs/operations.yaml")
    legacy = registry.get("us_daily_refresh")
    assert legacy.enabled_expected is False
    assert legacy.adapter == "retired"
    assert registry.get("broad_us_pipeline").enabled_expected is True


def test_daily_pipeline_stops_after_first_failed_stage(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        code = 1 if len(calls) == 2 else 0
        return SimpleNamespace(
            returncode=code,
            stdout=json.dumps({
                "status": "FAILED" if code else "SUCCESS",
                "target_session": "2026-08-10",
            }),
            stderr="provider failed" if code else "",
        )

    monkeypatch.setattr("scripts.run_broad_daily_pipeline.subprocess.run", fake_run)
    report, code = run(SimpleNamespace(
        target_session="2026-08-10",
        env_file="/etc/quant/market-data.env",
        report_dir=str(tmp_path),
        json=True,
    ))

    assert code == 1
    assert report["status"] == "FAILED"
    assert [stage["name"] for stage in report["stages"]] == [
        "SECURITY_MASTER",
        "US_EQUITY_COVERAGE",
    ]
    assert len(calls) == 2
    assert Path(report["report_path"]).is_file()
    assert _decode_json("not json") is None
    assert _decode_json('[INFO] ready\n{"status":"SUCCESS"}') == {
        "status": "SUCCESS"
    }


def test_daily_pipeline_freezes_coverage_version_for_pit(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        number = len(calls)
        payload = {
            "status": "PUBLISHED",
            "target_session": "2026-08-10",
        }
        if number == 2:
            payload["publication"] = {"version_id": "coverage-v1"}
        return SimpleNamespace(
            returncode=0,
            stdout="[INFO] stage complete\n" + json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr("scripts.run_broad_daily_pipeline.subprocess.run", fake_run)
    report, code = run(SimpleNamespace(
        target_session="2026-08-10",
        env_file="/etc/quant/market-data.env",
        report_dir=str(tmp_path),
        json=True,
    ))

    assert code == 0
    assert report["status"] == "SUCCESS"
    assert [stage["name"] for stage in report["stages"]] == [
        "SECURITY_MASTER",
        "US_EQUITY_COVERAGE",
        "US_LIQUID_5M_PIT",
    ]
    assert calls[2][calls[2].index("--dataset-version-id") + 1] == "coverage-v1"


def test_shadow_ledger_counts_distinct_consecutive_trading_sessions():
    observations = [
        {"status": "PASS", "target_session": value, "publication_id": value}
        for value in (
            "2026-08-03",
            "2026-08-04",
            "2026-08-05",
            "2026-08-06",
            "2026-08-07",
            "2026-08-07",
        )
    ]
    report = summarize_ledger(
        {
            "observations": observations,
            "last_attempt": {
                "status": "PASS",
                "target_session": "2026-08-07",
            },
        },
        required_sessions=5,
        expected_session="2026-08-07",
    )
    assert report["passed_sessions_total"] == 5
    assert report["consecutive_passed_sessions"] == 5
    assert report["remaining_sessions"] == 0
    assert report["ready_for_web_default"] is True

    report = summarize_ledger(
        {
            "observations": [
                item
                for item in observations
                if item["target_session"] != "2026-08-05"
            ],
            "last_attempt": {
                "status": "FAIL",
                "target_session": "2026-08-07",
            },
        },
        required_sessions=5,
        expected_session="2026-08-07",
    )
    assert report["consecutive_dates"] == ["2026-08-06", "2026-08-07"]
    assert report["ready_for_web_default"] is False


def test_incremental_coverage_rebuilds_a_new_month_without_parent_partition(
    tmp_path,
):
    july = tmp_path / "july.parquet"
    august = tmp_path / "august.parquet"
    index = tmp_path / "bars_index.json"
    index.write_text(json.dumps({
        "storage_type": "PARTITIONED_PARQUET_V1",
        "partitions": [
            {
                "file": july.name,
                "min_date": "2026-07-01",
                "max_date": "2026-07-31",
            },
            {
                "file": august.name,
                "min_date": "2026-08-03",
                "max_date": "2026-08-31",
            },
        ],
    }), encoding="utf-8")

    unchanged, replaced, rebuild = _parent_partition_paths(
        SimpleNamespace(bars_path=str(index)),
        affected_months={"2026-08", "2026-09"},
    )
    assert unchanged == [july.resolve()]
    assert replaced == [august.resolve()]
    assert rebuild == {"2026-08", "2026-09"}


def test_incremental_bulk_fetches_only_sessions_after_the_parent():
    sessions = _sessions_after_parent("2026-08-14", "2026-08-19")
    assert [value.date().isoformat() for value in sessions] == [
        "2026-08-17",
        "2026-08-18",
        "2026-08-19",
    ]
    assert _sessions_after_parent("2026-08-19", "2026-08-19").empty


def test_eod_bulk_cache_is_exactly_bound_and_hash_verified(tmp_path):
    session = pd.Timestamp("2026-08-19")
    cache_dir, contract, fingerprint = _prepare_provider_cache(
        output_dir=tmp_path,
        target=session,
        refresh_start=pd.Timestamp("2026-08-18"),
        refresh_sessions=pd.DatetimeIndex(["2026-08-18", "2026-08-19"]),
        history_start="2019-01-01",
        parent_version_id="coverage-v1",
        security_master_generation_id="security-v2",
        security_master_manifest_sha256="security-sha-v2",
    )
    assert cache_dir.name == f"binding={fingerprint}"
    assert json.loads((cache_dir / "contract.json").read_text()) == contract

    calls = []

    def fetcher(value):
        calls.append(value)
        frame = pd.DataFrame([{
            "date": session,
            "ticker": "AAA",
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "adj_close": 10.0,
            "volume": 100.0,
        }])
        frame.attrs["invalid_ticker_rows"] = 1
        return frame

    first, first_hit, manifest = _load_or_fetch_eod_bulk_session(
        cache_dir=cache_dir,
        session=session,
        fetcher=fetcher,
    )
    second, second_hit, _manifest = _load_or_fetch_eod_bulk_session(
        cache_dir=cache_dir,
        session=session,
        fetcher=fetcher,
    )
    assert not first_hit
    assert second_hit
    assert len(calls) == 1
    assert first.equals(second)
    assert second.attrs["invalid_ticker_rows"] == 1
    assert manifest["row_count"] == 1

    manifest_path = (
        cache_dir / "eod" / "session=2026-08-19" / "manifest.json"
    )
    bad = json.loads(manifest_path.read_text())
    bad["frame_sha256"] = "tampered"
    manifest_path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(DataFoundationError, match="manifest mismatch"):
        _load_or_fetch_eod_bulk_session(
            cache_dir=cache_dir,
            session=session,
            fetcher=fetcher,
        )


def test_identity_history_delta_cache_reuses_only_the_exact_binding(tmp_path):
    cache_dir, _contract, _fingerprint = _prepare_provider_cache(
        output_dir=tmp_path,
        target=pd.Timestamp("2026-08-19"),
        refresh_start=pd.Timestamp("2026-07-24"),
        refresh_sessions=pd.DatetimeIndex(["2026-08-19"]),
        history_start="2019-01-01",
        parent_version_id="coverage-v1",
        security_master_generation_id="security-v2",
        security_master_manifest_sha256="security-sha-v2",
    )
    universe = pd.DataFrame([{
        "security_id": "sec_aaa",
        "current_ticker": "AAA",
        "listing_date": pd.Timestamp("2019-01-02"),
        "delisting_date": pd.NaT,
        "coverage_start": pd.Timestamp("2019-01-02"),
    }])
    symbols = pd.DataFrame([{
        "security_id": "sec_aaa",
        "ticker": "AAA",
        "effective_from": pd.Timestamp("2019-01-02"),
        "effective_to": pd.NaT,
    }])
    calls = []

    def fetcher(ticker, start, end):
        calls.append((ticker, start, end))
        return pd.DataFrame({
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.0],
            "adj_close": [10.0],
            "volume": [100.0],
        }, index=pd.DatetimeIndex([start], name="date"))

    first = _load_or_fetch_history_delta(
        cache_dir=cache_dir,
        security_universe=universe,
        symbol_history=symbols,
        security_ids=["sec_aaa"],
        history_start="2019-01-01",
        history_end=pd.Timestamp("2026-07-23"),
        fetcher=fetcher,
    )
    second = _load_or_fetch_history_delta(
        cache_dir=cache_dir,
        security_universe=universe,
        symbol_history=symbols,
        security_ids=["sec_aaa"],
        history_start="2019-01-01",
        history_end=pd.Timestamp("2026-07-23"),
        fetcher=fetcher,
    )
    assert first[3] is False
    assert second[3] is True
    assert first[0].equals(second[0])
    assert len(calls) == 1


def test_operations_stage_surfaces_latest_security_master_candidate_failure(
    monkeypatch,
):
    job = JobDefinition(
        job_id="broad_us_pipeline",
        display_name="全美宽基每日生产链",
        category="migration",
        run_type="migration",
        adapter="broad_pipeline",
        order=60,
        enabled_expected=False,
        schedule={"target_policy": "latest_publishable_xnys"},
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad.expected_target_session",
        lambda *_args, **_kwargs: "2026-08-12",
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad.schedule_bounds",
        lambda *_args, **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._broad_catalog",
        lambda: {
            "security_master": {
                "generation_id": "published-before-latest-audit",
                "target_session": "2026-08-12",
                "active_count": 5733,
                "status": "PUBLISHED",
            },
        },
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._latest_security_master_audit",
        lambda: {
            "target_session": "2026-08-12",
            "quality": {
                "status": "FAIL",
                "identity_security_coverage": 0.9998,
                "failures": [
                    "identity key coverage incomplete: 10184/10186",
                ],
            },
            "_report_path": "/audit/latest.json",
        },
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._latest_pipeline_report",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._latest_initial_rollout_report",
        lambda: {
            "target_session": "2026-08-12",
            "status": "RUNNING",
            "current_stage": "US_EQUITY_COVERAGE_BACKFILL",
        },
    )
    checkpoint_calls = iter(({
        "target_session": "2026-08-12",
        "status": "RUNNING",
        "selected_security_count": 7800,
        "batch_size": 100,
        "batches": {
            str(index): {"status": "SUCCESS"}
            for index in range(47)
        },
        "_checkpoint_path": "/staging/checkpoint.json",
    }, None))
    monkeypatch.setattr(
        "src.operations.adapters.broad._latest_checkpoint",
        lambda *_args, **_kwargs: next(checkpoint_calls),
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._publication",
        lambda _path: None,
    )

    result = collect_broad_evidence(
        [job],
        now=datetime(2026, 8, 13, 4, 0, tzinfo=timezone.utc),
        observed_at="2026-08-13T04:00:00+00:00",
    )

    security_stage = result.projects[0].stages[0]
    assert security_stage["status"] == JobStatus.BLOCKED.value
    assert "10184/10186" in security_stage["detail"]
    assert security_stage["metadata"]["candidate_target_session"] == "2026-08-12"
    assert "最新证券主表候选未通过质量门禁" in result.snapshots[0].status_reason
    assert result.snapshots[0].status == JobStatus.BLOCKED
    assert result.projects[0].status == JobStatus.BLOCKED
    coverage_stage = result.projects[0].stages[1]
    assert coverage_stage["status"] == JobStatus.BLOCKED.value
    assert "47/78" in coverage_stage["detail"]
    assert "不代表任务仍在运行" in coverage_stage["detail"]
    assert result.projects[0].stages[2]["status"] == JobStatus.BLOCKED.value
    assert result.projects[0].stages[3]["status"] == JobStatus.BLOCKED.value


def test_operations_stage_surfaces_latest_fmp_coverage_failure(monkeypatch):
    job = JobDefinition(
        job_id="broad_us_pipeline",
        display_name="全美宽基每日生产链",
        category="migration",
        run_type="migration",
        adapter="broad_pipeline",
        order=60,
        enabled_expected=False,
        schedule={"target_policy": "latest_publishable_xnys"},
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad.expected_target_session",
        lambda *_args, **_kwargs: "2026-08-19",
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad.schedule_bounds",
        lambda *_args, **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._broad_catalog",
        lambda: {
            "security_master": {
                "generation_id": "security-current",
                "target_session": "2026-08-19",
                "active_count": 5737,
                "status": "PUBLISHED",
            },
            "coverage": {
                "version_id": "coverage-old",
                "target_session": "2026-08-14",
                "ticker_count": 7952,
                "status": "PUBLISHED",
            },
        },
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._latest_security_master_audit",
        lambda: {
            "target_session": "2026-08-19",
            "quality": {"status": "PASS", "failures": []},
        },
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._latest_pipeline_report",
        lambda: {
            "run_id": "provider-failure",
            "target_session": "2026-08-19",
            "status": "FAILED",
            "stages": [
                {"name": "SECURITY_MASTER", "status": "SUCCESS", "returncode": 0},
                {
                    "name": "US_EQUITY_COVERAGE",
                    "status": "FAILED",
                    "returncode": 1,
                    "result": {
                        "error": "FMP /eod-bulk failed: 502 Bad Gateway",
                    },
                },
            ],
            "_report_path": "/reports/provider-failure.json",
        },
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._latest_initial_rollout_report",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._active_rollout_runtime",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._latest_checkpoint",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._publication",
        lambda _path: None,
    )

    result = collect_broad_evidence(
        [job],
        now=datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc),
        observed_at="2026-08-20T04:00:00+00:00",
    )

    coverage_stage = result.projects[0].stages[1]
    assert coverage_stage["status"] == JobStatus.FAILED.value
    assert "502 Bad Gateway" in coverage_stage["detail"]
    assert coverage_stage["metadata"]["latest_pipeline_report"] == (
        "/reports/provider-failure.json"
    )
    assert result.snapshots[0].status == JobStatus.FAILED
    assert result.projects[0].status == JobStatus.FAILED
    assert "FMP_EOD_BULK_PROVIDER_UNAVAILABLE" in result.projects[0].blockers


def test_live_recovery_supersedes_old_failure_after_current_coverage_publish(monkeypatch):
    job = JobDefinition(
        job_id="broad_us_pipeline",
        display_name="全美宽基每日生产链",
        category="migration",
        run_type="migration",
        adapter="broad_pipeline",
        order=60,
        enabled_expected=False,
        schedule={"target_policy": "latest_publishable_xnys"},
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad.expected_target_session",
        lambda *_args, **_kwargs: "2026-08-19",
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad.schedule_bounds",
        lambda *_args, **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._broad_catalog",
        lambda: {
            "security_master": {
                "generation_id": "security-current",
                "target_session": "2026-08-19",
                "active_count": 5737,
                "status": "PUBLISHED",
            },
            "coverage": {
                "version_id": "coverage-current",
                "target_session": "2026-08-19",
                "ticker_count": 7960,
                "status": "PUBLISHED",
            },
            "pit": {
                "universe_version_id": "pit-old",
                "target_session": "2026-08-14",
                "current_member_count": 2780,
                "status": "PUBLISHED",
            },
        },
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._latest_security_master_audit",
        lambda: {
            "target_session": "2026-08-19",
            "quality": {"status": "PASS", "failures": []},
        },
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._latest_pipeline_report",
        lambda: {
            "run_id": "old-provider-failure",
            "target_session": "2026-08-19",
            "status": "FAILED",
            "stages": [{
                "name": "US_EQUITY_COVERAGE",
                "status": "FAILED",
                "result": {"error": "FMP /eod-bulk timed out"},
            }],
            "_report_path": "/reports/old-provider-failure.json",
        },
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._active_rollout_runtime",
        lambda: {
            "Id": "quant-broad-provider-retry.service",
            "LoadState": "loaded",
            "ActiveState": "activating",
            "SubState": "start",
            "ExecMainStartTimestamp": "2026-08-20T07:54:01+00:00",
        },
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._latest_initial_rollout_report",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._latest_checkpoint",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.operations.adapters.broad._publication",
        lambda _path: None,
    )

    result = collect_broad_evidence(
        [job],
        now=datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc),
        observed_at="2026-08-20T09:00:00+00:00",
    )

    coverage_stage = result.projects[0].stages[1]
    pit_stage = result.projects[0].stages[2]
    assert coverage_stage["status"] == JobStatus.SUCCESS.value
    assert "coverage-cur" in coverage_stage["detail"]
    assert coverage_stage["metadata"]["latest_pipeline_error"] is None
    assert pit_stage["status"] == JobStatus.RUNNING.value
    assert "正在基于 coverage" in pit_stage["detail"]
    assert result.snapshots[0].status == JobStatus.RUNNING
    assert result.projects[0].status == JobStatus.RUNNING
    assert "FMP_EOD_BULK_PROVIDER_UNAVAILABLE" not in result.projects[0].blockers
    assert result.snapshots[0].run_id == result.runs[-1].run_id
    assert result.runs[-1].source == "systemd.rollout_runtime"


def test_coverage_auto_resume_requires_one_exact_running_checkpoint(tmp_path):
    expected = {
        "target_session": "2026-08-13",
        "security_master_generation_id": "current",
        "methodology_version": "BROAD_COVERAGE_V1",
    }
    root = tmp_path / "asof=2026-08-13"
    current = root / "run=current"
    old = root / "run=old"
    current.mkdir(parents=True)
    old.mkdir(parents=True)
    (current / "checkpoint.json").write_text(json.dumps({
        **expected,
        "status": "RUNNING",
    }), encoding="utf-8")
    (old / "checkpoint.json").write_text(json.dumps({
        **expected,
        "security_master_generation_id": "old",
        "status": "RUNNING",
    }), encoding="utf-8")

    selected, diagnostics = _auto_resume_run_dir(
        tmp_path,
        expected=expected,
    )

    assert selected == current.resolve()
    assert [item["decision"] for item in diagnostics] == ["MATCH", "REJECT"]

    duplicate = root / "run=duplicate"
    duplicate.mkdir()
    (duplicate / "checkpoint.json").write_text(json.dumps({
        **expected,
        "status": "RUNNING",
    }), encoding="utf-8")
    with pytest.raises(RuntimeError, match="multiple exact coverage"):
        _auto_resume_run_dir(tmp_path, expected=expected)


def test_coverage_same_target_rebuilds_only_for_legacy_price_semantics():
    class Reader:
        def __init__(self, error: str | None):
            self.error = error

        def verify_version(self, _published, *, require_price_semantics):
            assert require_price_semantics is True
            if self.error:
                raise DataFoundationError(self.error)
            return {"schema_version": 4, "price_semantics": {"schema_version": 1}}

    manifest = _authenticated_manifest_or_none(Reader(None), object())
    assert manifest["schema_version"] == 4

    legacy = _authenticated_manifest_or_none(
        Reader("version predates the authenticated price-semantics contract"),
        object(),
    )
    assert legacy is None

    with pytest.raises(DataFoundationError, match="checksum mismatch"):
        _authenticated_manifest_or_none(
            Reader("published partition checksum mismatch"),
            object(),
        )


def test_coverage_resume_restores_progress_after_all_batches_are_known():
    expected = {
        "target_session": "2026-08-14",
        "security_master_generation_id": "security-v2",
    }
    checkpoint = {
        **expected,
        "status": "FAIL",
        "batches": {
            "0": {"status": "SUCCESS"},
            "1": {"status": "PARTIAL"},
        },
    }

    resumed = _prepare_resumed_checkpoint(
        checkpoint,
        expected=expected,
        total_batches=80,
        resume_diagnostics=[{"decision": "MATCH"}],
    )

    assert resumed["status"] == "RUNNING"
    assert resumed["current_phase"] == "FETCHING"
    assert resumed["progress"] == {
        "completed_batches": 2,
        "total_batches": 80,
        "successful_batches": 1,
        "partial_batches": 1,
    }


def test_factor_auto_resume_requires_exact_input_hashes(tmp_path):
    expected = {
        "target_session": "2026-08-13",
        "parent_dataset_version_id": "coverage-v2",
        "membership_sha256": "membership-v2",
    }
    current = tmp_path / ".staging_current"
    old = tmp_path / ".staging_old"
    current.mkdir()
    old.mkdir()
    (current / "checkpoint.json").write_text(json.dumps({
        **expected,
        "generation_id": "current",
    }), encoding="utf-8")
    (old / "checkpoint.json").write_text(json.dumps({
        **expected,
        "generation_id": "old",
        "membership_sha256": "membership-v1",
    }), encoding="utf-8")

    generation, diagnostics = _auto_resume_generation(
        SimpleNamespace(output_root=tmp_path),
        expected=expected,
    )

    assert generation == "current"
    assert [item["decision"] for item in diagnostics] == ["MATCH", "REJECT"]


def test_initial_rollout_accepts_only_expected_research_blockers(
    monkeypatch,
    tmp_path,
):
    stage_names: list[str] = []

    def fake_stage(name, _command, **_kwargs):
        stage_names.append(name)
        result = {"target_session": "2026-08-13"}
        if name == "US_EQUITY_COVERAGE_BACKFILL":
            result["publication"] = {"version_id": "coverage-v1"}
        elif name == "BROAD_RESEARCH_READINESS":
            result.update({
                "status": "BLOCKED",
                "blockers": [
                    "PIT_CLASSIFICATION_POLICY",
                    "PIT_INDUSTRY_COVERAGE",
                ],
            })
        elif name == "BROAD_SHADOW_OBSERVATION":
            result["last_attempt"] = {"status": "PASS"}
        return {
            "name": name,
            "status": "SUCCESS",
            "returncode": 0,
            "duration_seconds": 1.0,
            "result": result,
            "stderr_tail": None,
        }

    monkeypatch.setattr(
        "scripts.run_broad_initial_rollout._run_stage",
        fake_stage,
    )
    report, code = run_initial_rollout(SimpleNamespace(
        target_session="2026-08-13",
        env_file="/etc/quant/market-data.env",
        report_dir=str(tmp_path),
        skip_service_guard=True,
        json=True,
    ))

    assert code == 0
    assert report["status"] == "SUCCESS"
    assert stage_names == [
        "RESOURCE_GUARD",
        "SECURITY_MASTER",
        "US_EQUITY_COVERAGE_BACKFILL",
        "US_LIQUID_5M_PIT",
        "DAILY_COMPATIBILITY_PIPELINE",
        "BROAD_FACTOR_DATA",
        "BROAD_RESEARCH_READINESS",
        "BROAD_SHADOW_OBSERVATION",
    ]
