"""Stable contracts shared by group-analytics domain components."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import pandas as pd


class ReasonCode(StrEnum):
    SMALL_GROUP = "SMALL_GROUP"
    NO_EXPECTED_MEMBERS = "NO_EXPECTED_MEMBERS"
    NO_VALID_MEMBERS = "NO_VALID_MEMBERS"
    LOW_COUNT_COVERAGE = "LOW_COUNT_COVERAGE"
    LOW_WEIGHT_COVERAGE = "LOW_WEIGHT_COVERAGE"
    LOW_FRESHNESS = "LOW_FRESHNESS"
    STALE_QUOTE = "STALE_QUOTE"
    FUTURE_QUOTE_TIMESTAMP = "FUTURE_QUOTE_TIMESTAMP"
    SNAPSHOT_SPAN_EXCEEDED = "SNAPSHOT_SPAN_EXCEEDED"
    QUOTE_RETURN_MISMATCH = "QUOTE_RETURN_MISMATCH"
    QUOTE_APPROXIMATION = "QUOTE_APPROXIMATION"
    MISSING_PRICE = "MISSING_PRICE"
    MISSING_PREVIOUS_CLOSE = "MISSING_PREVIOUS_CLOSE"
    MISSING_RETURN = "MISSING_RETURN"
    MISSING_DELISTING_RETURN = "MISSING_DELISTING_RETURN"
    MISSING_CLASSIFICATION = "MISSING_CLASSIFICATION"
    MISSING_MARKET_CAP = "MISSING_MARKET_CAP"
    MARKET_CAP_PROXY_ONLY = "MARKET_CAP_PROXY_ONLY"
    CORPORATE_ACTION_UNVERIFIED = "CORPORATE_ACTION_UNVERIFIED"
    ETF_EXCLUDED = "ETF_EXCLUDED"
    SHARE_CLASS_DEDUPED = "SHARE_CLASS_DEDUPED"
    ISSUER_DEDUPE_UNAVAILABLE = "ISSUER_DEDUPE_UNAVAILABLE"
    ISSUER_CLASSIFICATION_CONFLICT = "ISSUER_CLASSIFICATION_CONFLICT"
    PIT_UNIVERSE_UNAVAILABLE = "PIT_UNIVERSE_UNAVAILABLE"
    PIT_CLASSIFICATION_UNAVAILABLE = "PIT_CLASSIFICATION_UNAVAILABLE"
    STATIC_MAPPING_RESEARCH_ONLY = "STATIC_MAPPING_RESEARCH_ONLY"
    SINGLE_NAME_CONCENTRATION = "SINGLE_NAME_CONCENTRATION"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    INSUFFICIENT_GROUPS_FOR_ZSCORE = "INSUFFICIENT_GROUPS_FOR_ZSCORE"
    BENCHMARK_UNAVAILABLE = "BENCHMARK_UNAVAILABLE"
    NO_THEME_EXPOSURE = "NO_THEME_EXPOSURE"
    UNKNOWN_LEGACY_CACHE = "UNKNOWN_LEGACY_CACHE"
    FAILED_LAST_ATTEMPT = "FAILED_LAST_ATTEMPT"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class FreshnessStatus(StrEnum):
    FRESH = "FRESH"
    DELAYED = "DELAYED"
    STALE = "STALE"


class QualityStatus(StrEnum):
    OK = "OK"
    LOW_COVERAGE = "LOW_COVERAGE"
    NO_DATA = "NO_DATA"


class DedupeStatus(StrEnum):
    NONE = "NONE"
    PARTIAL_OVERRIDES = "PARTIAL_OVERRIDES"
    FULL = "FULL"


@dataclass(frozen=True, slots=True)
class ArtifactCombination:
    universe: str
    taxonomy: str
    level: str
    mode: str = "eod"

    def normalized(self) -> "ArtifactCombination":
        return ArtifactCombination(
            universe=self.universe.upper(),
            taxonomy=self.taxonomy.upper(),
            level=self.level.lower(),
            mode=self.mode.lower(),
        )

    def as_dict(self) -> dict[str, str]:
        normalized = self.normalized()
        return {
            "universe": normalized.universe,
            "taxonomy": normalized.taxonomy,
            "level": normalized.level,
            "mode": normalized.mode,
        }


@dataclass(slots=True)
class ClassificationSnapshot:
    frame: pd.DataFrame
    provider: str
    taxonomy_version: str
    classification_hash: str
    classification_asof: str
    fetched_at: str
    group_id_mapping_version: str | None = None
    fallback: bool = False
    payload_hash: str | None = None
    source_path: Path | None = None
    diagnostics: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class EODMarketSnapshot:
    adj_close: pd.DataFrame
    volume: pd.DataFrame
    benchmark_adj_close: pd.DataFrame
    market_cap: pd.DataFrame | None = None
    input_paths: list[Path] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RunRequest:
    universe: str = "SP500"
    taxonomy: str = "FMP"
    level: str = "sector"
    mode: str = "eod"
    asof: str = "latest"
    dry_run: bool = False
    strict_pit: bool = False
    force: bool = False
    limit: int | None = None
    output_run_id: str | None = None

    @property
    def combination(self) -> ArtifactCombination:
        return ArtifactCombination(
            self.universe, self.taxonomy, self.level, self.mode
        ).normalized()


@dataclass(slots=True)
class GroupAnalyticsBundle:
    metrics: pd.DataFrame
    members: pd.DataFrame
    contributions: pd.DataFrame
    diagnostics: dict[str, Any]
    manifest: dict[str, Any]
    run: dict[str, Any]


@dataclass(slots=True)
class RunOutcome:
    run_id: str
    status: RunStatus
    dry_run: bool
    published: bool
    combination: ArtifactCombination
    asof: str | None = None
    artifact_locator: str | None = None
    error: dict[str, Any] | None = None


class ClassificationProvider(Protocol):
    def snapshot(
        self,
        *,
        universe: str,
        taxonomy: str,
        level: str,
        asof: str,
        force: bool = False,
    ) -> ClassificationSnapshot: ...


class EODMarketDataProvider(Protocol):
    def snapshot(
        self,
        *,
        symbols: list[str],
        benchmark: str,
        force: bool = False,
    ) -> EODMarketSnapshot: ...


class GroupArtifactStore(Protocol):
    def publish(
        self,
        *,
        run_id: str,
        combination: ArtifactCombination,
        bundle: GroupAnalyticsBundle,
        dry_run: bool = False,
    ) -> RunOutcome: ...


class GroupAnalyticsError(RuntimeError):
    """Base error with a stable machine-readable code."""

    code = "GROUP_ANALYTICS_ERROR"
    stage = "unknown"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


class FeatureDisabledError(GroupAnalyticsError):
    code = "FEATURE_DISABLED"
    stage = "validate_request"


class InvalidRequestError(GroupAnalyticsError):
    code = "INVALID_REQUEST"
    stage = "validate_request"


class UnsupportedCombinationError(GroupAnalyticsError):
    code = "UNSUPPORTED_COMBINATION"
    stage = "validate_request"


class InputCoverageError(GroupAnalyticsError):
    code = "INPUT_COVERAGE_BELOW_GATE"
    stage = "validate_inputs"


class ArtifactNotFoundError(GroupAnalyticsError):
    code = "ARTIFACT_NOT_FOUND"
    stage = "read_artifacts"


class NoSuccessfulRunError(GroupAnalyticsError):
    code = "NO_SUCCESSFUL_RUN"
    stage = "read_artifacts"


def sorted_reason_codes(values: list[str | ReasonCode] | set[str | ReasonCode]) -> list[str]:
    """Reason codes are set-like in the domain and sorted at serialization."""
    return sorted({str(value) for value in values})


__all__ = [
    "ArtifactCombination",
    "ArtifactNotFoundError",
    "ClassificationProvider",
    "ClassificationSnapshot",
    "DedupeStatus",
    "EODMarketDataProvider",
    "EODMarketSnapshot",
    "FeatureDisabledError",
    "FreshnessStatus",
    "GroupAnalyticsBundle",
    "GroupAnalyticsError",
    "GroupArtifactStore",
    "InputCoverageError",
    "InvalidRequestError",
    "NoSuccessfulRunError",
    "QualityStatus",
    "ReasonCode",
    "RunOutcome",
    "RunRequest",
    "RunStatus",
    "UnsupportedCombinationError",
    "sorted_reason_codes",
]
