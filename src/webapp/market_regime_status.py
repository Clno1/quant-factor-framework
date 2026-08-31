"""Read-only, fail-closed view model for broad-market regime research."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

import pandas as pd

from src.config import CONFIG
from src.market_regime_research.artifacts import file_sha256
from src.market_regime_research.screening_artifacts import (
    PREDICTIONS_FILE,
    SCORECARD_FILE,
    SUMMARY_FILE,
)
from src.market_regime_research.settings import (
    MarketRegimeResearchSettings,
    load_market_regime_research_settings,
)
from src.utils.market_calendar import latest_publishable_xnys_session


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_PRIMARY_CANDIDATE = "bottom_spx_return_5d__5d"
_REQUIRED_STAGE_A = (
    "features.parquet",
    "labels.parquet",
    "feature_registry.parquet",
)
_REQUIRED_SCREENING = (
    SCORECARD_FILE,
    PREDICTIONS_FILE,
    SUMMARY_FILE,
)
_INSTRUMENTS = {
    "spx": {"symbol": "^GSPC", "label": "标普 500"},
    "ndx": {"symbol": "^NDX", "label": "纳斯达克 100"},
}
_PERIODS = {
    "recent": {
        "label": "近期 1 年",
        "kind": "tail",
        "sessions": 252,
    },
    "wf_2020_2021": {
        "label": "2020-2021",
        "kind": "range",
        "start": "2020-01-01",
        "end": "2021-12-31",
    },
    "wf_2008_2009": {
        "label": "2008-2009",
        "kind": "range",
        "start": "2008-01-01",
        "end": "2009-12-31",
    },
    "wf_2002_2003": {
        "label": "2002-2003",
        "kind": "range",
        "start": "2002-01-01",
        "end": "2003-12-31",
    },
}


class MarketRegimeViewError(RuntimeError):
    """A research view cannot prove the integrity of its local artifacts."""


@dataclass(frozen=True, slots=True)
class ScreeningView:
    pointer: dict[str, Any]
    manifest: dict[str, Any]
    summary: dict[str, Any]
    source: dict[str, Any]
    scorecard: pd.DataFrame
    predictions: pd.DataFrame


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MarketRegimeViewError(f"无法读取研究产物：{path.name}") from exc
    if not isinstance(payload, dict):
        raise MarketRegimeViewError(f"研究产物不是对象：{path.name}")
    return payload


def _read_parquet(path: Path, *, label: str) -> pd.DataFrame:
    try:
        frame = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        raise MarketRegimeViewError(f"无法读取 {label}") from exc
    if not isinstance(frame, pd.DataFrame):
        raise MarketRegimeViewError(f"{label} 不是数据表")
    return frame


def _safe_id(value: Any, *, label: str) -> str:
    identifier = str(value or "").strip()
    if not _SAFE_ID.fullmatch(identifier) or identifier in {".", ".."}:
        raise MarketRegimeViewError(f"{label} 缺失或不安全")
    return identifier


def _safe_child(root: Path, relative: str | Path) -> Path:
    resolved_root = Path(root).resolve()
    candidate = (resolved_root / Path(relative)).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise MarketRegimeViewError("研究产物路径越过允许目录") from exc
    return candidate


def _verify_hashes(
    root: Path,
    artifacts: Mapping[str, Any],
    required: tuple[str, ...],
) -> None:
    for filename in required:
        path = _safe_child(root, filename)
        expected = str(artifacts.get(filename) or "")
        if not path.is_file() or not expected:
            raise MarketRegimeViewError(f"研究产物缺少 {filename}")
        try:
            actual = file_sha256(path)
        except OSError as exc:
            raise MarketRegimeViewError(f"无法校验研究产物：{filename}") from exc
        if actual != expected:
            raise MarketRegimeViewError(f"研究产物哈希不一致：{filename}")


def _clean_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _session_delay(expected: str, observed: str | None) -> int | None:
    if not observed:
        return None
    expected_ts = pd.Timestamp(expected).normalize()
    observed_ts = pd.Timestamp(observed).normalize()
    if observed_ts >= expected_ts:
        return 0
    try:
        import exchange_calendars as xcals

        calendar = xcals.get_calendar(
            "XNYS",
            start=(observed_ts - pd.Timedelta(days=3)).date().isoformat(),
            end=(expected_ts + pd.Timedelta(days=3)).date().isoformat(),
        )
        return int(
            len(
                calendar.sessions_in_range(
                    observed_ts.date().isoformat(),
                    expected_ts.date().isoformat(),
                )
            )
            - 1
        )
    except Exception:  # noqa: BLE001
        return int(len(pd.bdate_range(observed_ts, expected_ts)) - 1)


def _expected_session() -> str:
    delay = int(getattr(CONFIG.data.foundation, "close_delay_minutes", 120))
    return latest_publishable_xnys_session(
        delay_minutes=delay
    ).date().isoformat()


def _load_stage_a(settings: MarketRegimeResearchSettings) -> dict[str, Any]:
    root = settings.output_root
    pointer = _read_object(root / "latest.json")
    run_id = _safe_id(pointer.get("run_id"), label="Stage A run_id")
    run_dir = _safe_child(root / "runs", run_id)
    if not run_dir.is_dir():
        raise MarketRegimeViewError("Stage A 目录不存在")
    run = _read_object(run_dir / "run.json")
    manifest = _read_object(run_dir / "data_manifest.json")
    if run.get("status") != "SUCCESS" or manifest.get("run_id") != run_id:
        raise MarketRegimeViewError("Stage A 状态或身份不一致")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise MarketRegimeViewError("Stage A manifest 缺少哈希")
    _verify_hashes(run_dir, artifacts, _REQUIRED_STAGE_A)

    source_manifest = ((manifest.get("inputs") or {}).get("source_manifest") or {})
    primary = next(
        (
            item
            for item in source_manifest.get("sources", [])
            if isinstance(item, Mapping) and item.get("instrument") == "^GSPC"
        ),
        {},
    )
    pit = (manifest.get("inputs") or {}).get("point_in_time") or {}
    return {
        "status": "PASS",
        "run_id": run_id,
        "algorithm_version": manifest.get("algorithm_version"),
        "created_at": manifest.get("created_at"),
        "date_start": primary.get("first_observation"),
        "date_end": primary.get("last_observation"),
        "feature_rows": int(manifest.get("feature_rows") or 0),
        "feature_columns": int(manifest.get("feature_columns") or 0),
        "mode": "FULL_PIT" if pit else "MARKET_CORE_ONLY",
    }


def _verify_screening_source(
    settings: MarketRegimeResearchSettings,
    manifest: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    source = manifest.get("source")
    if not isinstance(source, Mapping):
        raise MarketRegimeViewError("Stage B manifest 缺少 Stage A 来源")
    run_id = _safe_id(source.get("run_id"), label="Stage B source run_id")
    if str(summary.get("source_research_run_id") or "") != run_id:
        raise MarketRegimeViewError("Stage B 与 Stage A 来源身份不一致")
    run_dir = _safe_child(settings.output_root / "runs", run_id)
    research_manifest_path = run_dir / "data_manifest.json"
    expected_manifest_hash = str(source.get("research_manifest_sha256") or "")
    if not research_manifest_path.is_file() or not expected_manifest_hash:
        raise MarketRegimeViewError("Stage B 引用的 Stage A manifest 哈希不一致")
    try:
        research_manifest_hash = file_sha256(research_manifest_path)
    except OSError as exc:
        raise MarketRegimeViewError("无法校验 Stage A manifest") from exc
    if research_manifest_hash != expected_manifest_hash:
        raise MarketRegimeViewError("Stage B 引用的 Stage A manifest 哈希不一致")
    research_manifest = _read_object(research_manifest_path)
    run = _read_object(run_dir / "run.json")
    if research_manifest.get("run_id") != run_id or run.get("status") != "SUCCESS":
        raise MarketRegimeViewError("Stage B 引用的 Stage A 状态无效")
    research_artifacts = source.get("research_artifacts")
    if not isinstance(research_artifacts, Mapping):
        raise MarketRegimeViewError("Stage B 缺少 Stage A 产物哈希")
    _verify_hashes(run_dir, research_artifacts, _REQUIRED_STAGE_A)
    research_inputs = research_manifest.get("inputs")
    if not isinstance(research_inputs, Mapping):
        raise MarketRegimeViewError("Stage B 引用的 Stage A 输入无效")
    source_manifest = research_inputs.get("source_manifest")
    if not isinstance(source_manifest, Mapping) or not isinstance(
        source_manifest.get("sources"),
        list,
    ):
        raise MarketRegimeViewError("Stage B 引用的 Stage A 来源清单无效")
    primary = next(
        (
            item
            for item in source_manifest.get("sources", [])
            if isinstance(item, Mapping) and item.get("instrument") == "^GSPC"
        ),
        {},
    )
    return {
        "run_id": run_id,
        "date_end": primary.get("last_observation"),
    }


def _validated_screening_predictions(
    predictions: pd.DataFrame,
    *,
    holdout_start: Any,
) -> pd.DataFrame:
    try:
        holdout = pd.Timestamp(str(holdout_start)).normalize()
    except (TypeError, ValueError) as exc:
        raise MarketRegimeViewError("Stage B holdout_start 无效") from exc
    if pd.isna(holdout):
        raise MarketRegimeViewError("Stage B holdout_start 无效")
    if holdout.tzinfo is not None:
        holdout = holdout.tz_convert(None)
    dates = pd.to_datetime(predictions["date"], errors="coerce", utc=True)
    if dates.isna().any():
        raise MarketRegimeViewError("Stage B predictions 包含无效日期")
    dates = dates.dt.tz_convert(None).dt.normalize()
    candidate_values = predictions["candidate_id"]
    candidate_ids = candidate_values.astype(str).str.strip()
    if candidate_values.isna().any() or candidate_ids.eq("").any():
        raise MarketRegimeViewError("Stage B predictions 包含空 candidate_id")
    keys = pd.DataFrame({"candidate_id": candidate_ids, "date": dates})
    if keys.duplicated().any():
        raise MarketRegimeViewError("Stage B predictions 包含重复候选日期")
    if dates.ge(holdout).any():
        raise MarketRegimeViewError("Stage B predictions 越过封存集边界")
    output = predictions.copy()
    output["candidate_id"] = candidate_ids
    output["date"] = dates
    return output


def _load_screening(settings: MarketRegimeResearchSettings) -> ScreeningView:
    root = settings.output_root
    pointer = _read_object(root / "latest_screening.json")
    screening_id = _safe_id(
        pointer.get("screening_id"),
        label="Stage B screening_id",
    )
    screening_dir = _safe_child(root / "screenings", screening_id)
    if not screening_dir.is_dir():
        raise MarketRegimeViewError("Stage B 目录不存在")
    manifest = _read_object(screening_dir / "screening_manifest.json")
    summary = _read_object(screening_dir / SUMMARY_FILE)
    run = _read_object(screening_dir / "screening.json")
    if (
        run.get("status") != "SUCCESS"
        or manifest.get("screening_id") != screening_id
        or summary.get("screening_id") != screening_id
        or manifest.get("holdout_status") != "SEALED_NOT_EVALUATED"
        or summary.get("holdout_status") != "SEALED_NOT_EVALUATED"
    ):
        raise MarketRegimeViewError("Stage B 状态或身份不一致")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise MarketRegimeViewError("Stage B manifest 缺少哈希")
    _verify_hashes(screening_dir, artifacts, _REQUIRED_SCREENING)

    scorecard = _read_parquet(
        screening_dir / SCORECARD_FILE,
        label="Stage B scorecard",
    )
    predictions = _read_parquet(
        screening_dir / PREDICTIONS_FILE,
        label="Stage B predictions",
    )
    score_required = {
        "candidate_id",
        "side",
        "horizon",
        "hypothesis_tier",
        "screening_status",
        "production_approved",
    }
    prediction_required = {
        "candidate_id",
        "date",
        "feature_value",
        "actual",
        "model_probability",
        "baseline_probability",
        "signal",
        "touch_day",
    }
    if scorecard.empty or not score_required.issubset(scorecard.columns):
        raise MarketRegimeViewError("Stage B scorecard 字段不完整")
    if predictions.empty or not prediction_required.issubset(predictions.columns):
        raise MarketRegimeViewError("Stage B predictions 字段不完整")
    if (
        scorecard["candidate_id"].isna().any()
        or scorecard["candidate_id"].astype(str).duplicated().any()
    ):
        raise MarketRegimeViewError("Stage B scorecard 包含重复 candidate_id")
    approved = scorecard["production_approved"]
    if approved.isna().any() or not approved.isin([True, False]).all():
        raise MarketRegimeViewError("Stage B production_approved 类型无效")
    source = _verify_screening_source(settings, manifest, summary)
    predictions = _validated_screening_predictions(
        predictions,
        holdout_start=summary.get("holdout_start"),
    )
    return ScreeningView(
        pointer=pointer,
        manifest=manifest,
        summary=summary,
        source=source,
        scorecard=scorecard,
        predictions=predictions,
    )


def _load_source_manifest(
    settings: MarketRegimeResearchSettings,
) -> dict[str, Any]:
    manifest = _read_object(settings.source_manifest_path)
    if manifest.get("schema_version") != "1.0.0":
        raise MarketRegimeViewError("市场观察 source manifest 版本不兼容")
    if not isinstance(manifest.get("sources"), list):
        raise MarketRegimeViewError("市场观察 source manifest 缺少来源")
    return manifest


def _source_entry(manifest: Mapping[str, Any], instrument: str) -> Mapping[str, Any]:
    matches = [
        item
        for item in manifest.get("sources", [])
        if isinstance(item, Mapping)
        and item.get("instrument") == instrument
        and item.get("path")
        and item.get("file_sha256")
    ]
    if len(matches) != 1 or matches[0].get("quality_status") != "PASS":
        raise MarketRegimeViewError(f"市场观察来源不可用：{instrument}")
    return matches[0]


def _load_verified_frame(
    settings: MarketRegimeResearchSettings,
    manifest: Mapping[str, Any],
    instrument: str,
) -> pd.DataFrame:
    entry = _source_entry(manifest, instrument)
    path = _safe_child(settings.raw_root, str(entry["path"]))
    if not path.is_file() or file_sha256(path) != entry["file_sha256"]:
        raise MarketRegimeViewError(f"市场观察文件哈希不一致：{instrument}")
    frame = _read_parquet(path, label=f"市场观察 {instrument}")
    frame.index = pd.to_datetime(frame.index, errors="coerce")
    if (
        frame.empty
        or frame.index.isna().any()
        or frame.index.has_duplicates
        or not frame.index.is_monotonic_increasing
    ):
        raise MarketRegimeViewError(f"市场观察索引无效：{instrument}")
    return frame


def _validated_ohlc(frame: pd.DataFrame, *, label: str) -> pd.DataFrame:
    columns = ["open", "high", "low", "close"]
    if not set(columns).issubset(frame.columns):
        raise MarketRegimeViewError(f"{label}缺少 OHLC")
    values = frame.loc[:, columns].apply(pd.to_numeric, errors="coerce")
    finite = values.apply(lambda column: column.map(math.isfinite))
    if values.isna().any().any() or not finite.all().all():
        raise MarketRegimeViewError(f"{label}包含非有限 OHLC")
    if (values <= 0).any().any():
        raise MarketRegimeViewError(f"{label}包含非正 OHLC")
    if (
        values["high"].lt(values[["open", "close", "low"]].max(axis=1)).any()
        or values["low"].gt(values[["open", "close", "high"]].min(axis=1)).any()
    ):
        raise MarketRegimeViewError(f"{label}的 high/low 关系无效")
    return values


def _market_snapshot(frame: pd.DataFrame) -> dict[str, Any]:
    close = _validated_ohlc(frame, label="市场观察行情")["close"]
    latest = close.iloc[-1]
    rolling_high = close.rolling(252, min_periods=1).max().iloc[-1]
    return {
        "date": frame.index[-1].date().isoformat(),
        "close": float(latest),
        "return_5d": _clean_number(close.pct_change(5).iloc[-1]),
        "return_20d": _clean_number(close.pct_change(20).iloc[-1]),
        "drawdown_252d": _clean_number(latest / rolling_high - 1.0),
    }


def _risk_snapshot(frame: pd.DataFrame) -> dict[str, Any]:
    required = {"VIX", "COR1M"}
    if not required.issubset(frame.columns):
        raise MarketRegimeViewError("风险观察缺少 VIX 或 COR1M")
    output: dict[str, Any] = {}
    for name, column in (
        ("vix", "VIX"),
        ("vix9d", "VIX9D"),
        ("vix3m", "VIX3M"),
        ("cor1m", "COR1M"),
    ):
        if column not in frame.columns:
            output[name] = None
            continue
        series = pd.to_numeric(frame[column], errors="coerce").dropna()
        if not series.map(math.isfinite).all():
            raise MarketRegimeViewError(f"风险观察包含非有限 {column}")
        output[name] = (
            {
                "date": series.index[-1].date().isoformat(),
                "value": float(series.iloc[-1]),
            }
            if not series.empty
            else None
        )
        if column in required and output[name] is None:
            raise MarketRegimeViewError(f"风险观察缺少有效 {column}")
    return output


def _load_pit_status(settings: MarketRegimeResearchSettings) -> dict[str, Any]:
    path = settings.raw_root / "pit" / f"{settings.pit.universe}_diagnostics.json"
    if not path.is_file():
        return {
            "status": "MISSING",
            "asof": None,
            "inconsistency_count": None,
            "note": "尚无专属市场宽度 PIT 诊断。",
        }
    payload = _read_object(path)
    quality = str(payload.get("quality_status") or "INVALID")
    inconsistencies = int(payload.get("inconsistency_count") or 0)
    return {
        "status": quality,
        "asof": payload.get("asof"),
        "inconsistency_count": inconsistencies,
        "snapshots": payload.get("snapshots"),
        "minimum_members": payload.get("minimum_members"),
        "maximum_members": payload.get("maximum_members"),
        "note": (
            "PIT 股票池未通过发布门禁，宽度指标尚无正式有效性结论。"
            if quality != "PASS"
            else "PIT 股票池已通过严格发布门禁。"
        ),
    }


def _candidate_payload(screening: ScreeningView) -> dict[str, Any] | None:
    rows = screening.scorecard.loc[
        screening.scorecard["candidate_id"].astype(str).eq(_PRIMARY_CANDIDATE)
    ]
    if rows.empty:
        passed = screening.scorecard.loc[
            screening.scorecard["side"].eq("bottom")
            & screening.scorecard["screening_status"].eq("STAGE_1_PASS")
        ]
        if passed.empty:
            return None
        row = passed.iloc[0]
    else:
        row = rows.iloc[0]
    numeric_fields = (
        "prevalence",
        "average_precision",
        "pr_auc_lift",
        "roc_auc",
        "brier_skill",
        "event_precision",
        "event_recall",
        "false_alarm_episodes_per_year",
        "fdr_q_value",
        "median_signaled_positive_touch_day",
    )
    integer_fields = (
        "signal_episodes",
        "positive_event_episodes",
        "development_event_episodes",
        "walk_forward_folds",
        "oos_rows",
    )
    try:
        horizon = int(row["horizon"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketRegimeViewError("Stage B 候选 horizon 无效") from exc
    if horizon <= 0:
        raise MarketRegimeViewError("Stage B 候选 horizon 无效")
    payload = {
        "candidate_id": str(row["candidate_id"]),
        "feature_name": str(row.get("feature_name") or ""),
        "side": str(row["side"]),
        "horizon": horizon,
        "screening_status": str(row["screening_status"]),
        "production_approved": bool(row["production_approved"]),
        "pending_final_gates": str(row.get("pending_final_gates") or ""),
    }
    payload.update({name: _clean_number(row.get(name)) for name in numeric_fields})
    payload.update(
        {
            name: int(row.get(name)) if not pd.isna(row.get(name)) else None
            for name in integer_fields
        }
    )
    return payload


def _bottom_status_counts(scorecard: pd.DataFrame) -> dict[str, int]:
    rows = scorecard.loc[
        scorecard["side"].eq("bottom")
        & scorecard["hypothesis_tier"].eq("confirmatory")
    ]
    counts = rows["screening_status"].value_counts()
    return {
        "stage_1_pass": int(counts.get("STAGE_1_PASS", 0)),
        "stage_1_fail": int(counts.get("STAGE_1_FAIL", 0)),
        "insufficient": int(counts.get("INSUFFICIENT_EVIDENCE", 0)),
        "total": int(len(rows)),
    }


def market_regime_status_payload(
    settings: MarketRegimeResearchSettings | None = None,
) -> dict[str, Any]:
    """Build the server-rendered status payload without exposing holdout data."""
    config = settings or load_market_regime_research_settings()
    expected = _expected_session()
    errors: list[str] = []

    try:
        stage_a = _load_stage_a(config)
    except Exception as exc:  # noqa: BLE001
        stage_a = {"status": "INVALID", "run_id": None}
        errors.append(str(exc))

    screening: ScreeningView | None = None
    try:
        screening = _load_screening(config)
        candidate = _candidate_payload(screening)
        candidate_predictions = (
            screening.predictions.loc[
                screening.predictions["candidate_id"].eq(
                    candidate["candidate_id"]
                )
            ]
            if candidate
            else pd.DataFrame()
        )
        bottom_counts = _bottom_status_counts(screening.scorecard)
        stage_b = {
            "status": "PASS",
            "screening_id": screening.summary.get("screening_id"),
            "source_research_run_id": screening.summary.get(
                "source_research_run_id"
            ),
            "source_date_end": screening.source.get("date_end"),
            "prediction_start": (
                candidate_predictions["date"].min().date().isoformat()
                if not candidate_predictions.empty
                else None
            ),
            "prediction_end": (
                candidate_predictions["date"].max().date().isoformat()
                if not candidate_predictions.empty
                else None
            ),
            "algorithm_version": screening.summary.get("algorithm_version"),
            "holdout_status": screening.summary.get("holdout_status"),
            "holdout_start": screening.summary.get("holdout_start"),
            "candidate_tests": screening.summary.get("candidate_tests"),
            "confirmatory_tests": screening.summary.get("confirmatory_tests"),
            "production_approved_count": int(
                screening.summary.get("production_approved_count") or 0
            ),
            "registry_version": (
                screening.summary.get("registry") or {}
            ).get("registry_version"),
        }
    except Exception as exc:  # noqa: BLE001
        candidate = None
        bottom_counts = {
            "stage_1_pass": 0,
            "stage_1_fail": 0,
            "insufficient": 0,
            "total": 0,
        }
        stage_b = {"status": "INVALID", "screening_id": None}
        errors.append(str(exc))

    observed: str | None = None
    market: dict[str, Any] = {}
    risk: dict[str, Any] = {}
    source_status = "INVALID"
    try:
        source_manifest = _load_source_manifest(config)
        for key, definition in _INSTRUMENTS.items():
            market[key] = _market_snapshot(
                _load_verified_frame(
                    config,
                    source_manifest,
                    str(definition["symbol"]),
                )
            )
        risk = _risk_snapshot(
            _load_verified_frame(
                config,
                source_manifest,
                "CBOE_RISK_INDEX_BUNDLE",
            )
        )
        observation_dates = [item["date"] for item in market.values()]
        observation_dates.extend(
            risk[key]["date"]
            for key in ("vix", "cor1m")
            if risk.get(key) is not None
        )
        observed = min(observation_dates) if observation_dates else None
        source_status = "PASS"
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))

    delay = _session_delay(expected, observed)
    data_status = (
        "INVALID"
        if source_status != "PASS"
        else "CURRENT"
        if delay == 0
        else "STALE"
    )
    try:
        pit = _load_pit_status(config)
    except Exception as exc:  # noqa: BLE001
        pit = {
            "status": "INVALID",
            "asof": None,
            "inconsistency_count": None,
            "note": "PIT 诊断文件无法通过校验。",
        }
        errors.append(str(exc))
    screening_registry = str(stage_b.get("registry_version") or "")
    v2_status = "READY_TO_SCREEN" if screening_registry.startswith("2.") else "PENDING"
    research_status = (
        "STAGE_1_CANDIDATE"
        if candidate and candidate["screening_status"] == "STAGE_1_PASS"
        else "NO_STAGE_1_CANDIDATE"
    )
    return {
        "status": "RESEARCH_ONLY",
        "research_status": research_status,
        "message": "已有阶段性底部候选，但当前没有获准生产的实时信号。",
        "expected_session": expected,
        "observed_session": observed,
        "data_delay_sessions": delay,
        "data_status": data_status,
        "source_integrity": source_status,
        "stage_a": stage_a,
        "stage_b": stage_b,
        "candidate": candidate,
        "bottom_counts": bottom_counts,
        "market": market,
        "risk": risk,
        "pit": pit,
        "current_signal": {
            "status": "NOT_RUNNING",
            "probability": None,
            "note": "G7-G9 和每日影子评分尚未完成，禁止推算今日底部概率。",
        },
        "pipeline": [
            {
                "name": "核心特征与标签",
                "status": "COMPLETE" if stage_a.get("status") == "PASS" else "BLOCKED",
                "note": "1990 年以来市场核心特征与 first-touch 标签。",
            },
            {
                "name": "G1-G6 有效性筛选",
                "status": "COMPLETE" if stage_b.get("status") == "PASS" else "BLOCKED",
                "note": "旧版 core-only 候选完成 walk-forward、bootstrap 与 FDR。",
            },
            {
                "name": "COR1M 与 PIT 市场宽度 v2",
                "status": (
                    "BLOCKED" if pit.get("status") != "PASS" else v2_status
                ),
                "note": pit.get("note"),
            },
            {
                "name": "G7 参数扰动",
                "status": "PENDING",
                "note": "尚未冻结并验证 3/5/10 日等邻近定义。",
            },
            {
                "name": "G8 增量信息",
                "status": "PENDING",
                "note": "尚未证明候选相对价格状态基线的独立增量。",
            },
            {
                "name": "G9 经济价值",
                "status": "PENDING",
                "note": "尚未用 next-open、滑点和 IBKR 费用进行交易检验。",
            },
            {
                "name": "G10 影子运行",
                "status": "NOT_STARTED",
                "note": "每日实时评分尚未启动，生产批准数仍为 0。",
            },
        ],
        "periods": [
            {"id": key, "label": value["label"]}
            for key, value in _PERIODS.items()
        ],
        "instruments": [
            {"id": key, "label": value["label"]}
            for key, value in _INSTRUMENTS.items()
        ],
        "errors": errors,
    }


def _episode_rows(
    predictions: pd.DataFrame,
    *,
    sessions: pd.DatetimeIndex,
    value_column: str,
    horizon: int,
) -> list[dict[str, Any]]:
    selected = predictions.loc[predictions[value_column].fillna(False).astype(bool)].copy()
    if selected.empty:
        return []
    selected["date"] = pd.to_datetime(selected["date"], errors="raise")
    selected = selected.sort_values("date", kind="stable").reset_index(drop=True)
    positions = sessions.get_indexer(pd.DatetimeIndex(selected["date"]))
    if (positions < 0).any():
        raise MarketRegimeViewError("预测日期不在行情交易日中")
    groups: list[list[int]] = [[0]]
    for index in range(1, len(selected)):
        if positions[index] - positions[index - 1] > horizon:
            groups.append([index])
        else:
            groups[-1].append(index)

    output: list[dict[str, Any]] = []
    for number, indexes in enumerate(groups, start=1):
        group = selected.iloc[indexes]
        if value_column == "signal":
            probabilities = pd.to_numeric(
                group["model_probability"], errors="coerce"
            )
            marker_index = probabilities.idxmax()
        else:
            marker_index = group.index[0]
        marker = selected.loc[marker_index]
        positive = group.loc[pd.to_numeric(group["actual"], errors="coerce").eq(1)]
        touch = pd.to_numeric(positive["touch_day"], errors="coerce").dropna()
        output.append(
            {
                "episode": number,
                "start_date": group["date"].iloc[0].date().isoformat(),
                "end_date": group["date"].iloc[-1].date().isoformat(),
                "date": pd.Timestamp(marker["date"]).date().isoformat(),
                "feature_value": _clean_number(marker.get("feature_value")),
                "model_probability": _clean_number(
                    marker.get("model_probability")
                ),
                "baseline_probability": _clean_number(
                    marker.get("baseline_probability")
                ),
                "outcome": bool(not positive.empty),
                "touch_day": int(touch.min()) if not touch.empty else None,
                "rows": int(len(group)),
            }
        )
    return output


def market_regime_chart_payload(
    *,
    instrument: str = "spx",
    period: str = "recent",
    settings: MarketRegimeResearchSettings | None = None,
) -> dict[str, Any]:
    """Return verified OHLC and development-only marker episodes for Plotly."""
    instrument_id = str(instrument).strip().lower()
    period_id = str(period).strip().lower()
    if instrument_id not in _INSTRUMENTS:
        raise ValueError("instrument must be spx or ndx")
    if period_id not in _PERIODS:
        raise ValueError("unknown chart period")
    config = settings or load_market_regime_research_settings()
    manifest = _load_source_manifest(config)
    definition = _INSTRUMENTS[instrument_id]
    prices = _load_verified_frame(
        config,
        manifest,
        str(definition["symbol"]),
    )
    prices = _validated_ohlc(prices, label="图表行情")
    period_definition = _PERIODS[period_id]
    if period_definition["kind"] == "tail":
        visible = prices.tail(int(period_definition["sessions"]))
    else:
        visible = prices.loc[
            str(period_definition["start"]):str(period_definition["end"])
        ]
    if visible.empty:
        raise MarketRegimeViewError("所选区间没有行情")

    screening = _load_screening(config)
    candidate = _candidate_payload(screening)
    predictions = screening.predictions.loc[
        screening.predictions["candidate_id"].astype(str).eq(
            candidate["candidate_id"] if candidate else _PRIMARY_CANDIDATE
        )
    ].copy()
    horizon = int(candidate["horizon"] if candidate else 5)
    signal_episodes = _episode_rows(
        predictions,
        sessions=pd.DatetimeIndex(prices.index),
        value_column="signal",
        horizon=horizon,
    )
    outcome_episodes = _episode_rows(
        predictions,
        sessions=pd.DatetimeIndex(prices.index),
        value_column="actual",
        horizon=horizon,
    )
    start = visible.index.min().date().isoformat()
    end = visible.index.max().date().isoformat()

    def in_window(item: Mapping[str, Any]) -> bool:
        return start <= str(item["date"]) <= end

    candles = [
        {
            "date": date.date().isoformat(),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
        }
        for date, row in visible[["open", "high", "low", "close"]].iterrows()
    ]
    return {
        "instrument": instrument_id,
        "instrument_label": definition["label"],
        "period": period_id,
        "period_label": period_definition["label"],
        "date_start": start,
        "date_end": end,
        "candles": candles,
        "signal_episodes": [item for item in signal_episodes if in_window(item)],
        "outcome_episodes": [item for item in outcome_episodes if in_window(item)],
        "marker_contract": {
            "signal": "当日可知的样本外 Stage 1 候选报警",
            "outcome": "使用未来路径计算的事后评估标签，不是实时信号",
            "holdout_included": False,
        },
    }


__all__ = [
    "MarketRegimeViewError",
    "market_regime_chart_payload",
    "market_regime_status_payload",
]
