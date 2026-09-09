"""Stage-1 group-analytics orchestration.

This service is the only write-side entry point used by the dedicated CLI.
It deliberately composes leaf providers owned by this package and never calls
the factor, backtest, paper-trading, alert, or web application domains.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import subprocess
import time
from typing import Any, Callable, Mapping

import numpy as np
import pandas as pd

from src.config import PROJECT_ROOT

from . import ALGORITHM_VERSION, SCHEMA_VERSION
from .adapters import FMPCurrentClassificationProvider, PublishedEODMarketDataProvider
from .aggregation import GroupAggregationResult, aggregate_groups
from .artifacts import (
    FileGroupArtifactStore,
    canonical_hash,
    compute_parameter_hash,
    compute_runtime_config_hash,
)
from .calendar import (
    latest_completed_session,
    official_session_close,
    previous_session,
)
from .classification import build_counting_units, load_issuer_overrides
from .models import (
    ArtifactCombination,
    FeatureDisabledError,
    GroupAnalyticsBundle,
    GroupAnalyticsError,
    InputCoverageError,
    InvalidRequestError,
    QualityStatus,
    ReasonCode,
    RunOutcome,
    RunRequest,
    UnsupportedCombinationError,
    sorted_reason_codes,
)
from .returns import compute_eod_return_audit, compute_eod_returns
from .settings import GroupAnalyticsSettings, load_group_analytics_settings


class PointInTimeDataUnavailableError(GroupAnalyticsError):
    code = "PIT_DATA_UNAVAILABLE"
    stage = "validate_point_in_time_inputs"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime | pd.Timestamp) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.isoformat().replace("+00:00", "Z")


def _date_label(value: str | pd.Timestamp) -> pd.Timestamp:
    try:
        parsed = pd.Timestamp(value)
    except Exception as exc:  # noqa: BLE001
        raise InvalidRequestError(
            "asof must be 'latest' or an ISO-8601 date",
            details={"asof": str(value)},
        ) from exc
    if pd.isna(parsed) or parsed.time() != datetime.min.time():
        raise InvalidRequestError(
            "Explicit asof must be a date without a time component",
            details={"asof": str(value)},
        )
    if parsed.tzinfo is not None:
        parsed = parsed.tz_localize(None)
    return parsed.normalize()


def _hash_frame(frame: pd.DataFrame) -> str:
    """Hash a numeric input frame independent of source row/column order."""
    ordered = frame.copy(deep=False)
    ordered = ordered.sort_index().sort_index(axis=1)
    hashed = pd.util.hash_pandas_object(ordered, index=True, categorize=False)
    digest = hashlib.sha256()
    digest.update(hashed.to_numpy(dtype="uint64").tobytes())
    digest.update("\x1f".join(map(str, ordered.columns)).encode("utf-8"))
    digest.update("\x1f".join(map(str, ordered.dtypes)).encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def _merge_reason_codes(*values: object) -> list[str]:
    result: list[str | ReasonCode] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, (str, ReasonCode)):
            result.append(value)
        elif isinstance(value, (list, tuple, set, frozenset)):
            result.extend(value)
    return sorted_reason_codes(result)


def _git_provenance() -> dict[str, str | None]:
    """Best-effort code provenance; a missing Git executable never blocks data."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        dirty_hash: str | None = None
        if status:
            digest = hashlib.sha256()
            tracked_diff = subprocess.run(
                ["git", "diff", "--binary", "HEAD", "--"],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
                timeout=20,
            ).stdout
            digest.update(tracked_diff)
            untracked = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard", "-z"],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
                timeout=10,
            ).stdout.split(b"\0")
            for relative_bytes in sorted(value for value in untracked if value):
                relative = relative_bytes.decode("utf-8", errors="surrogateescape")
                path = (PROJECT_ROOT / relative).resolve()
                try:
                    path.relative_to(PROJECT_ROOT.resolve())
                except ValueError:
                    continue
                if path.is_file():
                    digest.update(relative_bytes)
                    digest.update(b"\0")
                    digest.update(path.read_bytes())
            dirty_hash = "sha256:" + digest.hexdigest()
        return {
            "code_version": f"{commit[:12]}{'-dirty' if status else ''}",
            "git_commit": commit,
            "dirty_hash": dirty_hash,
        }
    except Exception:  # noqa: BLE001
        return {"code_version": "unknown", "git_commit": None, "dirty_hash": None}


