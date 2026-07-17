"""Read and gate immutable sector/sub-industry group artifacts."""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any

import pandas as pd

from src.group_analytics.aggregation import rank_group_metrics
from src.group_analytics.artifacts import ArtifactReader, LoadedArtifactRun
from src.group_analytics.models import ArtifactCombination
from src.group_analytics.settings import load_group_analytics_settings

from .models import SourceGateError
from .settings import PremarketDigestSettings


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: Any, *, fallback: str = "") -> str:
    if value is None or value is pd.NA:
        return fallback
    try:
        if bool(pd.isna(value)):
            return fallback
    except (TypeError, ValueError):
        pass
    text = str(value)
    return text if text else fallback


def _row_record(row: dict[str, Any]) -> dict[str, Any]:
    group_id = _text(row.get("group_id"))
    return {
        "group_id": group_id,
        "group_name": _text(row.get("group_name"), fallback=group_id)[:72],
        "robust_ew_return_1d": _finite(row.get("robust_ew_return_1d")),
        "up_pct": _finite(row.get("up_pct")),
        "n_valid": int(_finite(row.get("n_valid")) or 0),
        "n_expected": int(_finite(row.get("n_expected")) or 0),
        "count_coverage": _finite(row.get("count_coverage")),
        "headline_relative_return_1d": _finite(row.get("headline_relative_return_1d")),
        "top_driver_ticker": _text(row.get("top_driver_ticker")),
        "bottom_driver_ticker": _text(row.get("bottom_driver_ticker")),
    }


