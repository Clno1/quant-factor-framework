"""Transactional artifact publication and read-only access for group analytics.

The module deliberately owns its JSON and publication protocol instead of
changing :mod:`src.utils.io`.  Group-analytics JSON is strict (non-finite
numbers become ``null`` and unsupported objects fail), while an immutable run
directory plus one atomic pointer prevents readers from mixing two runs.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any, Iterator, Mapping, Sequence
from uuid import uuid4

import numpy as np
import pandas as pd

from src.group_analytics import ALGORITHM_VERSION, SCHEMA_VERSION
from src.group_analytics.models import (
    ArtifactCombination,
    ArtifactNotFoundError,
    GroupAnalyticsBundle,
    GroupAnalyticsError,
    NoSuccessfulRunError,
    RunOutcome,
    RunStatus,
)
from src.group_analytics.settings import (
    GroupAnalyticsSettings,
    load_group_analytics_settings,
)


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DAILY_METRICS_FILE = "daily_metrics.parquet"
MEMBERS_FILE = "members.parquet"
CONTRIBUTIONS_FILE = "member_contributions.parquet"
RUN_FILE = "run.json"
MANIFEST_FILE = "manifest.json"
DIAGNOSTICS_FILE = "diagnostics.json"

PUBLISHED_FILES = (
    RUN_FILE,
    DIAGNOSTICS_FILE,
    DAILY_METRICS_FILE,
    MEMBERS_FILE,
    CONTRIBUTIONS_FILE,
)

DAILY_PRIMARY_KEY = (
    "date",
    "universe",
    "taxonomy",
    "level",
    "mode",
    "snapshot_id",
    "group_id",
)
MEMBERS_PRIMARY_KEY = DAILY_PRIMARY_KEY + ("security_id",)
CONTRIBUTIONS_PRIMARY_KEY = MEMBERS_PRIMARY_KEY + ("return_method",)

DAILY_REQUIRED_COLUMNS = set(DAILY_PRIMARY_KEY) | {
    "group_name",
    "n_expected",
    "n_valid",
    "count_coverage",
    "headline_n_effective",
    "snapshot_quality_score",
    "snapshot_quality_grade",
    "quality_status",
    "raw_ew_return_1d",
    "robust_ew_return_1d",
    "median_return_1d",
    "up_pct",
    "down_pct",
    "breadth_net",
    "ad_ratio",
    "dispersion_mad",
    "dispersion_std",
    "driver_method",
    "top_driver_ticker",
    "bottom_driver_ticker",
    "single_name_concentration",
    "eligible_for_ranking",
    "reason_codes",
}
MEMBERS_REQUIRED_COLUMNS = set(MEMBERS_PRIMARY_KEY) | {
    "ticker",
    "is_valid_for_headline",
    "raw_return_1d",
    "winsorized_return_1d",
    "was_winsorized",
    "headline_weight",
    "headline_contribution",
    "reason_codes",
}
CONTRIBUTIONS_REQUIRED_COLUMNS = set(CONTRIBUTIONS_PRIMARY_KEY) | {
    "weight",
    "input_return",
    "contribution",
    "rank_within_group",
}


class ArtifactValidationError(GroupAnalyticsError):
    code = "ARTIFACT_VALIDATION_FAILED"
    stage = "validate_artifacts"


class RunIdCollisionError(GroupAnalyticsError):
    code = "RUN_ID_COLLISION"
    stage = "reserve_run_id"


class ConcurrentWriterError(GroupAnalyticsError):
    code = "CONCURRENT_WRITER"
    stage = "acquire_publish_lock"


class OutOfOrderPublicationError(GroupAnalyticsError):
    code = "OUT_OF_ORDER_PUBLICATION"
    stage = "publish_artifacts"


@dataclass(slots=True)
class LoadedArtifactRun:
    """One immutable run resolved before any of its files are read."""

    run_id: str
    combination: ArtifactCombination
    path: Path
    pointer: dict[str, Any] | None
    run: dict[str, Any]
    manifest: dict[str, Any]
    diagnostics: dict[str, Any]
    metrics: pd.DataFrame
    members: pd.DataFrame
    contributions: pd.DataFrame

    @property
    def bundle(self) -> GroupAnalyticsBundle:
        return GroupAnalyticsBundle(
            metrics=self.metrics,
            members=self.members,
            contributions=self.contributions,
            diagnostics=self.diagnostics,
            manifest=self.manifest,
            run=self.run,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def generate_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"ga_{stamp}_{uuid4().hex[:8]}"


def _validate_run_id(run_id: str) -> str:
    value = str(run_id)
    if not _RUN_ID_RE.fullmatch(value) or value in {".", ".."}:
        raise ArtifactValidationError(
            "run_id contains unsafe characters",
            details={"run_id": value},
        )
    return value


def _safe_segment(value: str, *, field: str) -> str:
    text = str(value)
    if not _PATH_SEGMENT_RE.fullmatch(text) or text in {".", ".."}:
        raise ArtifactValidationError(
            f"{field} contains unsafe path characters",
            details={field: text},
        )
    return text


def normalize_json_value(value: Any) -> Any:
    """Return a strict JSON value without lossy ``default=str`` fallback."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, Enum):
        return normalize_json_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return normalize_json_value(asdict(value))
    if isinstance(value, np.generic):
        return normalize_json_value(value.item())
    if isinstance(value, np.ndarray):
        return normalize_json_value(value.tolist())
    if isinstance(value, (pd.Timestamp, datetime)):
        if pd.isna(value):
            return None
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, pd.Timedelta):
        if pd.isna(value):
            return None
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, Enum):
                key = key.value
            if not isinstance(key, (str, int, float, bool)) or key is None:
                raise TypeError(f"Unsupported JSON object key type: {type(key).__name__}")
            result[str(key)] = normalize_json_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [normalize_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [normalize_json_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )
    raise TypeError(f"Unsupported JSON value type: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    normalized = normalize_json_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def compute_parameter_hash(value: GroupAnalyticsSettings | Mapping[str, Any]) -> str:
    config = value.algorithm_config() if isinstance(value, GroupAnalyticsSettings) else value
    return canonical_hash(config)


def compute_runtime_config_hash(value: GroupAnalyticsSettings | Mapping[str, Any]) -> str:
    config = value.runtime_config() if isinstance(value, GroupAnalyticsSettings) else value
    return canonical_hash(config)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _strict_json_load(path: Path) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"Non-standard JSON constant {value!r}")

    try:
        with path.open("r", encoding="utf-8") as handle:
            result = json.load(handle, parse_constant=reject_constant)
    except FileNotFoundError as exc:
        raise ArtifactNotFoundError(f"Artifact JSON not found: {path}") from exc
    except (OSError, ValueError, TypeError) as exc:
        raise ArtifactValidationError(
            f"Invalid artifact JSON: {path.name}",
            details={"path": str(path), "error": str(exc)},
        ) from exc
    if not isinstance(result, dict):
        raise ArtifactValidationError(
            f"Artifact JSON must contain an object: {path.name}",
            details={"path": str(path)},
        )
    return result


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_json_value(value)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                normalized,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _write_json_non_atomic(path: Path, value: Any) -> None:
    normalized = normalize_json_value(value)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(
            normalized,
            handle,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _coerce_settings(
    settings: GroupAnalyticsSettings | None,
    output_root: Path | str | None,
) -> GroupAnalyticsSettings:
    if settings is None:
        return load_group_analytics_settings(
            output_root=Path(output_root) if output_root is not None else None
        )
    if output_root is not None:
        return replace(settings, output_root=Path(output_root))
    return settings


class _ArtifactPaths:
    def __init__(self, settings: GroupAnalyticsSettings):
        self.settings = settings
        self.output_root = settings.output_root.resolve()
        self.artifact_root = settings.artifact_root.resolve()
        try:
            self.artifact_root.relative_to(self.output_root)
        except ValueError as exc:
            raise ArtifactValidationError(
                "group analytics artifact_root escapes output_root",
                details={"output_subdir": settings.output_subdir},
            ) from exc
        self.attempts_root = self.output_root / "_group_analytics_attempts"
        self._assert_confined(
            self.attempts_root,
            self.output_root,
            field="attempts_root",
        )
        if self.artifact_root == self.attempts_root:
            raise ArtifactValidationError(
                "artifact_root cannot overlap the global attempts namespace"
            )

    @staticmethod
    def _assert_confined(path: Path, root: Path, *, field: str) -> Path:
        resolved_root = root.resolve()
        resolved = path.resolve()
        try:
            resolved.relative_to(resolved_root)
        except ValueError as exc:
            raise ArtifactValidationError(
                f"{field} escapes its storage root",
                details={field: str(path)},
            ) from exc
        return path

    def combo_dir(self, combination: ArtifactCombination) -> Path:
        combo = combination.normalized()
        universe = _safe_segment(combo.universe, field="universe")
        taxonomy = _safe_segment(combo.taxonomy, field="taxonomy")
        level = _safe_segment(combo.level, field="level")
        mode = _safe_segment(combo.mode, field="mode")
        if mode not in {"eod", "live"}:
            raise ArtifactValidationError(
                "Persisted mode must be eod or live",
                details={"mode": mode},
            )
        candidate = (
            self.artifact_root
            / universe
            / "group_analytics"
            / taxonomy
            / level
            / mode
        )
        self._assert_confined(
            self.artifact_root,
            self.output_root,
            field="artifact_root",
        )
        return self._assert_confined(
            candidate,
            self.artifact_root,
            field="combination_dir",
        )

    def attempt_dir(self, run_id: str) -> Path:
        self._assert_confined(
            self.attempts_root,
            self.output_root,
            field="attempts_root",
        )
        return self._assert_confined(
            self.attempts_root / _validate_run_id(run_id),
            self.attempts_root,
            field="attempt_dir",
        )

    def resolve_locator(self, locator: str, *, run_id: str) -> Path:
        relative = Path(str(locator))
        if relative.is_absolute() or ".." in relative.parts:
            raise ArtifactValidationError(
                "artifact_locator must be a confined relative path",
                details={"artifact_locator": str(locator)},
            )
        target = (self.output_root / relative).resolve()
        try:
            target.relative_to(self.output_root)
        except ValueError as exc:
            raise ArtifactValidationError(
                "artifact_locator escapes output_root",
                details={"artifact_locator": str(locator)},
            ) from exc
        if target.name != _validate_run_id(run_id):
            raise ArtifactValidationError(
                "artifact_locator does not match run_id",
                details={"artifact_locator": str(locator), "run_id": run_id},
            )
        return target

    def locator_for(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.output_root).as_posix()
        except ValueError as exc:
            raise ArtifactValidationError("Run directory is outside output_root") from exc


@contextmanager
def _exclusive_file_lock(path: Path, *, timeout_seconds: float) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                if time.monotonic() >= deadline:
                    raise ConcurrentWriterError(
                        "Timed out waiting for group artifact writer lock",
                        details={"lock_path": str(path)},
                    ) from exc
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _error_payload(error: BaseException | Mapping[str, Any] | str) -> dict[str, Any]:
    if isinstance(error, Mapping):
        supplied = dict(error)
        return {
            "code": str(supplied.get("code") or "GROUP_ANALYTICS_ERROR"),
            "stage": str(supplied.get("stage") or "unknown"),
            "summary": str(
                supplied.get("summary") or supplied.get("message") or "Group analytics failed"
            )[:2000],
            **(
                {"details": normalize_json_value(supplied["details"])}
                if supplied.get("details") is not None
                else {}
            ),
        }
    if isinstance(error, str):
        return {"code": "GROUP_ANALYTICS_ERROR", "stage": "unknown", "summary": error[:2000]}
    return {
        "code": str(getattr(error, "code", "ARTIFACT_PUBLISH_FAILED")),
        "stage": str(getattr(error, "stage", "publish_artifacts")),
        "summary": str(error)[:2000] or error.__class__.__name__,
        **(
            {"details": normalize_json_value(getattr(error, "details"))}
            if getattr(error, "details", None)
            else {}
        ),
    }


def _ensure_columns(frame: pd.DataFrame, required: set[str], *, table: str) -> None:
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ArtifactValidationError(
            f"{table} is missing required columns",
            details={"table": table, "missing_columns": missing},
        )


def _validate_primary_key(
    frame: pd.DataFrame,
    columns: Sequence[str],
    *,
    table: str,
) -> None:
    _ensure_columns(frame, set(columns), table=table)
    if frame.empty:
        return
    null_mask = frame[list(columns)].isna().any(axis=1)
    if bool(null_mask.any()):
        raise ArtifactValidationError(
            f"{table} primary key contains nulls",
            details={"table": table, "null_rows": int(null_mask.sum())},
        )
    duplicate_mask = frame.duplicated(list(columns), keep=False)
    if bool(duplicate_mask.any()):
        sample = frame.loc[duplicate_mask, list(columns)].head(5).to_dict("records")
        raise ArtifactValidationError(
            f"{table} primary key is not unique",
            details={"table": table, "sample": sample},
        )


def _assert_no_infinite(frame: pd.DataFrame, columns: Sequence[str], *, table: str) -> None:
    for column in columns:
        if column not in frame.columns:
            continue
        numeric = pd.to_numeric(frame[column], errors="coerce")
        count = int(np.isinf(numeric.to_numpy(dtype=float, na_value=np.nan)).sum())
        if count:
            raise ArtifactValidationError(
                f"{table}.{column} contains infinite values",
                details={"table": table, "column": column, "count": count},
            )


def _key_from_row(row: pd.Series, columns: Sequence[str]) -> tuple[Any, ...]:
    result: list[Any] = []
    for column in columns:
        value = row[column]
        if isinstance(value, pd.Timestamp):
            value = value.isoformat()
        elif isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, date):
            value = value.isoformat()
        elif isinstance(value, np.generic):
            value = value.item()
        result.append(value)
    return tuple(result)


def _validate_combo_columns(
    frame: pd.DataFrame,
    combination: ArtifactCombination,
    *,
    table: str,
) -> None:
    if frame.empty:
        return
    combo = combination.normalized()
    expectations = {
        "universe": combo.universe,
        "taxonomy": combo.taxonomy,
        "level": combo.level,
        "mode": combo.mode,
    }
    for column, expected in expectations.items():
        values = {str(value) for value in frame[column].dropna().unique()}
        normalized_values = (
            {value.upper() for value in values}
            if column in {"universe", "taxonomy"}
            else {value.lower() for value in values}
        )
        if normalized_values != {expected}:
            raise ArtifactValidationError(
                f"{table}.{column} does not match artifact combination",
                details={"table": table, "column": column, "values": sorted(values)},
            )


def _validate_temporal_columns(
    frame: pd.DataFrame,
    combination: ArtifactCombination,
    *,
    table: str,
    asof: str,
) -> None:
    if frame.empty:
        return
    if not _ISO_DATE_RE.fullmatch(str(asof)):
        raise ArtifactValidationError(
            "Artifact asof must be an ISO calendar date",
            details={"asof": str(asof)},
        )
    try:
        expected_date = pd.Timestamp(asof).normalize()
        dates = pd.to_datetime(frame["date"], errors="raise")
    except Exception as exc:  # noqa: BLE001
        raise ArtifactValidationError(
            f"{table}.date contains invalid values",
            details={"table": table},
        ) from exc
    if getattr(dates.dt, "tz", None) is not None:
        dates = dates.dt.tz_convert("America/New_York").dt.tz_localize(None)
    normalized = dates.dt.normalize()
    if bool(normalized.ne(expected_date).any()):
        sample = sorted({pd.Timestamp(value).isoformat() for value in normalized.unique()})[:5]
        raise ArtifactValidationError(
            f"{table}.date does not match artifact asof",
            details={"table": table, "asof": asof, "sample_dates": sample},
        )
    expected_snapshot = "EOD" if combination.normalized().mode == "eod" else None
    snapshot_values = set(frame["snapshot_id"].dropna().astype(str).unique())
    if expected_snapshot is not None and snapshot_values != {expected_snapshot}:
        raise ArtifactValidationError(
            f"{table}.snapshot_id does not match mode",
            details={
                "table": table,
                "mode": combination.normalized().mode,
                "values": sorted(snapshot_values),
            },
        )


def _validate_metric_rows(metrics: pd.DataFrame) -> None:
    if metrics.empty:
        return
    expected = pd.to_numeric(metrics["n_expected"], errors="coerce")
    valid = pd.to_numeric(metrics["n_valid"], errors="coerce")
    if bool(expected.isna().any() or valid.isna().any()):
        raise ArtifactValidationError("n_expected and n_valid must be numeric")
    if bool(((expected < 0) | (valid < 0) | (valid > expected)).any()):
        raise ArtifactValidationError("n_expected/n_valid invariants failed")
    if bool(((expected % 1 != 0) | (valid % 1 != 0)).any()):
        raise ArtifactValidationError("n_expected and n_valid must be integers")

    coverage = pd.to_numeric(metrics["count_coverage"], errors="coerce")
    for index in metrics.index:
        n_expected = float(expected.loc[index])
        n_valid = float(valid.loc[index])
        actual = coverage.loc[index]
        if n_expected == 0:
            if pd.notna(actual):
                raise ArtifactValidationError("count_coverage must be null when n_expected is zero")
        else:
            if pd.isna(actual) or not math.isclose(
                float(actual), n_valid / n_expected, rel_tol=0.0, abs_tol=1e-12
            ):
                raise ArtifactValidationError(
                    "count_coverage does not reconcile with member counts",
                    details={"row_index": str(index)},
                )


def _validate_member_reconciliation(
    metrics: pd.DataFrame,
    members: pd.DataFrame,
) -> None:
    metric_by_key = {
        _key_from_row(row, DAILY_PRIMARY_KEY): row
        for _, row in metrics.iterrows()
    }
    member_counts: dict[tuple[Any, ...], int] = {}
    valid_counts: dict[tuple[Any, ...], int] = {}
    contribution_sums: dict[tuple[Any, ...], float] = {}
    for _, row in members.iterrows():
        key = _key_from_row(row, DAILY_PRIMARY_KEY)
        if key not in metric_by_key:
            raise ArtifactValidationError(
                "members contains a group absent from daily_metrics",
                details={"group_key": normalize_json_value(key)},
            )
        member_counts[key] = member_counts.get(key, 0) + 1
        is_valid = row["is_valid_for_headline"] is True or isinstance(
            row["is_valid_for_headline"], np.bool_
        ) and bool(row["is_valid_for_headline"])
        if is_valid:
            valid_counts[key] = valid_counts.get(key, 0) + 1
        value = pd.to_numeric(pd.Series([row["headline_contribution"]]), errors="coerce").iloc[0]
        if pd.notna(value):
            if not math.isfinite(float(value)):
                raise ArtifactValidationError("headline_contribution contains infinity")
            contribution_sums[key] = contribution_sums.get(key, 0.0) + float(value)

    for key, metric in metric_by_key.items():
        if member_counts.get(key, 0) != int(metric["n_expected"]):
            raise ArtifactValidationError(
                "members row count does not equal n_expected",
                details={
                    "group_key": normalize_json_value(key),
                    "member_count": member_counts.get(key, 0),
                    "n_expected": int(metric["n_expected"]),
                },
            )
        if valid_counts.get(key, 0) != int(metric["n_valid"]):
            raise ArtifactValidationError(
                "valid member count does not equal n_valid",
                details={"group_key": normalize_json_value(key)},
            )
        robust = pd.to_numeric(
            pd.Series([metric["robust_ew_return_1d"]]), errors="coerce"
        ).iloc[0]
        if pd.notna(robust):
            if not math.isclose(
                contribution_sums.get(key, 0.0),
                float(robust),
                rel_tol=0.0,
                abs_tol=1e-10,
            ):
                raise ArtifactValidationError(
                    "member headline contributions do not sum to RobustEW",
                    details={"group_key": normalize_json_value(key)},
                )
        elif key in contribution_sums:
            raise ArtifactValidationError(
                "null RobustEW group has non-null member contribution",
                details={"group_key": normalize_json_value(key)},
            )


def _validate_contribution_reconciliation(
    metrics: pd.DataFrame,
    contributions: pd.DataFrame,
) -> None:
    metric_by_key = {
        _key_from_row(row, DAILY_PRIMARY_KEY): row
        for _, row in metrics.iterrows()
    }
    robust_rows = contributions[
        contributions["return_method"].astype(str).str.upper().eq("ROBUST_EW")
    ]
    grouped: dict[tuple[Any, ...], list[pd.Series]] = {}
    for _, row in robust_rows.iterrows():
        key = _key_from_row(row, DAILY_PRIMARY_KEY)
        if key not in metric_by_key:
            raise ArtifactValidationError(
                "contributions contains a group absent from daily_metrics",
                details={"group_key": normalize_json_value(key)},
            )
        grouped.setdefault(key, []).append(row)

    for key, metric in metric_by_key.items():
        rows = grouped.get(key, [])
        finite_contributions: list[float] = []
        finite_weights: list[float] = []
        for row in rows:
            contribution = pd.to_numeric(
                pd.Series([row["contribution"]]), errors="coerce"
            ).iloc[0]
            weight = pd.to_numeric(pd.Series([row["weight"]]), errors="coerce").iloc[0]
            input_return = pd.to_numeric(
                pd.Series([row["input_return"]]), errors="coerce"
            ).iloc[0]
            if pd.notna(contribution):
                if not math.isfinite(float(contribution)):
                    raise ArtifactValidationError("contribution contains infinity")
                finite_contributions.append(float(contribution))
            if pd.notna(weight):
                if not math.isfinite(float(weight)) or float(weight) < 0:
                    raise ArtifactValidationError("contribution weight must be finite and non-negative")
                finite_weights.append(float(weight))
            if pd.notna(contribution) and pd.notna(weight) and pd.notna(input_return):
                if not math.isclose(
                    float(contribution),
                    float(weight) * float(input_return),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                ):
                    raise ArtifactValidationError(
                        "contribution does not equal weight times input_return",
                        details={"group_key": normalize_json_value(key)},
                    )

        n_valid = int(metric["n_valid"])
        robust = pd.to_numeric(
            pd.Series([metric["robust_ew_return_1d"]]), errors="coerce"
        ).iloc[0]
        if len(finite_contributions) != n_valid:
            raise ArtifactValidationError(
                "ROBUST_EW finite contribution count does not equal n_valid",
                details={
                    "group_key": normalize_json_value(key),
                    "contribution_count": len(finite_contributions),
                    "n_valid": n_valid,
                },
            )
        if n_valid:
            if len(finite_weights) != n_valid or not math.isclose(
                sum(finite_weights), 1.0, rel_tol=0.0, abs_tol=1e-12
            ):
                raise ArtifactValidationError(
                    "ROBUST_EW weights do not sum to one",
                    details={"group_key": normalize_json_value(key)},
                )
            if pd.isna(robust) or not math.isclose(
                sum(finite_contributions),
                float(robust),
                rel_tol=0.0,
                abs_tol=1e-10,
            ):
                raise ArtifactValidationError(
                    "ROBUST_EW contributions do not reconcile with daily_metrics",
                    details={"group_key": normalize_json_value(key)},
                )
        elif pd.notna(robust):
            raise ArtifactValidationError("n_valid=0 requires null RobustEW")


def validate_bundle_frames(
    metrics: pd.DataFrame,
    members: pd.DataFrame,
    contributions: pd.DataFrame,
    combination: ArtifactCombination,
    *,
    asof: str | None = None,
) -> None:
    for frame, table in (
        (metrics, DAILY_METRICS_FILE),
        (members, MEMBERS_FILE),
        (contributions, CONTRIBUTIONS_FILE),
    ):
        if not isinstance(frame, pd.DataFrame):
            raise ArtifactValidationError(f"{table} must be a pandas DataFrame")

    _ensure_columns(metrics, DAILY_REQUIRED_COLUMNS, table=DAILY_METRICS_FILE)
    _ensure_columns(members, MEMBERS_REQUIRED_COLUMNS, table=MEMBERS_FILE)
    _ensure_columns(
        contributions,
        CONTRIBUTIONS_REQUIRED_COLUMNS,
        table=CONTRIBUTIONS_FILE,
    )
    _validate_primary_key(metrics, DAILY_PRIMARY_KEY, table=DAILY_METRICS_FILE)
    _validate_primary_key(members, MEMBERS_PRIMARY_KEY, table=MEMBERS_FILE)
    _validate_primary_key(
        contributions,
        CONTRIBUTIONS_PRIMARY_KEY,
        table=CONTRIBUTIONS_FILE,
    )
    _validate_combo_columns(metrics, combination, table=DAILY_METRICS_FILE)
    _validate_combo_columns(members, combination, table=MEMBERS_FILE)
    _validate_combo_columns(contributions, combination, table=CONTRIBUTIONS_FILE)
    if asof is not None:
        _validate_temporal_columns(
            metrics, combination, table=DAILY_METRICS_FILE, asof=asof
        )
        _validate_temporal_columns(
            members, combination, table=MEMBERS_FILE, asof=asof
        )
        _validate_temporal_columns(
            contributions, combination, table=CONTRIBUTIONS_FILE, asof=asof
        )
    _assert_no_infinite(
        metrics,
        (
            "raw_ew_return_1d",
            "robust_ew_return_1d",
            "median_return_1d",
            "up_pct",
            "down_pct",
            "breadth_net",
            "ad_ratio",
        ),
        table=DAILY_METRICS_FILE,
    )
    _assert_no_infinite(
        members,
        ("raw_return_1d", "winsorized_return_1d", "headline_contribution"),
        table=MEMBERS_FILE,
    )
    _assert_no_infinite(
        contributions,
        ("weight", "input_return", "contribution"),
        table=CONTRIBUTIONS_FILE,
    )
    _validate_metric_rows(metrics)
    _validate_member_reconciliation(metrics, members)
    _validate_contribution_reconciliation(metrics, contributions)


class FileGroupArtifactStore:
    """Filesystem implementation of the immutable run publication protocol."""

    def __init__(
        self,
        settings: GroupAnalyticsSettings | None = None,
        *,
        output_root: Path | str | None = None,
        lock_timeout_seconds: float = 30.0,
    ):
        self.settings = _coerce_settings(settings, output_root)
        self.paths = _ArtifactPaths(self.settings)
        self.lock_timeout_seconds = float(lock_timeout_seconds)

    @property
    def output_root(self) -> Path:
        return self.paths.output_root

    @property
    def artifact_root(self) -> Path:
        return self.paths.artifact_root

    @property
    def attempts_root(self) -> Path:
        return self.paths.attempts_root

    def combination_dir(self, combination: ArtifactCombination) -> Path:
        return self.paths.combo_dir(combination)

    def _run_exists_outside_attempt_index(self, run_id: str) -> bool:
        pattern = f"*/group_analytics/*/*/*/runs/{run_id}"
        return any(self.artifact_root.glob(pattern))

    def new_run_id(self, requested: str | None = None) -> str:
        """Atomically reserve a globally unique run id in the attempts index."""
        run_id = _validate_run_id(requested or generate_run_id())
        if self._run_exists_outside_attempt_index(run_id):
            raise RunIdCollisionError(
                f"Run id already exists: {run_id}", details={"run_id": run_id}
            )
        attempt_dir = self.paths.attempt_dir(run_id)
        self.attempts_root.mkdir(parents=True, exist_ok=True)
        try:
            attempt_dir.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise RunIdCollisionError(
                f"Run id already exists: {run_id}", details={"run_id": run_id}
            ) from exc
        _atomic_write_json(
            attempt_dir / RUN_FILE,
            {
                "run_id": run_id,
                "last_attempt_status": RunStatus.RUNNING,
                "started_at": _utc_now(),
                "finished_at": None,
                "combination": None,
                "artifact_locator": None,
                "dry_run": None,
            },
        )
        return run_id

    def _ensure_reserved(self, run_id: str) -> dict[str, Any]:
        run_id = _validate_run_id(run_id)
        attempt_dir = self.paths.attempt_dir(run_id)
        if not attempt_dir.exists():
            self.new_run_id(run_id)
        state = _strict_json_load(attempt_dir / RUN_FILE)
        status = str(state.get("last_attempt_status") or "")
        if status in {RunStatus.SUCCESS.value, RunStatus.FAILED.value}:
            raise RunIdCollisionError(
                f"Run id is already terminal: {run_id}",
                details={"run_id": run_id, "status": status},
            )
        return state

    def _combo_lock(self, combination: ArtifactCombination) -> Iterator[None]:
        combo_dir = self.paths.combo_dir(combination)
        lock_path = self.paths._assert_confined(
            combo_dir / ".publish.lock",
            combo_dir,
            field="publish_lock",
        )
        return _exclusive_file_lock(
            lock_path,
            timeout_seconds=self.lock_timeout_seconds,
        )

    def _write_last_attempt_unlocked(
        self,
        combination: ArtifactCombination,
        payload: Mapping[str, Any],
    ) -> None:
        path = self.paths.combo_dir(combination) / "last_attempt.json"
        current: dict[str, Any] | None = None
        if path.exists():
            current = _strict_json_load(path)
        if current and current.get("run_id") != payload.get("run_id"):
            current_key = (str(current.get("started_at") or ""), str(current.get("run_id") or ""))
            new_key = (str(payload.get("started_at") or ""), str(payload.get("run_id") or ""))
            if current_key > new_key:
                return
        _atomic_write_json(path, payload)

    def record_running(
        self,
        run_id: str,
        combination: ArtifactCombination,
        metadata: Mapping[str, Any] | None = None,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        existing = self._ensure_reserved(run_id)
        combo = combination.normalized()
        existing_combo = existing.get("combination")
        if existing_combo is not None and existing_combo != combo.as_dict():
            raise ArtifactValidationError(
                "Reserved run_id belongs to a different combination",
                details={"run_id": run_id},
            )
        existing_dry_run = existing.get("dry_run")
        if existing_dry_run is not None and bool(existing_dry_run) != bool(dry_run):
            raise ArtifactValidationError(
                "Reserved run_id cannot change dry_run mode",
                details={"run_id": run_id},
            )
        payload = dict(existing)
        payload.update(dict(metadata or {}))
        payload.update(
            {
                "run_id": run_id,
                "last_attempt_status": RunStatus.RUNNING,
                "started_at": existing.get("started_at") or _utc_now(),
                "finished_at": None,
                "combination": combo.as_dict(),
                "algorithm_version": ALGORITHM_VERSION,
                "parameter_hash": compute_parameter_hash(self.settings),
                "runtime_config_hash": compute_runtime_config_hash(self.settings),
                "artifact_locator": None,
                "dry_run": bool(dry_run),
                "error": None,
            }
        )
        _atomic_write_json(self.paths.attempt_dir(run_id) / RUN_FILE, payload)
        if not dry_run:
            last_payload = self._last_attempt_payload(payload)
            try:
                with self._combo_lock(combo):
                    self._write_last_attempt_unlocked(combo, last_payload)
            except Exception as exc:
                # A writer-lock failure happens after the global attempt was
                # reserved.  Make that attempt terminal so monitoring never
                # observes an abandoned RUNNING record.
                payload.update(
                    {
                        "last_attempt_status": RunStatus.FAILED,
                        "finished_at": _utc_now(),
                        "error": _error_payload(exc),
                    }
                )
                _atomic_write_json(
                    self.paths.attempt_dir(run_id) / RUN_FILE,
                    payload,
                )
                raise
        return normalize_json_value(payload)

    def _assert_monotonic_publication(
        self,
        combination: ArtifactCombination,
        *,
        candidate_run_id: str,
        candidate_asof: str,
        candidate_started_at: str,
    ) -> None:
        """Prevent a slow, older computation from rolling latest backward."""

        pointer_path = self.paths.combo_dir(combination) / "latest_success.json"
        if not pointer_path.exists():
            return
        current = _strict_json_load(pointer_path)
        current_run_id = _validate_run_id(str(current.get("run_id") or ""))
        current_asof_raw = current.get("asof")
        if current_asof_raw is None:
            raise ArtifactValidationError("latest_success has no asof")
        try:
            current_asof = pd.Timestamp(current_asof_raw).tz_localize(None).normalize()
            candidate_date = pd.Timestamp(candidate_asof).tz_localize(None).normalize()
        except Exception as exc:  # noqa: BLE001
            raise ArtifactValidationError("publication asof is not a valid date") from exc
        if candidate_date < current_asof:
            raise OutOfOrderPublicationError(
                "An older session cannot replace the current latest_success",
                details={
                    "candidate_run_id": candidate_run_id,
                    "candidate_asof": str(candidate_asof),
                    "current_run_id": current_run_id,
                    "current_asof": str(current_asof_raw),
                },
            )
        if candidate_date > current_asof:
            return

        current_started = current.get("attempt_started_at")
        if current_started is None:
            # Backward compatibility: old pointers did not carry an attempt
            # timestamp, so resolve their immutable provenance once.
            locator = current.get("artifact_locator")
            if not isinstance(locator, str):
                raise ArtifactValidationError(
                    "latest_success lacks publication-order provenance"
                )
            current_dir = self.paths.resolve_locator(
                locator, run_id=current_run_id
            )
            current_run = _strict_json_load(current_dir / RUN_FILE)
            current_manifest = _strict_json_load(current_dir / MANIFEST_FILE)
            current_started = (
                current_run.get("started_at")
                or current_manifest.get("attempt_started_at")
                or current_manifest.get("generated_at")
            )
            if current_started is None:
                raise ArtifactValidationError(
                    "latest_success lacks publication-order provenance"
                )
        candidate_key = (str(candidate_started_at), candidate_run_id)
        current_key = (str(current_started), current_run_id)
        if candidate_key < current_key:
            raise OutOfOrderPublicationError(
                "An older same-session attempt cannot replace latest_success",
                details={
                    "candidate_run_id": candidate_run_id,
                    "candidate_started_at": candidate_started_at,
                    "current_run_id": current_run_id,
                    "current_started_at": current_started,
                },
            )

    @staticmethod
    def _last_attempt_payload(attempt: Mapping[str, Any]) -> dict[str, Any]:
        error = attempt.get("error") or {}
        return {
            "run_id": attempt.get("run_id"),
            "last_attempt_status": attempt.get("last_attempt_status"),
            "started_at": attempt.get("started_at"),
            "finished_at": attempt.get("finished_at"),
            "combination": attempt.get("combination"),
            "asof": attempt.get("asof"),
            "artifact_locator": attempt.get("artifact_locator"),
            "execution_result": attempt.get("execution_result"),
            "matched_run_id": attempt.get("matched_run_id"),
            "published": attempt.get("published"),
            "error_stage": error.get("stage"),
            "error_code": error.get("code"),
            "error_summary": error.get("summary"),
        }

    def _record_failure_unlocked(
        self,
        run_id: str,
        combination: ArtifactCombination,
        error: BaseException | Mapping[str, Any] | str,
        metadata: Mapping[str, Any] | None,
        *,
        dry_run: bool,
        diagnostics: Mapping[str, Any] | None,
        update_last_attempt: bool = True,
    ) -> RunOutcome:
        attempt_path = self.paths.attempt_dir(run_id) / RUN_FILE
        existing = _strict_json_load(attempt_path) if attempt_path.exists() else {}
        error_value = _error_payload(error)
        payload = dict(existing)
        payload.update(dict(metadata or {}))
        payload.update(
            {
                "run_id": run_id,
                "last_attempt_status": RunStatus.FAILED,
                "started_at": existing.get("started_at") or _utc_now(),
                "finished_at": _utc_now(),
                "combination": combination.normalized().as_dict(),
                "algorithm_version": ALGORITHM_VERSION,
                "parameter_hash": compute_parameter_hash(self.settings),
                "runtime_config_hash": compute_runtime_config_hash(self.settings),
                "artifact_locator": None,
                "dry_run": bool(dry_run),
                "error": error_value,
            }
        )
        _atomic_write_json(attempt_path, payload)
        if diagnostics is not None:
            _atomic_write_json(
                self.paths.attempt_dir(run_id) / DIAGNOSTICS_FILE,
                diagnostics,
            )
        if not dry_run and update_last_attempt:
            self._write_last_attempt_unlocked(
                combination,
                self._last_attempt_payload(payload),
            )
        return RunOutcome(
            run_id=run_id,
            status=RunStatus.FAILED,
            dry_run=bool(dry_run),
            published=False,
            combination=combination.normalized(),
            asof=payload.get("asof"),
            artifact_locator=None,
            error=error_value,
        )

    def record_failure(
        self,
        run_id: str,
        combination: ArtifactCombination,
        error: BaseException | Mapping[str, Any] | str,
        metadata: Mapping[str, Any] | None = None,
        *,
        dry_run: bool = False,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> RunOutcome:
        attempt_path = self.paths.attempt_dir(run_id) / RUN_FILE
        if attempt_path.exists():
            existing = _strict_json_load(attempt_path)
            existing_status = str(existing.get("last_attempt_status") or "")
            if existing_status == RunStatus.SUCCESS.value:
                raise RunIdCollisionError(
                    "A successful run attempt is immutable and cannot be marked failed",
                    details={"run_id": run_id},
                )
            if existing_status == RunStatus.FAILED.value:
                return RunOutcome(
                    run_id=run_id,
                    status=RunStatus.FAILED,
                    dry_run=bool(existing.get("dry_run")),
                    published=False,
                    combination=combination.normalized(),
                    asof=existing.get("asof"),
                    artifact_locator=None,
                    error=existing.get("error"),
                )
        else:
            self.new_run_id(run_id)
        # The globally indexed attempt becomes terminal before waiting for the
        # combo pointer lock.  A lock timeout may delay last_attempt visibility
        # but can never leave this run permanently RUNNING.
        outcome = self._record_failure_unlocked(
            run_id,
            combination,
            error,
            metadata,
            dry_run=dry_run,
            diagnostics=diagnostics,
            update_last_attempt=False,
        )
        if not dry_run:
            try:
                attempt = _strict_json_load(attempt_path)
                with self._combo_lock(combination):
                    self._write_last_attempt_unlocked(
                        combination,
                        self._last_attempt_payload(attempt),
                    )
            except ConcurrentWriterError:
                pass
        return outcome

    def record_skipped(
        self,
        run_id: str,
        combination: ArtifactCombination,
        matched_run_id: str,
        metadata: Mapping[str, Any] | None = None,
        *,
        dry_run: bool = False,
    ) -> RunOutcome:
        """Finish an idempotent attempt without publishing another bundle.

        ``SKIPPED`` is an execution result, not a persisted task status.  The
        attempt and combo ``last_attempt`` therefore retain the PRD's
        RUNNING/SUCCESS/FAILED state machine and persist ``SUCCESS`` alongside
        ``execution_result=SKIPPED_IDEMPOTENT``.  The matched immutable run is
        referenced by id only; the new attempt never owns its locator.
        """

        run_id = _validate_run_id(run_id)
        matched_run_id = _validate_run_id(matched_run_id)
        if run_id == matched_run_id:
            raise ArtifactValidationError(
                "An idempotent attempt cannot match itself",
                details={"run_id": run_id},
            )
        combo = combination.normalized()
        existing = self._ensure_reserved(run_id)
        existing_combo = existing.get("combination")
        if existing_combo is not None and existing_combo != combo.as_dict():
            raise ArtifactValidationError(
                "Reserved run_id belongs to a different combination",
                details={"run_id": run_id},
            )
        existing_dry_run = existing.get("dry_run")
        if existing_dry_run is not None and bool(existing_dry_run) != bool(dry_run):
            raise ArtifactValidationError(
                "Reserved run_id cannot change dry_run mode",
                details={"run_id": run_id},
            )

        combo_dir = self.paths.combo_dir(combo)
        with self._combo_lock(combo):
            # Pin the match under the same lock used by publishers.  If latest
            # changed after the service's idempotency lookup, the caller must
            # re-evaluate instead of recording a stale match.
            latest_path = combo_dir / "latest_success.json"
            if not latest_path.exists():
                raise NoSuccessfulRunError(
                    "Cannot record an idempotent skip without a successful run",
                    details={"combination": combo.as_dict()},
                )
            latest = _strict_json_load(latest_path)
            if latest.get("combination") != combo.as_dict():
                raise ArtifactValidationError("latest_success combination mismatch")
            if latest.get("run_id") != matched_run_id:
                raise ArtifactValidationError(
                    "Matched run is no longer latest; idempotency must be re-evaluated",
                    details={
                        "matched_run_id": matched_run_id,
                        "latest_run_id": latest.get("run_id"),
                    },
                )
            locator = latest.get("artifact_locator")
            if not isinstance(locator, str):
                raise ArtifactValidationError("Matched latest run has no artifact_locator")
            matched_dir = self.paths.resolve_locator(locator, run_id=matched_run_id)
            if not matched_dir.is_dir():
                raise ArtifactNotFoundError(
                    "Matched immutable run directory does not exist",
                    details={"matched_run_id": matched_run_id},
                )

            payload = dict(existing)
            payload.update(dict(metadata or {}))
            payload.update(
                {
                    "run_id": run_id,
                    # Persist only the PRD's RUNNING/SUCCESS/FAILED states.
                    "last_attempt_status": RunStatus.SUCCESS,
                    "execution_result": "SKIPPED_IDEMPOTENT",
                    "matched_run_id": matched_run_id,
                    "started_at": existing.get("started_at") or _utc_now(),
                    "finished_at": _utc_now(),
                    "combination": combo.as_dict(),
                    "asof": (metadata or {}).get("asof") or latest.get("asof"),
                    "algorithm_version": ALGORITHM_VERSION,
                    "parameter_hash": compute_parameter_hash(self.settings),
                    "runtime_config_hash": compute_runtime_config_hash(self.settings),
                    "artifact_locator": None,
                    "dry_run": bool(dry_run),
                    "published": False,
                    "error": None,
                }
            )
            _atomic_write_json(self.paths.attempt_dir(run_id) / RUN_FILE, payload)
            if not dry_run:
                self._write_last_attempt_unlocked(
                    combo,
                    self._last_attempt_payload(payload),
                )

        return RunOutcome(
            run_id=run_id,
            status=RunStatus.SKIPPED,
            dry_run=bool(dry_run),
            published=False,
            combination=combo,
            asof=payload.get("asof"),
            artifact_locator=None,
            error=None,
        )

    @staticmethod
    def _infer_asof(
        manifest: Mapping[str, Any],
        run: Mapping[str, Any],
        metrics: pd.DataFrame,
    ) -> str:
        value = manifest.get("asof") or run.get("asof")
        if value is not None:
            return str(normalize_json_value(value))
        if "date" in metrics.columns:
            values = [value for value in metrics["date"].dropna().unique()]
            if len(values) == 1:
                return str(normalize_json_value(values[0]))
        raise ArtifactValidationError("Unable to infer a single artifact asof date")

    @staticmethod
    def _fill_contract_columns(
        frame: pd.DataFrame,
        combination: ArtifactCombination,
        *,
        asof: str,
    ) -> pd.DataFrame:
        result = frame.copy(deep=True)
        combo = combination.normalized()
        values = {
            "date": asof,
            "universe": combo.universe,
            "taxonomy": combo.taxonomy,
            "level": combo.level,
            "mode": combo.mode,
            "snapshot_id": "EOD" if combo.mode == "eod" else None,
        }
        for column, value in values.items():
            if value is None:
                continue
            if column not in result.columns:
                result[column] = value
            else:
                # These fields are publication context, not calculated data.
                # A domain frame may reserve the column with nulls before the
                # service supplies its as-of/combination context.
                result[column] = result[column].where(result[column].notna(), value)
        return result

    @staticmethod
    def _set_or_validate(mapping: dict[str, Any], key: str, value: Any) -> None:
        if key in mapping and mapping[key] is not None:
            supplied = normalize_json_value(mapping[key])
            expected = normalize_json_value(value)
            if supplied != expected:
                raise ArtifactValidationError(
                    f"{key} does not match publication context",
                    details={"field": key, "supplied": supplied, "expected": expected},
                )
        mapping[key] = value

    def _prepare_bundle(
        self,
        run_id: str,
        combination: ArtifactCombination,
        bundle: GroupAnalyticsBundle,
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any], dict[str, Any], dict[str, Any]]:
        combo = combination.normalized()
        manifest = dict(bundle.manifest)
        run = dict(bundle.run)
        asof = self._infer_asof(manifest, run, bundle.metrics)
        metrics = self._fill_contract_columns(bundle.metrics, combo, asof=asof)
        members = self._fill_contract_columns(bundle.members, combo, asof=asof)
        contributions = self._fill_contract_columns(bundle.contributions, combo, asof=asof)

        now = _utc_now()
        expected_parameter_hash = compute_parameter_hash(self.settings)
        expected_runtime_hash = compute_runtime_config_hash(self.settings)
        for key, value in (
            ("schema_version", SCHEMA_VERSION),
            ("algorithm_version", ALGORITHM_VERSION),
            ("run_id", run_id),
            ("parameter_hash", expected_parameter_hash),
            ("runtime_config_hash", expected_runtime_hash),
            ("universe", combo.universe),
            ("taxonomy", combo.taxonomy),
            ("taxonomy_level", combo.level),
            ("mode", combo.mode),
            ("asof", asof),
        ):
            self._set_or_validate(manifest, key, value)
        manifest.setdefault("generated_at", now)
        manifest.setdefault("snapshot_id", "EOD" if combo.mode == "eod" else None)

        run.update(
            {
                "run_id": run_id,
                "last_attempt_status": RunStatus.SUCCESS,
                "finished_at": now,
                "combination": combo.as_dict(),
                "schema_version": SCHEMA_VERSION,
                "algorithm_version": ALGORITHM_VERSION,
                "parameter_hash": expected_parameter_hash,
                "runtime_config_hash": expected_runtime_hash,
                "asof": asof,
                "error": None,
            }
        )
        diagnostics = normalize_json_value(bundle.diagnostics)
        if not isinstance(diagnostics, dict):
            raise ArtifactValidationError("diagnostics must be a JSON object")
        validate_bundle_frames(
            metrics,
            members,
            contributions,
            combo,
            asof=asof,
        )
        return metrics, members, contributions, diagnostics, manifest, run

    @staticmethod
    def _write_frame(frame: pd.DataFrame, path: Path) -> None:
        frame.to_parquet(path, index=False, compression="snappy")
        with path.open("rb") as handle:
            os.fsync(handle.fileno())

    def _write_and_validate_staging(
        self,
        staging_dir: Path,
        *,
        run_id: str,
        combination: ArtifactCombination,
        metrics: pd.DataFrame,
        members: pd.DataFrame,
        contributions: pd.DataFrame,
        diagnostics: dict[str, Any],
        manifest: dict[str, Any],
        run: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self._write_frame(metrics, staging_dir / DAILY_METRICS_FILE)
        self._write_frame(members, staging_dir / MEMBERS_FILE)
        self._write_frame(contributions, staging_dir / CONTRIBUTIONS_FILE)
        _write_json_non_atomic(staging_dir / DIAGNOSTICS_FILE, diagnostics)

        row_counts = {
            DAILY_METRICS_FILE: len(metrics),
            MEMBERS_FILE: len(members),
            CONTRIBUTIONS_FILE: len(contributions),
        }
        run["output_row_counts"] = {
            "daily_metrics": len(metrics),
            "members": len(members),
            "member_contributions": len(contributions),
        }
        _write_json_non_atomic(staging_dir / RUN_FILE, run)

        file_hashes = {
            name: sha256_file(staging_dir / name)
            for name in PUBLISHED_FILES
        }
        manifest["output_files"] = list(PUBLISHED_FILES)
        manifest["file_hashes"] = file_hashes
        manifest["row_counts"] = row_counts
        _write_json_non_atomic(staging_dir / MANIFEST_FILE, manifest)
        _fsync_directory(staging_dir)

        loaded_manifest = _strict_json_load(staging_dir / MANIFEST_FILE)
        loaded_run = _strict_json_load(staging_dir / RUN_FILE)
        self._validate_staged_files(
            staging_dir,
            run_id=run_id,
            combination=combination,
            manifest=loaded_manifest,
            run=loaded_run,
        )
        return loaded_manifest, loaded_run

    @staticmethod
    def _validate_staged_files(
        directory: Path,
        *,
        run_id: str,
        combination: ArtifactCombination,
        manifest: Mapping[str, Any],
        run: Mapping[str, Any],
    ) -> None:
        combo = combination.normalized()
        if manifest.get("run_id") != run_id or run.get("run_id") != run_id:
            raise ArtifactValidationError("run_id is inconsistent within staged bundle")
        if manifest.get("universe") != combo.universe:
            raise ArtifactValidationError("manifest universe mismatch")
        if manifest.get("taxonomy") != combo.taxonomy:
            raise ArtifactValidationError("manifest taxonomy mismatch")
        if manifest.get("taxonomy_level") != combo.level or manifest.get("mode") != combo.mode:
            raise ArtifactValidationError("manifest level/mode mismatch")
        if manifest.get("asof") != run.get("asof"):
            raise ArtifactValidationError("manifest/run asof mismatch")

        output_files = manifest.get("output_files")
        if output_files != list(PUBLISHED_FILES):
            raise ArtifactValidationError("manifest output_files is not canonical")
        hashes = manifest.get("file_hashes")
        rows = manifest.get("row_counts")
        if not isinstance(hashes, dict) or not isinstance(rows, dict):
            raise ArtifactValidationError("manifest hashes/row_counts must be objects")
        for name in PUBLISHED_FILES:
            path = directory / name
            if not path.is_file():
                raise ArtifactValidationError(
                    "Published bundle is missing a file", details={"file": name}
                )
            expected_hash = hashes.get(name)
            if not isinstance(expected_hash, str) or not _SHA256_RE.fullmatch(expected_hash):
                raise ArtifactValidationError(
                    "Manifest contains invalid file hash", details={"file": name}
                )
            if sha256_file(path) != expected_hash:
                raise ArtifactValidationError(
                    "Artifact file hash mismatch", details={"file": name}
                )

        metrics = pd.read_parquet(directory / DAILY_METRICS_FILE)
        members = pd.read_parquet(directory / MEMBERS_FILE)
        contributions = pd.read_parquet(directory / CONTRIBUTIONS_FILE)
        actual_rows = {
            DAILY_METRICS_FILE: len(metrics),
            MEMBERS_FILE: len(members),
            CONTRIBUTIONS_FILE: len(contributions),
        }
        if actual_rows != rows:
            raise ArtifactValidationError(
                "Manifest row counts do not match Parquet files",
                details={"expected": rows, "actual": actual_rows},
            )
        validate_bundle_frames(
            metrics,
            members,
            contributions,
            combo,
            asof=str(manifest.get("asof") or ""),
        )
        for frame_name, frame in (
            (DAILY_METRICS_FILE, metrics),
            (MEMBERS_FILE, members),
            (CONTRIBUTIONS_FILE, contributions),
        ):
            if "run_id" in frame.columns:
                values = set(frame["run_id"].dropna().astype(str).unique())
                if values != {run_id}:
                    raise ArtifactValidationError(
                        "Parquet run_id mismatch",
                        details={"file": frame_name, "values": sorted(values)},
                    )

    def publish(
        self,
        *,
        run_id: str,
        combination: ArtifactCombination,
        bundle: GroupAnalyticsBundle,
        dry_run: bool = False,
    ) -> RunOutcome:
        run_id = _validate_run_id(run_id)
        combo = combination.normalized()
        if not self.paths.attempt_dir(run_id).exists():
            self.new_run_id(run_id)
        state = _strict_json_load(self.paths.attempt_dir(run_id) / RUN_FILE)
        if state.get("combination") is None:
            state = self.record_running(
                run_id,
                combo,
                {"asof": bundle.manifest.get("asof") or bundle.run.get("asof")},
                dry_run=dry_run,
            )
        elif state.get("combination") != combo.as_dict():
            raise ArtifactValidationError("Run attempt combination does not match publish call")
        elif str(state.get("last_attempt_status")) != RunStatus.RUNNING.value:
            raise RunIdCollisionError(f"Run id is already terminal: {run_id}")
        elif state.get("dry_run") is not None and bool(state.get("dry_run")) != bool(dry_run):
            raise ArtifactValidationError("Run attempt dry_run mode does not match publish call")

        combo_dir = self.paths.combo_dir(combo)
        staging_dir: Path | None = None
        final_dir = combo_dir / "runs" / run_id
        pointer_switched = False
        published_locator: str | None = None
        published_asof: str | None = None
        published_success_attempt: dict[str, Any] | None = None
        published_diagnostics: dict[str, Any] | None = None
        try:
            with self._combo_lock(combo):
                if final_dir.exists():
                    raise RunIdCollisionError(
                        f"Immutable run directory already exists: {run_id}"
                    )
                staging_root = self.paths._assert_confined(
                    combo_dir / ".staging",
                    combo_dir,
                    field="staging_root",
                )
                staging_root.mkdir(parents=True, exist_ok=True)
                staging_dir = Path(
                    tempfile.mkdtemp(prefix=f".{run_id}.", dir=str(staging_root))
                )
                metrics, members, contributions, diagnostics, manifest, run = self._prepare_bundle(
                    run_id, combo, bundle
                )
                attempt_existing = _strict_json_load(
                    self.paths.attempt_dir(run_id) / RUN_FILE
                )
                started_at = str(attempt_existing.get("started_at") or _utc_now())
                run["started_at"] = started_at
                manifest["attempt_started_at"] = started_at
                if not dry_run:
                    self._assert_monotonic_publication(
                        combo,
                        candidate_run_id=run_id,
                        candidate_asof=str(manifest.get("asof")),
                        candidate_started_at=started_at,
                    )
                planned_locator = None if dry_run else self.paths.locator_for(final_dir)
                run["artifact_locator"] = planned_locator
                run["dry_run"] = bool(dry_run)
                manifest, run = self._write_and_validate_staging(
                    staging_dir,
                    run_id=run_id,
                    combination=combo,
                    metrics=metrics,
                    members=members,
                    contributions=contributions,
                    diagnostics=diagnostics,
                    manifest=manifest,
                    run=run,
                )

                if dry_run:
                    success_attempt = dict(attempt_existing)
                    success_attempt.update(run)
                    success_attempt.update(
                        {
                            "last_attempt_status": RunStatus.SUCCESS,
                            "started_at": started_at,
                            "finished_at": _utc_now(),
                            "combination": combo.as_dict(),
                            "artifact_locator": None,
                            "dry_run": True,
                            "published": False,
                        }
                    )
                    _atomic_write_json(
                        self.paths.attempt_dir(run_id) / RUN_FILE,
                        success_attempt,
                    )
                    _atomic_write_json(
                        self.paths.attempt_dir(run_id) / DIAGNOSTICS_FILE,
                        diagnostics,
                    )
                    return RunOutcome(
                        run_id=run_id,
                        status=RunStatus.SUCCESS,
                        dry_run=True,
                        published=False,
                        combination=combo,
                        asof=manifest.get("asof"),
                    )

                runs_root = self.paths._assert_confined(
                    final_dir.parent,
                    combo_dir,
                    field="runs_root",
                )
                runs_root.mkdir(parents=True, exist_ok=True)
                os.rename(staging_dir, final_dir)
                staging_dir = None
                _fsync_directory(final_dir.parent)
                locator = str(planned_locator)
                pointer = {
                    "run_id": run_id,
                    "data_run_id": run_id,
                    "artifact_locator": locator,
                    "combination": combo.as_dict(),
                    "asof": manifest.get("asof"),
                    "generated_at": manifest.get("generated_at"),
                    "schema_version": manifest.get("schema_version"),
                    "algorithm_version": manifest.get("algorithm_version"),
                    "parameter_hash": manifest.get("parameter_hash"),
                    "attempt_started_at": started_at,
                }
                # This is the sole publication point. os.replace keeps the old
                # pointer intact if writing the replacement fails.
                _atomic_write_json(combo_dir / "latest_success.json", pointer)
                pointer_switched = True
                published_locator = locator
                published_asof = manifest.get("asof")
                published_diagnostics = diagnostics

                success_attempt = dict(attempt_existing)
                success_attempt.update(run)
                success_attempt.update(
                    {
                        "last_attempt_status": RunStatus.SUCCESS,
                        "started_at": started_at,
                        "finished_at": _utc_now(),
                        "combination": combo.as_dict(),
                        "artifact_locator": locator,
                        "dry_run": False,
                        "published": True,
                    }
                )
                published_success_attempt = success_attempt
                _atomic_write_json(
                    self.paths.attempt_dir(run_id) / RUN_FILE,
                    success_attempt,
                )
                _atomic_write_json(
                    self.paths.attempt_dir(run_id) / DIAGNOSTICS_FILE,
                    diagnostics,
                )
                self._write_last_attempt_unlocked(
                    combo,
                    self._last_attempt_payload(success_attempt),
                )
                return RunOutcome(
                    run_id=run_id,
                    status=RunStatus.SUCCESS,
                    dry_run=False,
                    published=True,
                    combination=combo,
                    asof=manifest.get("asof"),
                    artifact_locator=locator,
                )
        except Exception as exc:  # publication failures are persisted as data
            if staging_dir is not None and staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            if pointer_switched:
                # The atomic latest pointer is the commit record.  A later
                # bookkeeping I/O failure cannot safely turn this into a
                # failed publication (readers may already have observed it).
                # Reconcile the secondary records best-effort and report the
                # committed run as successful.
                try:
                    if published_success_attempt is not None:
                        _atomic_write_json(
                            self.paths.attempt_dir(run_id) / RUN_FILE,
                            published_success_attempt,
                        )
                        if published_diagnostics is not None:
                            _atomic_write_json(
                                self.paths.attempt_dir(run_id) / DIAGNOSTICS_FILE,
                                published_diagnostics,
                            )
                        with self._combo_lock(combo):
                            self._write_last_attempt_unlocked(
                                combo,
                                self._last_attempt_payload(published_success_attempt),
                            )
                except Exception:
                    pass
                return RunOutcome(
                    run_id=run_id,
                    status=RunStatus.SUCCESS,
                    dry_run=False,
                    published=True,
                    combination=combo,
                    asof=published_asof,
                    artifact_locator=published_locator,
                )
            try:
                with self._combo_lock(combo):
                    return self._record_failure_unlocked(
                        run_id,
                        combo,
                        exc,
                        {"asof": bundle.manifest.get("asof") or bundle.run.get("asof")},
                        dry_run=dry_run,
                        diagnostics=bundle.diagnostics,
                    )
            except Exception:
                # Never mask the original publication failure merely because
                # failure-state persistence also encountered an I/O problem.
                return RunOutcome(
                    run_id=run_id,
                    status=RunStatus.FAILED,
                    dry_run=bool(dry_run),
                    published=False,
                    combination=combo,
                    asof=bundle.manifest.get("asof") or bundle.run.get("asof"),
                    error=_error_payload(exc),
                )
        finally:
            if staging_dir is not None and staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)

    def find_matching_latest(
        self,
        combination: ArtifactCombination,
        *,
        parameter_hash: str | None = None,
        input_fingerprint: str | None = None,
        asof: str | None = None,
        manifest_match: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Return fixed latest metadata when every requested identity matches."""
        reader = ArtifactReader(self.settings)
        try:
            pointer, run_dir = reader.resolve_latest(combination)
        except NoSuccessfulRunError:
            return None
        manifest = _strict_json_load(run_dir / MANIFEST_FILE)
        expected = dict(manifest_match or {})
        if parameter_hash is not None:
            expected["parameter_hash"] = parameter_hash
        if input_fingerprint is not None:
            expected["input_fingerprint"] = input_fingerprint
        if asof is not None:
            expected["asof"] = asof
        if any(normalize_json_value(manifest.get(key)) != normalize_json_value(value) for key, value in expected.items()):
            return None
        return {
            "run_id": pointer["run_id"],
            "pointer": pointer,
            "manifest": manifest,
            "run_dir": run_dir,
        }

    def load_latest(self, combination: ArtifactCombination) -> LoadedArtifactRun:
        return ArtifactReader(self.settings).load_latest(combination)

    def load_run(
        self,
        run_id: str,
        combination: ArtifactCombination | None = None,
    ) -> LoadedArtifactRun:
        return ArtifactReader(self.settings).load_run(run_id, combination)

    def load_attempt(self, run_id: str) -> dict[str, Any]:
        return ArtifactReader(self.settings).load_attempt(run_id)

    def load_last_attempt(self, combination: ArtifactCombination) -> dict[str, Any] | None:
        return ArtifactReader(self.settings).load_last_attempt(combination)

    def scan_metadata(self, *, strict: bool = True) -> list[dict[str, Any]]:
        return ArtifactReader(self.settings).scan_metadata(strict=strict)


class ArtifactReader:
    """Read-only artifact facade; each request pins one immutable run first."""

    def __init__(
        self,
        settings: GroupAnalyticsSettings | None = None,
        *,
        output_root: Path | str | None = None,
    ):
        self.settings = _coerce_settings(settings, output_root)
        self.paths = _ArtifactPaths(self.settings)

    @property
    def output_root(self) -> Path:
        return self.paths.output_root

    @property
    def artifact_root(self) -> Path:
        return self.paths.artifact_root

    def resolve_latest(
        self,
        combination: ArtifactCombination,
    ) -> tuple[dict[str, Any], Path]:
        """Resolve latest_success exactly once and return its immutable path."""
        combo = combination.normalized()
        pointer_path = self.paths.combo_dir(combo) / "latest_success.json"
        if not pointer_path.exists():
            raise NoSuccessfulRunError(
                "No successful group-analytics run exists for this combination",
                details={"combination": combo.as_dict()},
            )
        pointer = _strict_json_load(pointer_path)
        run_id = _validate_run_id(str(pointer.get("run_id") or ""))
        if pointer.get("combination") != combo.as_dict():
            raise ArtifactValidationError("latest_success combination mismatch")
        locator = pointer.get("artifact_locator")
        if not isinstance(locator, str):
            raise ArtifactValidationError("latest_success has no artifact_locator")
        run_dir = self.paths.resolve_locator(locator, run_id=run_id)
        return pointer, run_dir

    def _load_fixed(
        self,
        run_id: str,
        combination: ArtifactCombination,
        run_dir: Path,
        *,
        pointer: dict[str, Any] | None,
    ) -> LoadedArtifactRun:
        if not run_dir.is_dir():
            raise ArtifactNotFoundError(
                f"Immutable artifact run not found: {run_id}",
                details={"run_id": run_id},
            )
        manifest = _strict_json_load(run_dir / MANIFEST_FILE)
        run = _strict_json_load(run_dir / RUN_FILE)
        FileGroupArtifactStore._validate_staged_files(
            run_dir,
            run_id=run_id,
            combination=combination,
            manifest=manifest,
            run=run,
        )
        diagnostics = _strict_json_load(run_dir / DIAGNOSTICS_FILE)
        metrics = pd.read_parquet(run_dir / DAILY_METRICS_FILE)
        members = pd.read_parquet(run_dir / MEMBERS_FILE)
        contributions = pd.read_parquet(run_dir / CONTRIBUTIONS_FILE)
        return LoadedArtifactRun(
            run_id=run_id,
            combination=combination.normalized(),
            path=run_dir,
            pointer=pointer,
            run=run,
            manifest=manifest,
            diagnostics=diagnostics,
            metrics=metrics,
            members=members,
            contributions=contributions,
        )

    def load_latest(self, combination: ArtifactCombination) -> LoadedArtifactRun:
        pointer, run_dir = self.resolve_latest(combination)
        # Do not consult latest_success again after this line.
        return self._load_fixed(
            str(pointer["run_id"]),
            combination,
            run_dir,
            pointer=pointer,
        )

    def load_attempt(self, run_id: str) -> dict[str, Any]:
        run_id = _validate_run_id(run_id)
        attempt_dir = self.paths.attempt_dir(run_id)
        run = _strict_json_load(attempt_dir / RUN_FILE)
        diagnostics = (
            _strict_json_load(attempt_dir / DIAGNOSTICS_FILE)
            if (attempt_dir / DIAGNOSTICS_FILE).exists()
            else {}
        )
        return {**run, "diagnostics": diagnostics}

    def load_last_attempt(self, combination: ArtifactCombination) -> dict[str, Any] | None:
        path = self.paths.combo_dir(combination) / "last_attempt.json"
        return _strict_json_load(path) if path.exists() else None

    def load_run(
        self,
        run_id: str,
        combination: ArtifactCombination | None = None,
    ) -> LoadedArtifactRun:
        run_id = _validate_run_id(run_id)
        if combination is None:
            attempt = self.load_attempt(run_id)
            combo_value = attempt.get("combination")
            if not isinstance(combo_value, dict):
                raise ArtifactNotFoundError(
                    "Run attempt has no artifact combination",
                    details={"run_id": run_id},
                )
            combination = ArtifactCombination(
                universe=str(combo_value.get("universe") or ""),
                taxonomy=str(combo_value.get("taxonomy") or ""),
                level=str(combo_value.get("level") or ""),
                mode=str(combo_value.get("mode") or ""),
            ).normalized()
            locator = attempt.get("artifact_locator")
            if not isinstance(locator, str):
                raise ArtifactNotFoundError(
                    "Run attempt has no published artifact",
                    details={
                        "run_id": run_id,
                        "status": attempt.get("last_attempt_status"),
                    },
                )
            run_dir = self.paths.resolve_locator(locator, run_id=run_id)
        else:
            combination = combination.normalized()
            run_dir = self.paths.combo_dir(combination) / "runs" / run_id
        return self._load_fixed(
            run_id,
            combination,
            run_dir,
            pointer=None,
        )

    def scan_metadata(self, *, strict: bool = True) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        pattern = "*/group_analytics/*/*/*/latest_success.json"
        for pointer_path in sorted(self.artifact_root.glob(pattern)):
            try:
                relative = pointer_path.parent.relative_to(self.artifact_root)
                if len(relative.parts) != 5 or relative.parts[1] != "group_analytics":
                    continue
                combination = ArtifactCombination(
                    universe=relative.parts[0],
                    taxonomy=relative.parts[2],
                    level=relative.parts[3],
                    mode=relative.parts[4],
                ).normalized()
                # This scan also resolves each pointer once, then reads only
                # from the selected immutable run.
                pointer = _strict_json_load(pointer_path)
                run_id = _validate_run_id(str(pointer.get("run_id") or ""))
                locator = pointer.get("artifact_locator")
                if pointer.get("combination") != combination.as_dict() or not isinstance(locator, str):
                    raise ArtifactValidationError("Metadata pointer is inconsistent")
                run_dir = self.paths.resolve_locator(locator, run_id=run_id)
                manifest = _strict_json_load(run_dir / MANIFEST_FILE)
                rows.append(
                    {
                        "universe": combination.universe,
                        "taxonomy": combination.taxonomy,
                        "level": combination.level,
                        "mode": combination.mode,
                        "latest_run_id": run_id,
                        "latest_asof": manifest.get("asof"),
                        "schema_version": manifest.get("schema_version"),
                        "algorithm_version": manifest.get("algorithm_version"),
                        "parameter_hash": manifest.get("parameter_hash"),
                        "generated_at": manifest.get("generated_at"),
                    }
                )
            except GroupAnalyticsError:
                if strict:
                    raise
        return sorted(
            rows,
            key=lambda row: (
                row["universe"],
                row["taxonomy"],
                row["level"],
                row["mode"],
            ),
        )

    def metadata(self, *, strict: bool = True) -> dict[str, Any]:
        combinations = self.scan_metadata(strict=strict)
        defaults = {
            "universe": self.settings.default_universe,
            "taxonomy": self.settings.classification.default_taxonomy,
            "level": self.settings.classification.default_level,
            "mode": "eod",
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "defaults": defaults,
            "features": {
                "heat": bool(combinations),
                "live": any(item["mode"] == "live" for item in combinations),
                "momentum": False,
                "themes": False,
                "history": False,
            },
            "available_combinations": combinations,
        }


# Concise aliases for callers and tests; the descriptive class name remains the
# canonical implementation.
ArtifactStore = FileGroupArtifactStore
GroupAnalyticsArtifactStore = FileGroupArtifactStore
strict_json_value = normalize_json_value


__all__ = [
    "ArtifactReader",
    "ArtifactStore",
    "ArtifactValidationError",
    "ConcurrentWriterError",
    "CONTRIBUTIONS_FILE",
    "CONTRIBUTIONS_PRIMARY_KEY",
    "DAILY_METRICS_FILE",
    "DAILY_PRIMARY_KEY",
    "FileGroupArtifactStore",
    "GroupAnalyticsArtifactStore",
    "LoadedArtifactRun",
    "MANIFEST_FILE",
    "MEMBERS_FILE",
    "MEMBERS_PRIMARY_KEY",
    "OutOfOrderPublicationError",
    "RunIdCollisionError",
    "canonical_hash",
    "canonical_json_bytes",
    "compute_parameter_hash",
    "compute_runtime_config_hash",
    "generate_run_id",
    "normalize_json_value",
    "sha256_file",
    "strict_json_value",
    "validate_bundle_frames",
]
