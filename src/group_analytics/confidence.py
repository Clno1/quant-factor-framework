"""Coverage, snapshot quality, and Stage-1 ranking eligibility."""
from __future__ import annotations

from dataclasses import dataclass
import math

from .models import QualityStatus, ReasonCode, sorted_reason_codes


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    n_expected: int
    n_valid: int
    count_coverage: float | None
    weight_coverage: float | None
    fresh_quote_coverage: float | None
    headline_n_effective: float
    snapshot_quality_score: float | None
    snapshot_quality_grade: str | None
    quality_status: QualityStatus

    def as_dict(self) -> dict[str, object]:
        return {
            "n_expected": self.n_expected,
            "n_valid": self.n_valid,
            "count_coverage": self.count_coverage,
            "headline_n_effective": self.headline_n_effective,
            "snapshot_quality_score": self.snapshot_quality_score,
            "snapshot_quality_grade": self.snapshot_quality_grade,
            "quality_status": self.quality_status.value,
        }


@dataclass(frozen=True, slots=True)
class RankingAssessment:
    eligible_for_ranking: bool
    reason_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "eligible_for_ranking": self.eligible_for_ranking,
            "reason_codes": list(self.reason_codes),
        }


def _coverage(value: float | None, *, name: str) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise ValueError(f"{name} must be finite and in [0, 1]")
    return parsed


def compute_count_coverage(n_expected: int, n_valid: int) -> float | None:
    """Return valid/expected, or ``None`` for an empty expected set."""

    if n_expected < 0 or n_valid < 0 or n_valid > n_expected:
        raise ValueError("expected and valid counts must satisfy 0 <= valid <= expected")
    return None if n_expected == 0 else n_valid / n_expected


def quality_grade(score: float, n_valid: int) -> str:
    """Apply the frozen A/B/C/D Stage-1 quality-grade boundaries."""

    if not math.isfinite(score) or not 0.0 <= score <= 100.0:
        raise ValueError("quality score must be finite and in [0, 100]")
    if n_valid >= 10 and score >= 90.0:
        return "A"
    if n_valid >= 5 and score >= 75.0:
        return "B"
    if n_valid >= 3 and score >= 60.0:
        return "C"
    return "D"


def compute_snapshot_quality(
    n_expected: int,
    n_valid: int,
    *,
    weight_coverage: float | None = None,
    fresh_quote_coverage: float = 1.0,
    min_count_coverage: float = 0.80,
) -> QualityAssessment:
    """Compute the Stage-1 EOD headline quality score.

    For RobustEW the base weights are equal, so omitted ``weight_coverage``
    defaults to count coverage.  EOD callers normally leave
    ``fresh_quote_coverage`` at one: artifact age is a separate run-level
    freshness status.
    """

    count_coverage = compute_count_coverage(n_expected, n_valid)
    minimum = _coverage(min_count_coverage, name="min_count_coverage")
    fresh = _coverage(fresh_quote_coverage, name="fresh_quote_coverage")
    if count_coverage is None or n_valid == 0:
        return QualityAssessment(
            n_expected=n_expected,
            n_valid=n_valid,
            count_coverage=count_coverage,
            weight_coverage=None,
            fresh_quote_coverage=fresh,
            headline_n_effective=float(n_valid),
            snapshot_quality_score=None,
            snapshot_quality_grade=None,
            quality_status=QualityStatus.NO_DATA,
        )

    weight = count_coverage if weight_coverage is None else _coverage(
        weight_coverage, name="weight_coverage"
    )
    assert weight is not None and fresh is not None and minimum is not None
    n_score = min(1.0, n_valid / 10.0)
    score = 100.0 * (
        0.35 * count_coverage
        + 0.25 * weight
        + 0.20 * fresh
        + 0.20 * n_score
    )
    status = (
        QualityStatus.OK
        if count_coverage >= minimum
        else QualityStatus.LOW_COVERAGE
    )
    return QualityAssessment(
        n_expected=n_expected,
        n_valid=n_valid,
        count_coverage=count_coverage,
        weight_coverage=weight,
        fresh_quote_coverage=fresh,
        headline_n_effective=float(n_valid),
        snapshot_quality_score=score,
        snapshot_quality_grade=quality_grade(score, n_valid),
        quality_status=status,
    )


def evaluate_ranking(
    quality: QualityAssessment,
    *,
    min_members: int = 5,
    min_count_coverage: float = 0.80,
    min_freshness_coverage: float = 0.80,
    allowed_quality_grades: tuple[str, ...] = ("A", "B"),
    extra_reason_codes: list[str | ReasonCode] | set[str | ReasonCode] | None = None,
) -> RankingAssessment:
    """Evaluate the frozen Stage-1 ranking gate with deterministic reasons."""

    if min_members < 1:
        raise ValueError("min_members must be positive")
    count_min = _coverage(min_count_coverage, name="min_count_coverage")
    freshness_min = _coverage(
        min_freshness_coverage, name="min_freshness_coverage"
    )
    assert count_min is not None and freshness_min is not None
    reasons: list[str | ReasonCode] = list(extra_reason_codes or [])
    if quality.n_expected == 0:
        reasons.append(ReasonCode.NO_EXPECTED_MEMBERS)
    if quality.n_valid == 0:
        reasons.append(ReasonCode.NO_VALID_MEMBERS)
    if 0 < quality.n_valid < min_members:
        reasons.append(ReasonCode.SMALL_GROUP)
    if (
        quality.count_coverage is not None
        and quality.count_coverage < count_min
    ):
        reasons.append(ReasonCode.LOW_COUNT_COVERAGE)
    if (
        quality.fresh_quote_coverage is not None
        and quality.fresh_quote_coverage < freshness_min
    ):
        reasons.append(ReasonCode.LOW_FRESHNESS)

    allowed = {str(grade).upper() for grade in allowed_quality_grades}
    codes = tuple(sorted_reason_codes(reasons))
    blocking_codes = {
        ReasonCode.NO_EXPECTED_MEMBERS.value,
        ReasonCode.NO_VALID_MEMBERS.value,
        ReasonCode.SMALL_GROUP.value,
        ReasonCode.LOW_COUNT_COVERAGE.value,
        ReasonCode.LOW_FRESHNESS.value,
    }
    eligible = (
        not blocking_codes.intersection(codes)
        and quality.snapshot_quality_grade is not None
        and quality.snapshot_quality_grade in allowed
    )
    return RankingAssessment(eligible_for_ranking=eligible, reason_codes=codes)


__all__ = [
    "QualityAssessment",
    "RankingAssessment",
    "compute_count_coverage",
    "compute_snapshot_quality",
    "evaluate_ranking",
    "quality_grade",
]