class GroupArtifactDigestSource:
    def __init__(
        self,
        settings: PremarketDigestSettings,
        *,
        reader: ArtifactReader | None = None,
        now: datetime | None = None,
    ) -> None:
        self.settings = settings
        group_settings = load_group_analytics_settings()
        self.reader = reader or ArtifactReader(group_settings)
        self.now = now or datetime.now(timezone.utc)

    def _load_level(
        self,
        level: str,
        source_session: str,
        *,
        top_n: int,
        bottom_n: int,
    ) -> dict[str, Any]:
        combination = ArtifactCombination(
            self.settings.group_universe,
            self.settings.group_taxonomy,
            level,
            "eod",
        )
        try:
            loaded = self.reader.load_latest(combination)
        except Exception as exc:  # noqa: BLE001
            raise SourceGateError(
                "GROUP_ARTIFACT_UNAVAILABLE",
                f"{level} immutable artifact could not be loaded",
                details={"level": level, "error_type": type(exc).__name__},
            ) from None
        self._validate_manifest(loaded, level, source_session)
        metrics = loaded.metrics.copy(deep=True)
        if metrics.empty:
            raise SourceGateError(
                "GROUP_NO_DATA", f"{level} artifact has no group metrics"
            )
        _, top, bottom = rank_group_metrics(
            metrics,
            top_n=top_n,
            bottom_n=bottom_n,
        )
        if top.empty or bottom.empty:
            raise SourceGateError(
                "GROUP_NOT_RANKABLE",
                f"{level} artifact has too few eligible groups",
                details={"level": level},
            )
        quality = loaded.manifest.get("quality_summary")
        quality = dict(quality) if isinstance(quality, dict) else {}
        benchmark_values = pd.to_numeric(
            metrics.get("benchmark_return_1d", pd.Series(dtype=float)),
            errors="coerce",
        )
        benchmark_values = benchmark_values[
            benchmark_values.notna() & benchmark_values.map(math.isfinite)
        ]
        try:
            last_attempt = self.reader.load_last_attempt(combination)
        except Exception:
            last_attempt = None
        warning = None
        if isinstance(last_attempt, dict) and str(
            last_attempt.get("last_attempt_status") or ""
        ) == "FAILED":
            warning = "较新的计算尝试失败；本摘要使用最后一个校验成功的同日产物。"
        return {
            "level": level,
            "run_id": loaded.run_id,
            "source_session": source_session,
            "algorithm_version": loaded.manifest.get("algorithm_version"),
            "taxonomy_version": loaded.manifest.get("taxonomy_version"),
            "quality_status": loaded.manifest.get("quality_status"),
            "quality_summary": quality,
            "benchmark": loaded.manifest.get("benchmark") or "SPY",
            "benchmark_return_1d": (
                None if benchmark_values.empty else float(benchmark_values.iloc[0])
            ),
            "warning": warning,
            "top": [_row_record(row) for row in top.to_dict(orient="records")],
            "bottom": [_row_record(row) for row in bottom.to_dict(orient="records")],
        }

    def _validate_manifest(
        self,
        loaded: LoadedArtifactRun,
        level: str,
        source_session: str,
    ) -> None:
        manifest = loaded.manifest
        expected = {
            "asof": source_session,
            "mode": "eod",
            "snapshot_id": "EOD",
            "session_status": "FINAL",
            "universe": self.settings.group_universe,
            "taxonomy": self.settings.group_taxonomy,
            "taxonomy_level": level,
        }
        mismatches = {
            key: {"expected": value, "actual": manifest.get(key)}
            for key, value in expected.items()
            if str(manifest.get(key) or "") != str(value)
        }
        if mismatches:
            raise SourceGateError(
                "GROUP_MANIFEST_MISMATCH",
                f"{level} artifact is not the required completed session",
                details={"level": level, "mismatches": mismatches},
            )
        if manifest.get("research_only") is not False:
            raise SourceGateError(
                "GROUP_RESEARCH_ONLY",
                f"{level} artifact is research-only and cannot be sent",
            )
        quality_status = str(manifest.get("quality_status") or "")
        if quality_status not in {"OK", "LOW_COVERAGE"}:
            raise SourceGateError(
                "GROUP_INVALID_QUALITY_STATUS",
                f"{level} artifact does not have a usable quality status",
                details={"level": level, "quality_status": quality_status},
            )
        quality = manifest.get("quality_summary")
        coverage = _finite(
            quality.get("count_coverage") if isinstance(quality, dict) else None
        )
        if coverage is None or coverage < self.settings.group_min_coverage:
            raise SourceGateError(
                "GROUP_LOW_COVERAGE",
                f"{level} artifact coverage is below the send gate",
                details={
                    "level": level,
                    "coverage": coverage,
                    "minimum": self.settings.group_min_coverage,
                },
            )
        generated_at = manifest.get("generated_at")
        if not generated_at:
            raise SourceGateError(
                "GROUP_INVALID_GENERATED_AT",
                f"{level} manifest generated_at is missing",
            )
        try:
            generated = pd.Timestamp(generated_at)
            if generated.tzinfo is None:
                generated = generated.tz_localize("UTC")
            else:
                generated = generated.tz_convert("UTC")
            now = pd.Timestamp(self.now)
            if now.tzinfo is None:
                now = now.tz_localize("UTC")
            else:
                now = now.tz_convert("UTC")
            if generated > now + pd.Timedelta(minutes=1):
                raise SourceGateError(
                    "GROUP_FUTURE_ARTIFACT",
                    f"{level} artifact was generated in the future",
                )
        except SourceGateError:
            raise
        except Exception:
            raise SourceGateError(
                "GROUP_INVALID_GENERATED_AT",
                f"{level} manifest generated_at is invalid",
            ) from None

    def load(self, source_session: str) -> dict[str, Any]:
        specs = (
            (
                "sector",
                self.settings.sector_top_n,
                self.settings.sector_bottom_n,
            ),
            (
                "sub_industry",
                self.settings.sub_industry_top_n,
                self.settings.sub_industry_bottom_n,
            ),
        )
        levels: dict[str, dict[str, Any]] = {}
        errors: dict[str, dict[str, Any]] = {}
        for level, top_n, bottom_n in specs:
            try:
                levels[level] = self._load_level(
                    level,
                    source_session,
                    top_n=top_n,
                    bottom_n=bottom_n,
                )
            except SourceGateError as exc:
                errors[level] = {"code": exc.code, "details": exc.details}
        if not levels:
            raise SourceGateError(
                "GROUP_ALL_LEVELS_UNAVAILABLE",
                "neither sector nor sub-industry passed the T-1 artifact gate",
                details={"levels": errors},
            )
        return {
            "source_session": source_session,
            "universe": self.settings.group_universe,
            "taxonomy": self.settings.group_taxonomy,
            "methodology": "ROBUST_EW / MAD winsor",
            "partial": bool(errors),
            "errors": errors,
            "levels": levels,
        }


__all__ = ["GroupArtifactDigestSource"]
