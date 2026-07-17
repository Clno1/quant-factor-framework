"""Read-only Stage-1 Web and API adapter for group analytics.

This module reads only immutable artifacts through :class:`ArtifactReader`.
It intentionally imports neither data providers nor the computation service;
refresh and publication remain CLI/systemd responsibilities.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Mapping
from uuid import uuid4

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from src.group_analytics import ALGORITHM_VERSION, SCHEMA_VERSION
from src.group_analytics.artifacts import (
    ArtifactReader,
    ArtifactValidationError,
    LoadedArtifactRun,
    normalize_json_value,
)
from src.group_analytics.calendar import (
    latest_completed_session,
    official_session_close,
)
from src.group_analytics.models import (
    ArtifactCombination,
    ArtifactNotFoundError,
    GroupAnalyticsError,
    NoSuccessfulRunError,
    ReasonCode,
    sorted_reason_codes,
)
from src.group_analytics.settings import (
    GroupAnalyticsSettings,
    load_group_analytics_settings,
)
from src.utils.logger import get_logger


log = get_logger(__name__)
_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
router = APIRouter()

# A single read-only facade is easy to replace with a fixture reader in tests.
settings: GroupAnalyticsSettings = load_group_analytics_settings()
_READER = ArtifactReader(settings)

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_ALLOWED_UNIVERSES = ("SP500",)
_ALLOWED_TAXONOMIES = ("FMP",)
_ALLOWED_LEVELS = ("sector", "sub_industry")
_ALLOWED_MODES = ("eod",)
_ALLOWED_ASOF = ("latest",)
_ALLOWED_RETURN_METHODS = ("ROBUST_EW",)
_HEAT_SORT_FIELDS = (
    "robust_ew_return_1d",
    "up_pct",
    "n_valid",
    "group_name",
)
_MEMBER_SORT_FIELDS = (
    "ticker",
    "raw_return_1d",
    "headline_contribution",
    "is_valid_for_headline",
)
_DIAGNOSTIC_TYPES = (
    "missing_members",
    "low_confidence_groups",
    "classification_diagnostics",
)
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_SENSITIVE_DIAGNOSTIC_KEYS = {
    "path",
    "provenance_path",
    "source_path",
    "input_path",
    "input_paths",
    "raw_ohlcv_root",
    "token",
    "api_key",
    "secret",
}


class _APIContractError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = dict(details or {})


def _request_id(request: Request) -> str:
    supplied = request.headers.get("x-request-id", "")
    if _REQUEST_ID_RE.fullmatch(supplied):
        return supplied
    return f"req_{uuid4().hex}"


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": normalize_json_value(dict(details or {})),
                "request_id": _request_id(request),
            }
        },
    )


def _api_call(request: Request, operation: Callable[[], Any]) -> JSONResponse:
    try:
        payload = operation()
        return JSONResponse(content=normalize_json_value(payload))
    except _APIContractError as exc:
        return _error_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
        )
    except ArtifactNotFoundError as exc:
        return _error_response(
            request,
            status_code=404,
            code="ARTIFACT_NOT_FOUND",
            message="Requested artifact was not found",
            details=exc.details,
        )
    except NoSuccessfulRunError as exc:
        return _error_response(
            request,
            status_code=404,
            code="NO_SUCCESSFUL_RUN",
            message="No successful run exists for this combination",
            details=exc.details,
        )
    except ArtifactValidationError:
        request_id = _request_id(request)
        log.exception("group analytics artifact validation failed request_id=%s", request_id)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "ARTIFACT_VALIDATION_FAILED",
                    "message": "Published artifact validation failed",
                    "details": {},
                    "request_id": request_id,
                }
            },
        )
    except GroupAnalyticsError as exc:
        request_id = _request_id(request)
        log.exception(
            "group analytics read failed code=%s request_id=%s", exc.code, request_id
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": exc.code,
                    "message": "Unable to read group analytics artifacts",
                    "details": {},
                    "request_id": request_id,
                }
            },
        )
    except Exception:  # never expose paths, tokens, or traceback to clients
        request_id = _request_id(request)
        log.exception("unexpected group analytics API error request_id=%s", request_id)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Internal server error",
                    "details": {},
                    "request_id": request_id,
                }
            },
        )


def _validate_query_keys(request: Request, allowed: set[str]) -> None:
    unknown = sorted(set(request.query_params.keys()).difference(allowed))
    duplicate = sorted(
        key for key in set(request.query_params.keys())
        if len(request.query_params.getlist(key)) > 1
    )
    if unknown or duplicate:
        raise _APIContractError(
            422,
            "INVALID_REQUEST",
            "Query parameters failed validation",
            details={
                "unknown_parameters": unknown,
                "duplicate_parameters": duplicate,
                "allowed_parameters": sorted(allowed),
            },
        )


def _choice(value: str, *, field: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise _APIContractError(
            422,
            "INVALID_REQUEST",
            f"Invalid {field}",
            details={"field": field, "value": value, "allowed_values": list(allowed)},
        )
    return value


def _integer(
    value: str | int | None,
    *,
    field: str,
    default: int | None,
    minimum: int,
    maximum: int,
) -> int | None:
    if value is None:
        return default
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise _APIContractError(
            422,
            "INVALID_REQUEST",
            f"Invalid {field}",
            details={"field": field, "value": value},
        ) from exc
    if not minimum <= parsed <= maximum:
        raise _APIContractError(
            422,
            "INVALID_REQUEST",
            f"Invalid {field}",
            details={
                "field": field,
                "value": parsed,
                "minimum": minimum,
                "maximum": maximum,
            },
        )
    return parsed


def _boolean(value: str | bool, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).casefold()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise _APIContractError(
        422,
        "INVALID_REQUEST",
        f"Invalid {field}",
        details={"field": field, "value": value, "allowed_values": ["true", "false"]},
    )


def _safe_run_id(value: str, *, field: str = "data_run_id") -> str:
    if not _RUN_ID_RE.fullmatch(value) or value in {".", ".."}:
        raise _APIContractError(
            422,
            "INVALID_REQUEST",
            f"Invalid {field}",
            details={"field": field, "value": value},
        )
    return value


def _safe_group_id(value: str) -> str:
    if not _GROUP_ID_RE.fullmatch(value) or value in {".", ".."}:
        raise _APIContractError(
            422,
            "INVALID_REQUEST",
            "Invalid group_id",
            details={"field": "group_id", "value": value},
        )
    return value


def _combination(
    *,
    universe: str,
    taxonomy: str,
    level: str,
    mode: str,
    asof: str,
    benchmark: str,
    return_method: str,
) -> ArtifactCombination:
    _choice(universe, field="universe", allowed=_ALLOWED_UNIVERSES)
    _choice(taxonomy, field="taxonomy", allowed=_ALLOWED_TAXONOMIES)
    _choice(level, field="level", allowed=_ALLOWED_LEVELS)
    _choice(mode, field="mode", allowed=_ALLOWED_MODES)
    _choice(asof, field="asof", allowed=_ALLOWED_ASOF)
    _choice(benchmark, field="benchmark", allowed=(settings.benchmark,))
    _choice(return_method, field="return_method", allowed=_ALLOWED_RETURN_METHODS)
    return ArtifactCombination(universe, taxonomy, level, mode).normalized()


def _strict_json_object(path: Path) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle, parse_constant=reject_constant)
    except FileNotFoundError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise ArtifactValidationError("Invalid last_attempt pointer") from exc
    if not isinstance(value, dict):
        raise ArtifactValidationError("last_attempt pointer must be an object")
    return value


def _last_attempt(
    reader: ArtifactReader,
    combination: ArtifactCombination,
) -> dict[str, Any] | None:
    path = reader.paths.combo_dir(combination) / "last_attempt.json"
    if not path.is_file():
        return None
    value = _strict_json_object(path)
    run_id = str(value.get("run_id") or "")
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ArtifactValidationError("last_attempt contains invalid run_id")
    if value.get("combination") != combination.normalized().as_dict():
        raise ArtifactValidationError("last_attempt combination mismatch")
    if value.get("last_attempt_status") not in {"RUNNING", "SUCCESS", "FAILED"}:
        raise ArtifactValidationError("last_attempt contains invalid status")
    return value


def _load_fixed_run(
    reader: ArtifactReader,
    combination: ArtifactCombination,
    *,
    data_run_id: str | None,
) -> tuple[LoadedArtifactRun, dict[str, Any] | None]:
    last = _last_attempt(reader, combination)
    if data_run_id:
        run_id = _safe_run_id(data_run_id)
        loaded = reader.load_run(run_id)
        if loaded.combination.as_dict() != combination.normalized().as_dict():
            raise _APIContractError(
                404,
                "UNSUPPORTED_COMBINATION",
                "The immutable run does not match the requested combination",
                details={
                    "requested": combination.normalized().as_dict(),
                    "actual": loaded.combination.as_dict(),
                    "data_run_id": run_id,
                },
            )
        return loaded, last

    try:
        return reader.load_latest(combination), last
    except NoSuccessfulRunError as exc:
        if last and last.get("last_attempt_status") == "FAILED":
            raise _APIContractError(
                503,
                "NO_SUCCESSFUL_RUN",
                "No successful run exists and the latest attempt failed",
                details={
                    "combination": combination.normalized().as_dict(),
                    "last_attempt_run_id": last.get("run_id"),
                    "last_attempt_status": "FAILED",
                    "error_code": last.get("error_code"),
                },
            ) from exc
        raise _APIContractError(
            404,
            "UNSUPPORTED_COMBINATION",
            "Requested precomputed combination is unavailable",
            details={
                "combination": combination.normalized().as_dict(),
                "enabled_combinations": [
                    {
                        "universe": item.get("universe"),
                        "taxonomy": item.get("taxonomy"),
                        "level": item.get("level"),
                        "mode": item.get("mode"),
                    }
                    for item in reader.scan_metadata(strict=False)
                ],
            },
        ) from exc


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _reason_codes(value: Any) -> list[str]:
    if value is None or value is pd.NA:
        return []
    if isinstance(value, str):
        return sorted_reason_codes([value])
    if isinstance(value, np.ndarray):
        value = value.tolist()
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted_reason_codes(list(value))
    try:
        if bool(pd.isna(value)):
            return []
    except (TypeError, ValueError):
        pass
    raise ArtifactValidationError("reason_codes has invalid artifact type")


def _ranked_metrics(metrics: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = metrics.copy(deep=True).reset_index(drop=True)
    if "group_id" not in rows or "eligible_for_ranking" not in rows:
        raise ArtifactValidationError("daily metrics lacks ranking fields")
    rows["group_id"] = rows["group_id"].astype(str)
    rows["headline_rank"] = pd.Series(pd.NA, index=rows.index, dtype="Int64")
    robust = pd.to_numeric(rows.get("robust_ew_return_1d"), errors="coerce")
    eligible = rows["eligible_for_ranking"].fillna(False).astype(bool)
    eligible &= robust.notna() & np.isfinite(robust)
    ranked = rows.loc[eligible].sort_values(
        ["robust_ew_return_1d", "up_pct", "n_valid", "group_id"],
        ascending=[False, False, False, True],
        kind="mergesort",
        na_position="last",
    )
    for position, index in enumerate(ranked.index, start=1):
        rows.at[index, "headline_rank"] = position
    ranked = rows.loc[ranked.index].copy()
    return rows, ranked


def _driver_maps(contributions: pd.DataFrame) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    required = {"group_id", "ticker", "return_method", "contribution"}
    if contributions.empty or not required.issubset(contributions.columns):
        return {}, {}
    frame = contributions.loc[
        contributions["return_method"].astype(str).eq("ROBUST_EW")
    ].copy()
    frame["contribution"] = pd.to_numeric(frame["contribution"], errors="coerce")
    frame = frame.loc[frame["contribution"].notna() & np.isfinite(frame["contribution"])]
    if frame.empty:
        return {}, {}
    frame["group_id"] = frame["group_id"].astype(str)
    frame["ticker"] = frame["ticker"].astype(str)
    if "security_id" not in frame:
        frame["security_id"] = ""
    positive: dict[str, dict[str, Any]] = {}
    negative: dict[str, dict[str, Any]] = {}
    for group_id, group in frame.groupby("group_id", sort=True):
        top = group.sort_values(
            ["contribution", "ticker", "security_id"],
            ascending=[False, True, True],
            kind="mergesort",
        ).iloc[0]
        bottom = group.sort_values(
            ["contribution", "ticker", "security_id"],
            ascending=[True, True, True],
            kind="mergesort",
        ).iloc[0]
        positive[group_id] = {
            "ticker": str(top["ticker"]),
            "contribution": float(top["contribution"]),
        }
        negative[group_id] = {
            "ticker": str(bottom["ticker"]),
            "contribution": float(bottom["contribution"]),
        }
    return positive, negative


def _metric_records(frame: pd.DataFrame, loaded: LoadedArtifactRun) -> list[dict[str, Any]]:
    top_drivers, bottom_drivers = _driver_maps(loaded.contributions)
    records: list[dict[str, Any]] = []
    for raw in frame.to_dict(orient="records"):
        row = dict(raw)
        group_id = str(row.get("group_id") or "")
        row["reason_codes"] = _reason_codes(row.get("reason_codes"))
        row.setdefault("cap_return_1d", None)
        row.setdefault("cap_type", "UNAVAILABLE")
        row.setdefault("cap_status", "UNAVAILABLE")
        row.setdefault("cap_availability_coverage", None)
        row.setdefault("cap_return_coverage", None)
        row.setdefault("cap_n_effective", None)
        row["top_driver"] = top_drivers.get(group_id) or (
            {"ticker": row.get("top_driver_ticker"), "contribution": None}
            if row.get("top_driver_ticker")
            else None
        )
        row["bottom_driver"] = bottom_drivers.get(group_id) or (
            {"ticker": row.get("bottom_driver_ticker"), "contribution": None}
            if row.get("bottom_driver_ticker")
            else None
        )
        records.append(normalize_json_value(row))
    return records


def _quality_summary(metrics: pd.DataFrame) -> tuple[dict[str, Any], str]:
    expected = pd.to_numeric(metrics.get("n_expected"), errors="coerce").fillna(0)
    valid = pd.to_numeric(metrics.get("n_valid"), errors="coerce").fillna(0)
    n_expected = int(expected.sum())
    n_valid = int(valid.sum())
    coverage = None if n_expected == 0 else n_valid / n_expected
    ranked = int(metrics.get("eligible_for_ranking", pd.Series(dtype=bool)).fillna(False).astype(bool).sum())
    if n_expected == 0 or n_valid == 0:
        status = "NO_DATA"
    elif coverage is not None and coverage < settings.inputs.min_return_coverage:
        status = "LOW_COVERAGE"
    else:
        status = "OK"
    return {
        "n_expected": n_expected,
        "n_valid": n_valid,
        "count_coverage": coverage,
        "n_groups_expected": int(len(metrics)),
        "n_groups_ranked": ranked,
        "n_groups_low_confidence": int(len(metrics) - ranked),
    }, status


def _run_state(
    loaded: LoadedArtifactRun,
    last: dict[str, Any] | None,
) -> dict[str, Any]:
    manifest = loaded.manifest
    run = loaded.run
    last_run_id = str((last or {}).get("run_id") or loaded.run_id)
    last_status = str((last or {}).get("last_attempt_status") or "SUCCESS")
    reason_codes = _reason_codes(run.get("reason_codes"))
    if last_status == "FAILED":
        reason_codes.append(ReasonCode.FAILED_LAST_ATTEMPT)
    freshness = str(
        run.get("freshness_status")
        or manifest.get("freshness_status")
        or loaded.diagnostics.get("freshness_status")
        or "FRESH"
    )
    if freshness not in {"FRESH", "DELAYED", "STALE"}:
        raise ArtifactValidationError("artifact freshness_status is invalid")
    freshness = _derive_eod_freshness(
        manifest,
        freshness,
        mode=loaded.combination.mode,
    )
    # A failed newer attempt means the pinned success is no longer the latest
    # verified input.  Downgrade FRESH to DELAYED; the session/SLA calculation
    # above (or an already-persisted status) may have made it STALE already.
    if last_status == "FAILED" and freshness == "FRESH":
        freshness = "DELAYED"
    quality_summary, derived_quality = _quality_summary(loaded.metrics)
    artifact_quality = manifest.get("quality_summary")
    if isinstance(artifact_quality, dict):
        quality_summary = {**quality_summary, **artifact_quality}
    quality = str(
        run.get("quality_status")
        or manifest.get("quality_status")
        or loaded.diagnostics.get("quality_status")
        or derived_quality
    )
    if quality not in {"OK", "LOW_COVERAGE", "NO_DATA"}:
        raise ArtifactValidationError("artifact quality_status is invalid")
    return {
        "last_attempt_run_id": last_run_id,
        "last_attempt_status": last_status,
        "freshness_status": freshness,
        "quality_status": quality,
        "reason_codes": sorted_reason_codes(reason_codes),
        "quality_summary": quality_summary,
    }


def _derive_eod_freshness(
    manifest: Mapping[str, Any],
    persisted: str,
    *,
    mode: str,
    now: pd.Timestamp | None = None,
    calendar: Any | None = None,
) -> str:
    """Downgrade an old EOD artifact against the exchange-session clock.

    Natural elapsed time over weekends and exchange holidays is intentionally
    ignored: an artifact remains fresh while its ``asof`` is the latest
    completed XNYS session.  After a newer session closes, the old artifact is
    DELAYED during the configured publication SLA and STALE afterwards.
    Calendar failures never make an otherwise readable artifact unavailable.
    """

    severity = {"FRESH": 0, "DELAYED": 1, "STALE": 2}
    if persisted not in severity or str(mode).lower() != "eod":
        return persisted
    asof_value = manifest.get("asof")
    if asof_value is None:
        return persisted
    try:
        artifact_session = pd.Timestamp(asof_value).tz_localize(None).normalize()
        now_utc = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
        if now_utc.tzinfo is None:
            now_utc = now_utc.tz_localize("UTC")
        else:
            now_utc = now_utc.tz_convert("UTC")
        latest = latest_completed_session(now=now_utc, calendar=calendar)
        if artifact_session >= latest:
            return persisted
        latest_close = official_session_close(latest, calendar=calendar)
        age_minutes = max(
            0.0,
            (now_utc - latest_close).total_seconds() / 60.0,
        )
        derived = (
            "STALE"
            if age_minutes > settings.freshness.eod_publish_sla_minutes
            else "DELAYED"
        )
        return max((persisted, derived), key=lambda value: severity[value])
    except Exception:  # calendar availability must not break artifact reads
        return persisted


def _sort_all_view(frame: pd.DataFrame, *, sort_by: str, sort_order: str) -> pd.DataFrame:
    ascending = sort_order == "asc"
    rows = frame.copy()
    if sort_by == "robust_ew_return_1d":
        return rows.sort_values(
            [sort_by, "up_pct", "n_valid", "group_id"],
            ascending=[ascending, False, False, True],
            kind="mergesort",
            na_position="last",
        )
    if sort_by == "group_name":
        rows["__group_name_sort"] = rows["group_name"].fillna("").astype(str).str.casefold()
        rows = rows.sort_values(
            ["__group_name_sort", "group_id"],
            ascending=[ascending, True],
            kind="mergesort",
        )
        return rows.drop(columns="__group_name_sort")
    return rows.sort_values(
        [sort_by, "group_id"],
        ascending=[ascending, True],
        kind="mergesort",
        na_position="last",
    )


def _heat_payload(
    loaded: LoadedArtifactRun,
    last: dict[str, Any] | None,
    *,
    view: str,
    sort_by: str,
    sort_order: str,
    view_min_members: int,
    show_low_confidence: bool,
    limit: int | None,
) -> dict[str, Any]:
    all_rows, ranked = _ranked_metrics(loaded.metrics)
    ranked = ranked.loc[
        pd.to_numeric(ranked["n_valid"], errors="coerce").fillna(0)
        >= view_min_members
    ]
    if view == "top":
        count = min(settings.ranking.top_n, len(ranked) // 2)
        selected = ranked.head(count).copy()
    elif view == "bottom":
        count = min(settings.ranking.bottom_n, len(ranked) // 2)
        selected = ranked.tail(count).iloc[::-1].copy()
    else:
        selected = all_rows.loc[
            pd.to_numeric(all_rows["n_valid"], errors="coerce").fillna(0)
            >= view_min_members
        ]
        if not show_low_confidence:
            selected = selected.loc[
                selected["eligible_for_ranking"].fillna(False).astype(bool)
            ]
        selected = _sort_all_view(
            selected,
            sort_by=sort_by,
            sort_order=sort_order,
        )
    selected = selected.loc[
        pd.to_numeric(selected["n_valid"], errors="coerce").fillna(0)
        >= view_min_members
    ]
    if limit is not None:
        selected = selected.head(limit)
    selected = selected.copy().reset_index(drop=True)
    selected["view_rank"] = pd.Series(range(1, len(selected) + 1), dtype="Int64")

    state = _run_state(loaded, last)
    manifest = loaded.manifest
    methodology = {
        "headline_method": "ROBUST_EW",
        "driver_method": "ROBUST_EW",
        "counting_unit": manifest.get("counting_unit") or settings.classification.counting_unit,
        "issuer_dedupe_status": manifest.get("issuer_dedupe_status") or "NONE",
        "issuer_overrides_applied": bool(manifest.get("issuer_overrides_applied", False)),
        "issuer_override_count": int(manifest.get("issuer_override_count") or 0),
        "issuer_override_version": manifest.get("issuer_override_version"),
        "pit_universe_applied": bool(manifest.get("pit_universe_applied", False)),
        "pit_classification_applied": bool(manifest.get("pit_classification_applied", False)),
    }
    benchmark_values = pd.to_numeric(
        all_rows.get("benchmark_return_1d", pd.Series(dtype=float)),
        errors="coerce",
    )
    benchmark_values = benchmark_values.loc[
        benchmark_values.notna() & np.isfinite(benchmark_values)
    ]
    benchmark_return = (
        None if benchmark_values.empty else float(benchmark_values.iloc[0])
    )
    return {
        "schema_version": manifest.get("schema_version") or SCHEMA_VERSION,
        "algorithm_version": manifest.get("algorithm_version") or ALGORITHM_VERSION,
        "data_run_id": loaded.run_id,
        **state,
        "parameter_hash": manifest.get("parameter_hash"),
        "generated_at": manifest.get("generated_at"),
        "asof": manifest.get("asof") or loaded.run.get("asof"),
        "snapshot_time": manifest.get("snapshot_time"),
        "source_max_date": manifest.get("source_max_date") or manifest.get("asof"),
        "session_status": "FINAL",
        "mode": loaded.combination.mode,
        "universe": loaded.combination.universe,
        "universe_version": manifest.get("universe_version"),
        "taxonomy": loaded.combination.taxonomy,
        "taxonomy_level": loaded.combination.level,
        "taxonomy_version": manifest.get("taxonomy_version"),
        "group_id_mapping_version": manifest.get("group_id_mapping_version"),
        "benchmark": manifest.get("benchmark") or settings.benchmark,
        "benchmark_return_1d": benchmark_return,
        "methodology": methodology,
        "sort": {
            "sort_by": sort_by,
            "sort_order": sort_order,
            "view": view,
        },
        "rows": _metric_records(selected, loaded),
    }


def _metadata_payload(reader: ArtifactReader) -> dict[str, Any]:
    value = reader.metadata(strict=True)
    combinations: list[dict[str, Any]] = []
    for raw in value.get("available_combinations", []):
        item = dict(raw)
        item["benchmarks"] = [settings.benchmark]
        item["return_methods"] = ["ROBUST_EW"]
        item["sort_fields"] = list(_HEAT_SORT_FIELDS)
        combinations.append(item)
    value["available_combinations"] = combinations
    value["member_sort_fields"] = list(_MEMBER_SORT_FIELDS)
    return value


def _detail_payload(
    loaded: LoadedArtifactRun,
    *,
    group_id: str,
    page: int,
    page_size: int,
    member_sort_by: str,
    member_sort_order: str,
) -> dict[str, Any]:
    all_metrics, _ = _ranked_metrics(loaded.metrics)
    summary_frame = all_metrics.loc[all_metrics["group_id"].astype(str).eq(group_id)]
    if summary_frame.empty:
        raise _APIContractError(
            404,
            "GROUP_NOT_FOUND",
            "The requested group does not exist in this immutable run",
            details={"group_id": group_id, "data_run_id": loaded.run_id},
        )
    if len(summary_frame) != 1:
        raise ArtifactValidationError("daily metrics contains duplicate group_id")
    members = loaded.members.loc[
        loaded.members["group_id"].astype(str).eq(group_id)
    ].copy()
    summary = _metric_records(summary_frame, loaded)[0]
    expected = int(summary.get("n_expected") or 0)
    if len(members) != expected:
        raise ArtifactValidationError("detail member total does not equal n_expected")
    if member_sort_by not in members.columns:
        raise ArtifactValidationError("member artifact lacks an allowed sort field")
    if "ticker" not in members:
        raise ArtifactValidationError("member artifact lacks ticker")
    if "security_id" not in members:
        raise ArtifactValidationError("member artifact lacks security_id")
    members = members.sort_values(
        [member_sort_by, "ticker", "security_id"],
        ascending=[member_sort_order == "asc", True, True],
        kind="mergesort",
        na_position="last",
    )
    total = len(members)
    start = (page - 1) * page_size
    paged = members.iloc[start : start + page_size]
    member_rows: list[dict[str, Any]] = []
    for raw in paged.to_dict(orient="records"):
        row = dict(raw)
        row["reason_codes"] = _reason_codes(row.get("reason_codes"))
        row.setdefault("counting_unit_id", f"security:{row.get('ticker', '')}")
        row.setdefault("issuer_id", None)
        row.setdefault("membership_valid_from", None)
        row.setdefault("membership_valid_to", None)
        row.setdefault("t_1_weight", None)
        row.setdefault("theme_exposure", None)
        row.setdefault("quote_timestamp", None)
        row.setdefault("data_asof", row.get("date") or loaded.manifest.get("asof"))
        if "contribution_bps" not in row:
            contribution = _finite(row.get("headline_contribution"))
            row["contribution_bps"] = None if contribution is None else 10_000 * contribution
        member_rows.append(normalize_json_value(row))

    contribution_frame = loaded.contributions.copy()
    if not contribution_frame.empty:
        contribution_frame = contribution_frame.loc[
            contribution_frame["group_id"].astype(str).eq(group_id)
            & contribution_frame["return_method"].astype(str).eq("ROBUST_EW")
        ].copy()
        contribution_frame["contribution"] = pd.to_numeric(
            contribution_frame["contribution"], errors="coerce"
        )
        contribution_frame = contribution_frame.loc[
            contribution_frame["contribution"].notna()
            & np.isfinite(contribution_frame["contribution"])
        ]

    def contribution_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
        fields = (
            "security_id",
            "ticker",
            "weight",
            "input_return",
            "contribution",
            "contribution_bps",
        )
        records: list[dict[str, Any]] = []
        for raw in frame.head(5).to_dict(orient="records"):
            records.append(
                normalize_json_value({key: raw.get(key) for key in fields})
            )
        return records

    positive = contribution_frame.loc[
        contribution_frame.get("contribution", pd.Series(dtype=float)).gt(0)
    ].sort_values(
        ["contribution", "ticker", "security_id"],
        ascending=[False, True, True],
        kind="mergesort",
    )
    negative = contribution_frame.loc[
        contribution_frame.get("contribution", pd.Series(dtype=float)).lt(0)
    ].sort_values(
        ["contribution", "ticker", "security_id"],
        ascending=[True, True, True],
        kind="mergesort",
    )

    winsorized = loaded.members.loc[
        loaded.members["group_id"].astype(str).eq(group_id)
        & loaded.members.get("was_winsorized", pd.Series(False, index=loaded.members.index))
        .fillna(False)
        .astype(bool)
    ]

    def unique_finite(column: str) -> float | None:
        if column not in members.columns:
            return None
        values = pd.to_numeric(members[column], errors="coerce")
        values = values.loc[values.notna() & np.isfinite(values)]
        return None if values.empty else float(values.iloc[0])

    return {
        "schema_version": loaded.manifest.get("schema_version") or SCHEMA_VERSION,
        "algorithm_version": loaded.manifest.get("algorithm_version") or ALGORITHM_VERSION,
        "data_run_id": loaded.run_id,
        "asof": loaded.manifest.get("asof") or loaded.run.get("asof"),
        "snapshot_time": loaded.manifest.get("snapshot_time"),
        "provenance": {
            "universe": loaded.combination.universe,
            "universe_version": loaded.manifest.get("universe_version"),
            "taxonomy": loaded.combination.taxonomy,
            "taxonomy_level": loaded.combination.level,
            "taxonomy_version": loaded.manifest.get("taxonomy_version"),
            "classification_asof": loaded.manifest.get("classification_asof"),
            "classification_hash": loaded.manifest.get("classification_hash"),
            "classification_provider": loaded.manifest.get("classification_provider"),
            "group_id_mapping_version": loaded.manifest.get("group_id_mapping_version"),
            "fallback": loaded.manifest.get("fallback"),
            "fetched_at": loaded.manifest.get("fetched_at"),
            "pit_universe_applied": bool(
                loaded.manifest.get("pit_universe_applied", False)
            ),
            "pit_classification_applied": bool(
                loaded.manifest.get("pit_classification_applied", False)
            ),
        },
        "summary": summary,
        "methodology": {"headline_method": "ROBUST_EW", "driver_method": "ROBUST_EW"},
        "contribution_drivers": {
            "top_positive": contribution_records(positive),
            "top_negative": contribution_records(negative),
        },
        "distribution": {
            "median_return_1d": _finite(summary.get("median_return_1d")),
            "dispersion_mad": _finite(summary.get("dispersion_mad")),
            "dispersion_std": _finite(summary.get("dispersion_std")),
            "winsor_lower": unique_finite("winsor_lower"),
            "winsor_upper": unique_finite("winsor_upper"),
            "n_winsorized": int(len(winsorized)),
            "winsorized_tickers": sorted(
                winsorized.get("ticker", pd.Series(dtype=str)).astype(str).tolist()
            ),
        },
        "members": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_next": start + page_size < total,
            "rows": member_rows,
        },
    }


def _run_payload(
    attempt: dict[str, Any],
    *,
    diagnostic_type: str | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    diagnostics = attempt.get("diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    error = attempt.get("error")
    safe_error = None
    if isinstance(error, dict):
        summary_value = str(error.get("summary") or error.get("message") or "")
        lowered_summary = summary_value.casefold()
        if (
            "/" in summary_value
            or "\\" in summary_value
            or "token" in lowered_summary
            or "traceback" in lowered_summary
        ):
            summary_value = "Run failed; inspect server-side diagnostics"
        elif len(summary_value) > 500:
            summary_value = summary_value[:497] + "..."
        safe_error = {
            "code": error.get("code"),
            "stage": error.get("stage"),
            "summary": summary_value or None,
        }
    counts = attempt.get("diagnostic_counts")
    if not isinstance(counts, dict):
        counts = {
            key: len(value)
            for key, value in diagnostics.items()
            if isinstance(value, list)
        }
    payload: dict[str, Any] = {
        "run_id": attempt.get("run_id"),
        "last_attempt_status": attempt.get("last_attempt_status"),
        "execution_result": attempt.get("execution_result"),
        "matched_run_id": attempt.get("matched_run_id"),
        "started_at": attempt.get("started_at"),
        "finished_at": attempt.get("finished_at"),
        "combination": attempt.get("combination"),
        "asof": attempt.get("asof"),
        "algorithm_version": attempt.get("algorithm_version"),
        "parameter_hash": attempt.get("parameter_hash"),
        "artifact_locator": attempt.get("artifact_locator"),
        "input_row_counts": attempt.get("input_row_counts") or {},
        "output_row_counts": attempt.get("output_row_counts") or {},
        "diagnostic_counts": counts,
        "error": safe_error,
    }

    def public_diagnostic(value: Any) -> Any:
        if isinstance(value, Mapping):
            clean: dict[str, Any] = {}
            for key, item in value.items():
                normalized_key = str(key).casefold()
                if (
                    normalized_key in _SENSITIVE_DIAGNOSTIC_KEYS
                    or normalized_key.endswith("_path")
                    or "token" in normalized_key
                    or "secret" in normalized_key
                    or normalized_key.endswith("api_key")
                ):
                    continue
                clean[str(key)] = public_diagnostic(item)
            return clean
        if isinstance(value, (list, tuple)):
            return [public_diagnostic(item) for item in value]
        if isinstance(value, str) and (
            value.startswith("/") or _WINDOWS_ABSOLUTE_PATH_RE.match(value)
        ):
            return "[REDACTED]"
        return normalize_json_value(value)

    if diagnostic_type is not None:
        values = diagnostics.get(diagnostic_type, [])
        if not isinstance(values, list):
            raise ArtifactValidationError("requested diagnostics are not a list")
        start = (page - 1) * page_size
        payload["diagnostics"] = {
            "diagnostic_type": diagnostic_type,
            "page": page,
            "page_size": page_size,
            "total": len(values),
            "has_next": start + page_size < len(values),
            "rows": public_diagnostic(values[start : start + page_size]),
        }
    return normalize_json_value(payload)


@router.get("/group-analytics", response_class=HTMLResponse)
def group_analytics_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "group_analytics.html", {})


@router.get("/group-analytics/groups/{group_id}", response_class=HTMLResponse)
def group_detail_page(
    request: Request,
    group_id: str,
    data_run_id: str | None = None,
    level: str = "sector",
) -> HTMLResponse:
    try:
        safe_group_id = _safe_group_id(group_id)
        safe_run_id = _safe_run_id(data_run_id) if data_run_id else None
        safe_level = _choice(level, field="level", allowed=_ALLOWED_LEVELS)
    except _APIContractError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    return templates.TemplateResponse(
        request,
        "group_detail.html",
        {
            "group_id": safe_group_id,
            "data_run_id": safe_run_id,
            "level": safe_level,
        },
    )


@router.get("/api/group-analytics/metadata", response_class=JSONResponse)
def group_metadata(request: Request) -> JSONResponse:
    def operation() -> dict[str, Any]:
        _validate_query_keys(request, set())
        return _metadata_payload(_READER)

    return _api_call(request, operation)


@router.get("/api/group-analytics/heat", response_class=JSONResponse)
def group_heat(
    request: Request,
    universe: str = "SP500",
    taxonomy: str = "FMP",
    level: str = "sector",
    asof: str = "latest",
    mode: str = "eod",
    benchmark: str = "SPY",
    return_method: str = "ROBUST_EW",
    data_run_id: str | None = None,
    sort_by: str = "robust_ew_return_1d",
    sort_order: str = "desc",
    view_min_members: str = "5",
    show_low_confidence: str = "false",
    view: str = "all",
    limit: str | None = None,
) -> JSONResponse:
    def operation() -> dict[str, Any]:
        _validate_query_keys(
            request,
            {
                "universe", "taxonomy", "level", "asof", "mode", "benchmark",
                "return_method", "data_run_id", "sort_by", "sort_order",
                "view_min_members", "show_low_confidence", "view", "limit",
            },
        )
        combo = _combination(
            universe=universe,
            taxonomy=taxonomy,
            level=level,
            mode=mode,
            asof=asof,
            benchmark=benchmark,
            return_method=return_method,
        )
        _choice(sort_by, field="sort_by", allowed=_HEAT_SORT_FIELDS)
        _choice(sort_order, field="sort_order", allowed=("asc", "desc"))
        _choice(view, field="view", allowed=("all", "top", "bottom"))
        if view in {"top", "bottom"} and (
            sort_by != "robust_ew_return_1d" or sort_order != "desc"
        ):
            raise _APIContractError(
                422,
                "INVALID_REQUEST",
                "Top/Bottom views use the frozen headline sort only",
                details={"sort_by": sort_by, "sort_order": sort_order, "view": view},
            )
        minimum = _integer(
            view_min_members,
            field="view_min_members",
            default=5,
            minimum=0,
            maximum=100_000,
        )
        row_limit = _integer(limit, field="limit", default=None, minimum=1, maximum=1_000)
        show_low = _boolean(show_low_confidence, field="show_low_confidence")
        loaded, last = _load_fixed_run(_READER, combo, data_run_id=data_run_id)
        assert minimum is not None
        return _heat_payload(
            loaded,
            last,
            view=view,
            sort_by=sort_by,
            sort_order=sort_order,
            view_min_members=minimum,
            show_low_confidence=show_low,
            limit=row_limit,
        )

    return _api_call(request, operation)


@router.get("/api/group-analytics/groups/{group_id}", response_class=JSONResponse)
def group_detail(
    request: Request,
    group_id: str,
    universe: str = "SP500",
    taxonomy: str = "FMP",
    level: str = "sector",
    asof: str = "latest",
    mode: str = "eod",
    benchmark: str = "SPY",
    return_method: str = "ROBUST_EW",
    data_run_id: str | None = None,
    page: str = "1",
    page_size: str = "50",
    member_sort_by: str = "ticker",
    member_sort_order: str = "asc",
) -> JSONResponse:
    def operation() -> dict[str, Any]:
        _validate_query_keys(
            request,
            {
                "universe", "taxonomy", "level", "asof", "mode", "benchmark",
                "return_method", "data_run_id", "page", "page_size",
                "member_sort_by", "member_sort_order",
            },
        )
        safe_group_id = _safe_group_id(group_id)
        combo = _combination(
            universe=universe,
            taxonomy=taxonomy,
            level=level,
            mode=mode,
            asof=asof,
            benchmark=benchmark,
            return_method=return_method,
        )
        _choice(member_sort_by, field="member_sort_by", allowed=_MEMBER_SORT_FIELDS)
        _choice(member_sort_order, field="member_sort_order", allowed=("asc", "desc"))
        parsed_page = _integer(page, field="page", default=1, minimum=1, maximum=1_000_000)
        parsed_size = _integer(page_size, field="page_size", default=50, minimum=1, maximum=200)
        loaded, _ = _load_fixed_run(_READER, combo, data_run_id=data_run_id)
        assert parsed_page is not None and parsed_size is not None
        return _detail_payload(
            loaded,
            group_id=safe_group_id,
            page=parsed_page,
            page_size=parsed_size,
            member_sort_by=member_sort_by,
            member_sort_order=member_sort_order,
        )

    return _api_call(request, operation)


@router.get("/api/group-analytics/runs/{run_id}", response_class=JSONResponse)
def group_run(
    request: Request,
    run_id: str,
    diagnostic_type: str | None = None,
    page: str = "1",
    page_size: str = "50",
) -> JSONResponse:
    def operation() -> dict[str, Any]:
        _validate_query_keys(request, {"diagnostic_type", "page", "page_size"})
        safe_run_id = _safe_run_id(run_id, field="run_id")
        if diagnostic_type is not None:
            _choice(
                diagnostic_type,
                field="diagnostic_type",
                allowed=_DIAGNOSTIC_TYPES,
            )
        elif "page" in request.query_params or "page_size" in request.query_params:
            raise _APIContractError(
                422,
                "INVALID_REQUEST",
                "page/page_size require diagnostic_type",
                details={"allowed_values": list(_DIAGNOSTIC_TYPES)},
            )
        parsed_page = _integer(page, field="page", default=1, minimum=1, maximum=1_000_000)
        parsed_size = _integer(page_size, field="page_size", default=50, minimum=1, maximum=200)
        attempt = _READER.load_attempt(safe_run_id)
        assert parsed_page is not None and parsed_size is not None
        return _run_payload(
            attempt,
            diagnostic_type=diagnostic_type,
            page=parsed_page,
            page_size=parsed_size,
        )

    return _api_call(request, operation)


__all__ = ["router", "templates"]
