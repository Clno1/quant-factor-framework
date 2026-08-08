"""Stable data contracts for market-regime research."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd


class MarketRegimeResearchError(RuntimeError):
    """Base error for failures that must be visible to research callers."""


class DataContractError(MarketRegimeResearchError):
    """Raised when an input is incomplete, inconsistent, or ambiguous."""


class PointInTimeReconstructionError(DataContractError):
    """Raised when change events cannot produce trustworthy PIT snapshots."""


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    """Human-readable provenance for one feature column."""

    feature_name: str
    group: str
    instrument: str
    formula: str
    lookback_sessions: int
    description: str
    availability: str = "known_after_same_session_close"

    def as_dict(self) -> dict[str, Any]:
        return {
            "feature_name": self.feature_name,
            "group": self.group,
            "instrument": self.instrument,
            "formula": self.formula,
            "lookback_sessions": self.lookback_sessions,
            "description": self.description,
            "availability": self.availability,
        }


@dataclass(slots=True)
class FeatureBundle:
    """Feature matrix plus the registry needed to audit every column."""

    values: pd.DataFrame
    registry: list[FeatureDefinition] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PITReconstructionResult:
    """Complete snapshots and diagnostics reconstructed from change events."""

    membership: pd.DataFrame
    normalized_events: pd.DataFrame
    diagnostics: dict[str, Any]


@dataclass(slots=True)
class ResearchRunResult:
    """Files emitted by one immutable local research run."""

    run_id: str
    run_dir: Path
    features_path: Path
    labels_path: Path
    feature_registry_path: Path
    manifest_path: Path
    diagnostics_path: Path


@dataclass(slots=True)
class ScreeningRunResult:
    """Files emitted by one immutable Stage B effectiveness screen."""

    screening_id: str
    screening_dir: Path
    candidate_registry_path: Path
    event_studies_path: Path
    fold_results_path: Path
    predictions_path: Path
    scorecard_path: Path
    manifest_path: Path
    summary_path: Path
    report_path: Path


__all__ = [
    "DataContractError",
    "FeatureBundle",
    "FeatureDefinition",
    "MarketRegimeResearchError",
    "PITReconstructionResult",
    "PointInTimeReconstructionError",
    "ResearchRunResult",
    "ScreeningRunResult",
]