def _file_states(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted({Path(item) for item in paths}, key=lambda item: str(item)):
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append(
            {
                "path": str(path),
                "size_bytes": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
            }
        )
    return rows


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    return frame.where(pd.notna(frame), None).to_dict(orient="records")


class GroupAnalyticsService:
    """Compute and publish the frozen Stage-1 current EOD heat snapshot."""

    def __init__(
        self,
        settings: GroupAnalyticsSettings | None = None,
        *,
        classification_provider: Any | None = None,
        market_provider: Any | None = None,
        artifact_store: FileGroupArtifactStore | None = None,
        now: Callable[[], datetime] = _utc_now,
        exchange_calendar: Any | None = None,
    ) -> None:
        self.settings = settings or load_group_analytics_settings()
        self.classification_provider = (
            classification_provider or FMPCurrentClassificationProvider(
                group_id_mapping_path=self.settings.group_id_mapping_path,
            )
        )
        self.market_provider = market_provider or PublishedEODMarketDataProvider(
            universe=self.settings.default_universe,
            require_benchmark=self.settings.inputs.require_benchmark,
        )
        self.artifact_store = artifact_store or FileGroupArtifactStore(self.settings)
        self._now = now
        self._exchange_calendar = exchange_calendar

    def _validate_request(self, request: RunRequest) -> tuple[ArtifactCombination, bool]:
        if not self.settings.enabled:
            # Disabled means no provider call and no attempt/artifact mutation.
            raise FeatureDisabledError(
                "group_analytics is disabled; set group_analytics.enabled=true to run it"
            )
        combo = request.combination
        allowed = {
            "universes": ["SP500"],
            "taxonomies": ["FMP"],
            "levels": ["sector", "sub_industry"],
            "modes": ["eod"],
        }
        if (
            combo.universe != "SP500"
            or combo.taxonomy != "FMP"
            or combo.level not in {"sector", "sub_industry"}
            or combo.mode != "eod"
        ):
            raise UnsupportedCombinationError(
                "Stage 1 supports only SP500/FMP sector or sub_industry in EOD mode",
                details={"requested": combo.as_dict(), "allowed_values": allowed},
            )
        if request.limit is not None and request.limit < 1:
            raise InvalidRequestError("limit must be a positive integer")
        dry_run = bool(
            request.dry_run
            or request.limit is not None
            or str(request.asof).lower() != "latest"
        )
        return combo, dry_run

    def _resolve_session(self, request: RunRequest) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, bool]:
        explicit_research = str(request.asof).lower() != "latest"
        if explicit_research:
            target = _date_label(request.asof)
        else:
            target = latest_completed_session(
                now=self._now(), calendar=self._exchange_calendar
            )
        previous = previous_session(target, calendar=self._exchange_calendar)
        close = official_session_close(target, calendar=self._exchange_calendar)
        return target, previous, close, explicit_research

    def run(self, request: RunRequest | None = None) -> RunOutcome:
        started_clock = time.perf_counter()
        request = request or RunRequest(
            universe=self.settings.default_universe,
            taxonomy=self.settings.classification.default_taxonomy,
            level=self.settings.classification.default_level,
        )
        combo, dry_run = self._validate_request(request)
        run_id = self.artifact_store.new_run_id(request.output_run_id)
        metadata: dict[str, Any] = {
            "requested_asof": request.asof,
            "input_row_counts": {},
            "output_row_counts": {},
            "diagnostic_counts": {},
        }
        diagnostics: dict[str, Any] = {
            "missing_members": [],
            "low_confidence_groups": [],
            "classification_diagnostics": [],
        }
        try:
            self.artifact_store.record_running(
                run_id, combo, metadata, dry_run=dry_run
            )
            if request.strict_pit:
                raise PointInTimeDataUnavailableError(
                    "Stage 1 current classification is not point-in-time and cannot satisfy --strict-pit",
                    details={
                        "reason_codes": [
                            ReasonCode.PIT_UNIVERSE_UNAVAILABLE,
                            ReasonCode.PIT_CLASSIFICATION_UNAVAILABLE,
                        ]
                    },
                )

            target, prior, close, explicit_research = self._resolve_session(request)
            asof = target.date().isoformat()
            if explicit_research:
                # A current FMP mapping cannot be formally published as history.
                dry_run = True
            metadata.update({"asof": asof, "dry_run": dry_run})

            classification = self.classification_provider.snapshot(
                universe=combo.universe,
                taxonomy=combo.taxonomy,
                level=combo.level,
                asof=asof,
                force=request.force,
            )
            universe_frame = classification.frame.copy(deep=True)
            if not self.settings.classification.include_etfs and "asset_type" in universe_frame:
                excluded = universe_frame["asset_type"].astype(str).str.upper().eq("ETF")
                if bool(excluded.any()):
                    diagnostics["classification_diagnostics"].extend(
                        {
                            "ticker": str(row.ticker),
                            "code": ReasonCode.ETF_EXCLUDED.value,
                        }
                        for row in universe_frame.loc[excluded].itertuples()
                    )
                universe_frame = universe_frame.loc[~excluded].copy()
            universe_frame = universe_frame.sort_values("ticker", kind="mergesort")
            if request.limit is not None:
                universe_frame = universe_frame.head(request.limit).copy()
            symbols = universe_frame["ticker"].astype(str).tolist()
            if not symbols:
                raise InputCoverageError("The requested universe has no eligible securities")

            market = self.market_provider.snapshot(
                symbols=symbols,
                benchmark=self.settings.benchmark,
                asof=target,
                force=request.force,
            )
            pair_index = pd.DatetimeIndex([prior, target], name="session")
            price_pair = market.adj_close.reindex(
                index=pair_index, columns=symbols
            )
            delisting_required = (
                universe_frame.set_index("ticker")["delisting_return_required"]
                if "delisting_return_required" in universe_frame.columns
                else None
            )
            return_audit = compute_eod_return_audit(
                price_pair,
                asof=target,
                delisting_return_required=delisting_required,
            )
            returns = return_audit["raw_return_1d"].copy()
            valid_security_returns = int(return_audit["is_valid_return"].sum())
            security_coverage = valid_security_returns / len(symbols)
            metadata["input_row_counts"] = {
                "universe": len(symbols),
                "returns": valid_security_returns,
            }
            if security_coverage < self.settings.inputs.min_return_coverage:
                diagnostics["missing_members"] = _records(
                    return_audit.loc[
                        ~return_audit["is_valid_return"],
                        [
                            "ticker",
                            "adj_close_t",
                            "adj_close_t_1",
                            "data_asof",
                            "reason_codes",
                        ],
                    ]
                )
                raise InputCoverageError(
                    "Adjusted-close return coverage is below the frozen publication gate",
                    details={
                        "asof": asof,
                        "n_expected": len(symbols),
                        "n_valid": valid_security_returns,
                        "count_coverage": security_coverage,
                        "minimum": self.settings.inputs.min_return_coverage,
                    },
                )

            reason_lookup = return_audit["reason_codes"].to_dict()
            universe_frame["reason_codes"] = [
                _merge_reason_codes(existing, reason_lookup.get(str(ticker)))
                for existing, ticker in zip(
                    universe_frame.get(
                        "reason_codes",
                        pd.Series([[] for _ in range(len(universe_frame))], index=universe_frame.index),
                    ),
                    universe_frame["ticker"],
                )
            ]
            overrides = load_issuer_overrides(self.settings.issuer_override_path)
            counting_units, counting_diagnostics = build_counting_units(
                universe_frame,
                security_returns=returns,
                asof=asof,
                overrides=overrides,
                liquidity_price=market.adj_close,
                volume=market.volume,
                market_cap=market.market_cap,
            )

            unassigned = counting_units[counting_units["group_id"].isna()].copy()
            if not unassigned.empty:
                diagnostics["classification_diagnostics"].extend(
                    _records(
                        unassigned[
                            [
                                "security_id",
                                "counting_unit_id",
                                "ticker",
                                "reason_codes",
                            ]
                        ]
                    )
                )
            aggregatable = counting_units[counting_units["group_id"].notna()].copy()
            if aggregatable.empty:
                raise InputCoverageError(
                    "No securities have a usable Stage-1 classification",
                    details={"n_counting_units": len(counting_units)},
                )

            benchmark_return: float | None = None
            benchmark_pair = market.benchmark_adj_close.reindex(index=pair_index)
            if self.settings.benchmark in benchmark_pair.columns:
                candidate = compute_eod_returns(benchmark_pair, asof=target).get(
                    self.settings.benchmark, np.nan
                )
                if pd.notna(candidate) and np.isfinite(float(candidate)):
                    benchmark_return = float(candidate)
            if benchmark_return is None and self.settings.inputs.require_benchmark:
                raise InputCoverageError(
                    "Benchmark adjusted-close return is unavailable",
                    details={"benchmark": self.settings.benchmark, "asof": asof},
                )

            result = aggregate_groups(
                aggregatable,
                settings=self.settings,
                benchmark_return_1d=benchmark_return,
            )
            self._enrich_frames(
                result,
                run_id=run_id,
                asof=asof,
                explicit_research=explicit_research,
            )

            n_expected = int(len(counting_units))
            n_valid = int(
                (
                    counting_units["group_id"].notna()
                    & counting_units["is_valid_for_headline"].fillna(False).astype(bool)
                ).sum()
            )
            overall_coverage = n_valid / n_expected if n_expected else None
            if n_valid == 0:
                quality_status = QualityStatus.NO_DATA.value
            elif overall_coverage is not None and overall_coverage < self.settings.inputs.min_return_coverage:
                quality_status = QualityStatus.LOW_COVERAGE.value
            else:
                quality_status = QualityStatus.OK.value
            low_confidence = result.metrics[
                ~result.metrics["eligible_for_ranking"].fillna(False).astype(bool)
            ]
            diagnostics["low_confidence_groups"] = _records(
                low_confidence[
                    [
                        "group_id",
                        "group_name",
                        "n_expected",
                        "n_valid",
                        "count_coverage",
                        "snapshot_quality_grade",
                        "reason_codes",
                    ]
                ]
            )
            invalid_members = counting_units[
                ~counting_units["is_valid_for_headline"].fillna(False).astype(bool)
            ]
            diagnostics["missing_members"] = _records(
                invalid_members[
                    [
                        "security_id",
                        "counting_unit_id",
                        "ticker",
                        "group_id",
                        "raw_return_1d",
                        "reason_codes",
                    ]
                ]
            )
            diagnostics["classification_diagnostics"] = [
                *classification.diagnostics,
                *diagnostics["classification_diagnostics"],
            ]
            diagnostics.update(
                {
                    "classification": {
                        "provider": classification.provider,
                        "classification_asof": classification.classification_asof,
                        "classification_hash": classification.classification_hash,
                        "fallback": classification.fallback,
                    },
                    "counting_units": counting_diagnostics,
                    "market_data": getattr(self.market_provider, "last_diagnostics", {}),
                    "input_coverage": {
                        "n_expected_securities": len(symbols),
                        "n_valid_security_returns": valid_security_returns,
                        "security_return_coverage": security_coverage,
                        "n_expected_counting_units": n_expected,
                        "n_valid_counting_units": n_valid,
                        "counting_unit_coverage": overall_coverage,
                    },
                }
            )
            metadata["diagnostic_counts"] = {
                "missing_members": len(diagnostics["missing_members"]),
                "low_confidence_groups": len(diagnostics["low_confidence_groups"]),
                "classification_diagnostics": len(diagnostics["classification_diagnostics"]),
            }

            input_paths = list(market.input_paths)
            if classification.source_path is not None:
                input_paths.append(classification.source_path)
            file_states = _file_states(input_paths)
            source_max_date = self._source_max_date(market.adj_close)
            fingerprint_payload = {
                "asof": asof,
                "previous_session": prior.date().isoformat(),
                "classification_hash": classification.classification_hash,
                "group_id_mapping_version": classification.group_id_mapping_version,
                "issuer_override_hash": counting_diagnostics.get("issuer_override_file_hash"),
                "price_pair_hash": _hash_frame(price_pair),
                "benchmark_pair_hash": _hash_frame(benchmark_pair),
                "counting_units": _records(
                    counting_units[
                        [
                            "security_id",
                            "counting_unit_id",
                            "ticker",
                            "group_id",
                            "raw_return_1d",
                            "selection_method",
                            "selection_data_through",
                            "member_weights_json",
                            "is_valid_for_headline",
                        ]
                    ]
                ),
            }
            input_fingerprint = canonical_hash(fingerprint_payload)
            parameter_hash = compute_parameter_hash(self.settings)
            if not request.force and not dry_run:
                matched = self.artifact_store.find_matching_latest(
                    combo,
                    parameter_hash=parameter_hash,
                    input_fingerprint=input_fingerprint,
                    asof=asof,
                )
                if matched is not None:
                    return self.artifact_store.record_skipped(
                        run_id,
                        combo,
                        matched_run_id=str(matched["run_id"]),
                        metadata={
                            **metadata,
                            "asof": asof,
                            "input_fingerprint": input_fingerprint,
                        },
                        dry_run=False,
                    )

            quality_summary = {
                "n_expected": n_expected,
                "n_valid": n_valid,
                "count_coverage": overall_coverage,
                "n_groups_expected": int(len(result.metrics)),
                "n_groups_ranked": int(result.metrics["eligible_for_ranking"].sum()),
                "n_groups_low_confidence": int(len(low_confidence)),
            }
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "algorithm_version": ALGORITHM_VERSION,
                **_git_provenance(),
                "generated_at": _iso_utc(self._now()),
                "asof": asof,
                "snapshot_id": "EOD",
                "snapshot_time": _iso_utc(close),
                "session_status": "FINAL",
                "freshness_status": "FRESH",
                "quality_status": quality_status,
                "source_max_date": source_max_date,
                "mode": combo.mode,
                "universe": combo.universe,
                "universe_version": classification.classification_asof,
                "taxonomy": combo.taxonomy,
                "taxonomy_level": combo.level,
                "taxonomy_version": classification.taxonomy_version,
                "classification_asof": classification.classification_asof,
                "classification_hash": classification.classification_hash,
                "classification_provider": classification.provider,
                "group_id_mapping_version": classification.group_id_mapping_version,
                "fallback": classification.fallback,
                "fetched_at": classification.fetched_at,
                "pit_universe_applied": False,
                "pit_classification_applied": False,
                "counting_unit": "security_with_overrides",
                "issuer_dedupe_status": counting_diagnostics["issuer_dedupe_status"],
                "issuer_overrides_applied": counting_diagnostics["issuer_overrides_applied"],
                "issuer_override_count": counting_diagnostics["issuer_override_count"],
                "issuer_override_version": counting_diagnostics["issuer_override_version"],
                "weight_source": "EQUAL_WEIGHT_ROBUST",
                "benchmark": self.settings.benchmark,
                "input_paths": [item["path"] for item in file_states],
                "input_mtimes": {item["path"]: item["mtime_ns"] for item in file_states},
                "input_max_date": source_max_date,
                "input_row_counts": metadata["input_row_counts"],
                "input_fingerprint": input_fingerprint,
                "quality_summary": quality_summary,
                "research_only": explicit_research,
            }
            run = {
                **metadata,
                "asof": asof,
                "parameter_hash": parameter_hash,
                "runtime_config_hash": compute_runtime_config_hash(self.settings),
                "input_fingerprint": input_fingerprint,
                "freshness_status": "FRESH",
                "quality_status": quality_status,
                "input_row_counts": metadata["input_row_counts"],
                "diagnostic_counts": metadata["diagnostic_counts"],
                "duration_ms": round((time.perf_counter() - started_clock) * 1000, 3),
            }
            bundle = GroupAnalyticsBundle(
                metrics=result.metrics,
                members=result.members,
                contributions=result.contributions,
                diagnostics=diagnostics,
                manifest=manifest,
                run=run,
            )
            return self.artifact_store.publish(
                run_id=run_id,
                combination=combo,
                bundle=bundle,
                dry_run=dry_run,
            )
        except Exception as exc:  # every started attempt becomes terminal
            metadata["duration_ms"] = round(
                (time.perf_counter() - started_clock) * 1000,
                3,
            )
            metadata["diagnostic_counts"] = {
                "missing_members": len(diagnostics.get("missing_members", [])),
                "low_confidence_groups": len(diagnostics.get("low_confidence_groups", [])),
                "classification_diagnostics": len(
                    diagnostics.get("classification_diagnostics", [])
                ),
            }
            return self.artifact_store.record_failure(
                run_id,
                combo,
                exc,
                metadata,
                dry_run=dry_run,
                diagnostics=diagnostics,
            )

    @staticmethod
    def _source_max_date(frame: pd.DataFrame) -> str | None:
        if frame.empty:
            return None
        valid_rows = frame.notna().any(axis=1)
        if not bool(valid_rows.any()):
            return None
        value = pd.Timestamp(frame.index[valid_rows][-1])
        if value.tzinfo is not None:
            value = value.tz_localize(None)
        return value.date().isoformat()

    @staticmethod
    def _enrich_frames(
        result: GroupAggregationResult,
        *,
        run_id: str,
        asof: str,
        explicit_research: bool,
    ) -> None:
        common = {"run_id": run_id, "date": asof, "snapshot_id": "EOD"}
        for frame in (result.metrics, result.members, result.contributions):
            for key, value in common.items():
                frame[key] = value

        result.metrics["rvol"] = None
        result.metrics["rvol_coverage"] = None
        result.metrics["rvol_status"] = "UNAVAILABLE"
        result.metrics["quote_age_seconds_max"] = None
        if explicit_research:
            result.metrics["reason_codes"] = result.metrics["reason_codes"].map(
                lambda values: _merge_reason_codes(
                    values, ReasonCode.STATIC_MAPPING_RESEARCH_ONLY
                )
            )

        defaults: Mapping[str, Any] = {
            "counting_unit_id": None,
            "issuer_id": None,
            "membership_valid_from": None,
            "membership_valid_to": None,
            "t_1_weight": None,
            "theme_exposure": None,
            "data_asof": asof,
            "quote_timestamp": None,
        }
        if "valid_from" in result.members:
            result.members["membership_valid_from"] = result.members["valid_from"]
        if "valid_to" in result.members:
            result.members["membership_valid_to"] = result.members["valid_to"]
        for key, value in defaults.items():
            if key not in result.members:
                result.members[key] = value


def run_group_analytics(
    request: RunRequest | None = None,
    *,
    settings: GroupAnalyticsSettings | None = None,
) -> RunOutcome:
    """Convenience entry point for callers that do not need dependency injection."""
    return GroupAnalyticsService(settings).run(request)


__all__ = [
    "GroupAnalyticsService",
    "PointInTimeDataUnavailableError",
    "run_group_analytics",
]
