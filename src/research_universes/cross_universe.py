"""Deterministic cross-universe factor evidence and verdict rules."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import math
from typing import Any, Mapping, Sequence

from src.research_universes.models import ResearchUniverse, ResearchUniverseRole


class CrossUniverseVerdict(StrEnum):
    ROBUST = "ROBUST"
    PRIMARY_ONLY = "PRIMARY_ONLY"
    SEGMENT_SPECIFIC = "SEGMENT_SPECIFIC"
    CONFLICT = "CONFLICT"
    INSUFFICIENT = "INSUFFICIENT"
    REJECT = "REJECT"


class EvidenceStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"
    STALE = "STALE"
    INVALID = "INVALID"


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class UniverseFactorEvidence:
    universe_id: str
    role: ResearchUniverseRole
    status: EvidenceStatus
    target_session: str | None = None
    verdict: str | None = None
    dataset_version_id: str | None = None
    research_publication_id: str | None = None
    factor_generation_id: str | None = None
    confidence_sha256: str | None = None
    direction_sign: int | None = None
    n_obs: int | None = None
    ic_mean: float | None = None
    ic_ir: float | None = None
    t_stat: float | None = None
    p_value: float | None = None
    q_value: float | None = None
    ci95_low: float | None = None
    ci95_high: float | None = None
    long_short_ann_return: float | None = None
    long_short_sharpe: float | None = None
    cost_bps_per_year_avg: float | None = None
    reason: str | None = None

    @classmethod
    def from_confidence_report(
        cls,
        *,
        universe: ResearchUniverse,
        target_session: str,
        dataset_version_id: str,
        research_publication_id: str,
        factor_generation_id: str,
        confidence_sha256: str,
        report: Mapping[str, Any],
    ) -> "UniverseFactorEvidence":
        summary = report.get("summary")
        if not isinstance(summary, Mapping):
            return cls(
                universe_id=universe.universe_id,
                role=universe.role,
                status=EvidenceStatus.INVALID,
                target_session=target_session,
                reason="CONFIDENCE_SUMMARY_MISSING",
            )
        verdict = str(report.get("verdict") or "").upper()
        if verdict not in {"PASS", "WATCH", "FAIL"}:
            return cls(
                universe_id=universe.universe_id,
                role=universe.role,
                status=EvidenceStatus.INVALID,
                target_session=target_session,
                reason="CONFIDENCE_VERDICT_INVALID",
            )
        direction = summary.get("direction_sign")
        return cls(
            universe_id=universe.universe_id,
            role=universe.role,
            status=EvidenceStatus.AVAILABLE,
            target_session=target_session,
            verdict=verdict,
            dataset_version_id=dataset_version_id,
            research_publication_id=research_publication_id,
            factor_generation_id=factor_generation_id,
            confidence_sha256=confidence_sha256,
            direction_sign=int(direction) if direction in {-1, 1} else None,
            n_obs=int(summary["n_obs"]) if summary.get("n_obs") is not None else None,
            ic_mean=_finite(summary.get("ic_mean")),
            ic_ir=_finite(summary.get("ic_ir")),
            t_stat=_finite(summary.get("t_stat")),
            p_value=_finite(summary.get("p_value")),
            q_value=_finite(summary.get("q_value")),
            ci95_low=_finite(summary.get("ci95_low")),
            ci95_high=_finite(summary.get("ci95_high")),
            long_short_ann_return=_finite(summary.get("long_short_ann_return")),
            long_short_sharpe=_finite(summary.get("long_short_sharpe")),
            cost_bps_per_year_avg=_finite(summary.get("cost_bps_per_year_avg")),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["role"] = self.role.value
        payload["status"] = self.status.value
        return payload


@dataclass(frozen=True)
class CrossUniverseFactorAssessment:
    factor_id: str
    target_session: str
    universes: dict[str, UniverseFactorEvidence]
    direction_consistent: bool | None
    verdict: CrossUniverseVerdict
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_id": self.factor_id,
            "target_session": self.target_session,
            "universes": {
                key: value.to_dict() for key, value in self.universes.items()
            },
            "direction_consistent": self.direction_consistent,
            "verdict": self.verdict.value,
            "summary": self.summary,
        }


def _significant_opposite(evidence: UniverseFactorEvidence) -> bool:
    if evidence.ic_mean is None or evidence.ic_mean >= 0:
        return False
    if evidence.ci95_high is not None and evidence.ci95_high < 0:
        return True
    if evidence.q_value is not None and evidence.q_value <= 0.20:
        return True
    return evidence.p_value is not None and evidence.p_value <= 0.10


def assess_factor_across_universes(
    *,
    factor_id: str,
    target_session: str,
    research_universes: Sequence[ResearchUniverse],
    evidence: Mapping[str, UniverseFactorEvidence],
) -> CrossUniverseFactorAssessment:
    required = [item for item in research_universes if item.cross_universe_enabled]
    primary = [item for item in required if item.role == ResearchUniverseRole.PRIMARY]
    secondary = [item for item in required if item.role == ResearchUniverseRole.SECONDARY]
    if len(primary) != 1 or not secondary:
        raise ValueError("Cross-universe assessment requires one PRIMARY and a SECONDARY")

    resolved: dict[str, UniverseFactorEvidence] = {}
    for universe in required:
        resolved[universe.universe_id] = evidence.get(
            universe.universe_id,
            UniverseFactorEvidence(
                universe_id=universe.universe_id,
                role=universe.role,
                status=EvidenceStatus.MISSING,
                reason="EVIDENCE_NOT_PROVIDED",
            ),
        )
    unavailable = [
        item
        for item in resolved.values()
        if item.status != EvidenceStatus.AVAILABLE
        or item.target_session != target_session
    ]
    if unavailable:
        labels = ", ".join(
            f"{item.universe_id}:{item.status.value}"
            for item in unavailable
        )
        return CrossUniverseFactorAssessment(
            factor_id=factor_id,
            target_session=target_session,
            universes=resolved,
            direction_consistent=None,
            verdict=CrossUniverseVerdict.INSUFFICIENT,
            summary=f"必要研究证据不完整或日期不可比：{labels}",
        )

    available = list(resolved.values())
    if any(item.ic_mean is None for item in available):
        return CrossUniverseFactorAssessment(
            factor_id=factor_id,
            target_session=target_session,
            universes=resolved,
            direction_consistent=None,
            verdict=CrossUniverseVerdict.INSUFFICIENT,
            summary="至少一个研究池缺少可比较的方向调整后 IC",
        )
    directions = {1 if item.ic_mean > 0 else -1 if item.ic_mean < 0 else 0 for item in available}
    direction_consistent = len(directions - {0}) <= 1 and 0 not in directions
    if any(_significant_opposite(item) for item in available):
        return CrossUniverseFactorAssessment(
            factor_id=factor_id,
            target_session=target_session,
            universes=resolved,
            direction_consistent=False,
            verdict=CrossUniverseVerdict.CONFLICT,
            summary="研究池之间存在统计上不可忽略的反向因子证据",
        )

    primary_evidence = resolved[primary[0].universe_id]
    secondary_evidence = [resolved[item.universe_id] for item in secondary]
    all_verdicts = [item.verdict for item in available]
    if not direction_consistent:
        if primary_evidence.verdict == "PASS" and any(
            item.verdict == "FAIL" for item in secondary_evidence
        ):
            verdict = CrossUniverseVerdict.CONFLICT
            summary = "主要池通过，但次级池失败且方向相反"
        else:
            verdict = CrossUniverseVerdict.INSUFFICIENT
            summary = "方向不一致但尚不足以形成稳健跨池结论"
    elif primary_evidence.verdict == "PASS" and all(
        item.verdict == "PASS" for item in secondary_evidence
    ):
        verdict = CrossUniverseVerdict.ROBUST
        summary = "PRIMARY 与 SECONDARY 均通过，且预测方向一致"
    elif primary_evidence.verdict == "PASS":
        verdict = CrossUniverseVerdict.PRIMARY_ONLY
        summary = "主要研究池通过，次级研究池同方向但证据较弱"
    elif any(item.verdict == "PASS" for item in secondary_evidence):
        verdict = CrossUniverseVerdict.SEGMENT_SPECIFIC
        summary = "次级研究池通过，但主要研究池未通过"
    elif all(value == "FAIL" for value in all_verdicts):
        verdict = CrossUniverseVerdict.REJECT
        summary = "PRIMARY 与 SECONDARY 均未通过"
    else:
        verdict = CrossUniverseVerdict.INSUFFICIENT
        summary = "现有证据未达到通过或明确拒绝门槛"
    return CrossUniverseFactorAssessment(
        factor_id=factor_id,
        target_session=target_session,
        universes=resolved,
        direction_consistent=direction_consistent,
        verdict=verdict,
        summary=summary,
    )


__all__ = [
    "CrossUniverseFactorAssessment",
    "CrossUniverseVerdict",
    "EvidenceStatus",
    "UniverseFactorEvidence",
    "assess_factor_across_universes",
]
