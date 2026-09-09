"""Fail-closed readiness checks for formal broad-universe research.

Broad factor observations may be published with a clearly labelled latest-known
industry backfill.  IC, ICIR, confidence and portfolio research require stricter
point-in-time classifications.  This module keeps those two claims separate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

import pandas as pd

from src.data.security_master import UNKNOWN_CLASSIFICATION


@dataclass(frozen=True)
class BroadResearchReadinessCheck:
    code: str
    passed: bool
    observed: Any
    required: Any
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BroadResearchReadiness:
    status: str
    target_session: str | None
    factor_data_publication_id: str | None
    checks: tuple[BroadResearchReadinessCheck, ...]

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(check.code for check in self.checks if not check.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": self.status,
            "target_session": self.target_session,
            "factor_data_publication_id": self.factor_data_publication_id,
            "blockers": list(self.blockers),
            "checks": [check.to_dict() for check in self.checks],
        }


def _text_set(values: Iterable[Any]) -> set[str]:
    return {
        str(value).strip().upper()
        for value in values
        if str(value).strip()
    }


def _classification_snapshot_coverage(
    membership: pd.DataFrame,
    classifications: pd.DataFrame,
    *,
    accepted_policies: set[str],
) -> dict[str, Any]:
    required_membership = {"date", "security_id", "active"}
    required_classification = {
        "security_id",
        "sector",
        "effective_from",
        "effective_to",
        "knowledge_date",
        "classification_policy",
    }
    missing_membership = sorted(required_membership - set(membership.columns))
    missing_classification = sorted(
        required_classification - set(classifications.columns)
    )
    if missing_membership or missing_classification:
        return {
            "minimum_coverage": 0.0,
            "snapshot_count": 0,
            "snapshots": [],
            "missing_membership_columns": missing_membership,
            "missing_classification_columns": missing_classification,
        }

    members = membership.loc[:, list(required_membership)].copy()
    members["date"] = pd.to_datetime(
        members["date"], errors="coerce"
    ).dt.normalize()
    members["security_id"] = members["security_id"].astype(str)
    members = members.loc[
        members["date"].notna()
        & members["active"].fillna(False).astype(bool)
    ]

    history = classifications.copy()
    history["security_id"] = history["security_id"].astype(str)
    history["sector"] = history["sector"].fillna("").astype(str).str.strip()
    history["classification_policy"] = (
        history["classification_policy"].fillna("").astype(str).str.upper()
    )
    for column in ("effective_from", "effective_to", "knowledge_date"):
        history[column] = pd.to_datetime(
            history[column], errors="coerce"
        ).dt.normalize()

    snapshots: list[dict[str, Any]] = []
    for snapshot_date, rows in members.groupby("date", sort=True):
        member_ids = set(rows["security_id"])
        eligible = history.loc[
            history["security_id"].isin(member_ids)
            & history["classification_policy"].isin(accepted_policies)
            & history["effective_from"].notna()
            & history["effective_from"].le(snapshot_date)
            & (
                history["effective_to"].isna()
                | history["effective_to"].ge(snapshot_date)
            )
            & history["knowledge_date"].notna()
            & history["knowledge_date"].le(snapshot_date)
            & history["sector"].ne("")
            & history["sector"].ne(UNKNOWN_CLASSIFICATION)
        ]
        known = int(eligible["security_id"].nunique())
        total = int(len(member_ids))
        snapshots.append({
            "date": pd.Timestamp(snapshot_date).date().isoformat(),
            "members": total,
            "known_pit_industry": known,
            "coverage": known / total if total else 0.0,
        })
    minimum = min(
        (float(item["coverage"]) for item in snapshots), default=0.0
    )
    return {
        "minimum_coverage": minimum,
        "snapshot_count": len(snapshots),
        "snapshots": snapshots,
        "missing_membership_columns": [],
        "missing_classification_columns": [],
    }


def _evaluable_sessions(
    audit: pd.DataFrame,
    *,
    factor_ids: list[str],
    minimum_cross_section: int,
) -> dict[str, int]:
    required = {"factor_id", "date", "clean_non_null"}
    if audit.empty or not required.issubset(audit.columns):
        return {factor_id: 0 for factor_id in factor_ids}
    work = audit.loc[:, list(required)].copy()
    work["factor_id"] = work["factor_id"].astype(str).str.upper()
    work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.normalize()
    work["clean_non_null"] = pd.to_numeric(
        work["clean_non_null"], errors="coerce"
    ).fillna(0)
    work = (
        work.dropna(subset=["date"])
        .groupby(["factor_id", "date"], as_index=False)["clean_non_null"]
        .max()
    )
    return {
        factor_id: int(
            work.loc[
                work["factor_id"].eq(factor_id)
                & work["clean_non_null"].ge(int(minimum_cross_section)),
                "date",
            ].nunique()
        )
        for factor_id in factor_ids
    }


def assess_broad_research_readiness(
    *,
    publication: dict[str, Any],
    membership: pd.DataFrame,
    classifications: pd.DataFrame,
    preprocessing_audit: pd.DataFrame,
    expected_factor_ids: Iterable[str],
    minimum_evaluable_sessions: int,
    minimum_cross_section: int,
    minimum_pit_industry_coverage: float,
    accepted_classification_policies: Iterable[str],
) -> BroadResearchReadiness:
    """Return a complete, machine-readable gate without publishing research."""
    factors = sorted(_text_set(expected_factor_ids))
    accepted = _text_set(accepted_classification_policies)
    published_factors = _text_set((publication.get("factors") or {}).keys())
    target_session = str(publication.get("target_session") or "") or None
    checks: list[BroadResearchReadinessCheck] = []

    checks.append(BroadResearchReadinessCheck(
        code="FACTOR_DATA_PUBLICATION_MODE",
        passed=publication.get("publication_mode") == "FACTOR_DATA",
        observed=publication.get("publication_mode"),
        required="FACTOR_DATA",
        message="正式研究只能消费已认证的宽基因子数据 publication。",
    ))
    missing_factors = sorted(set(factors) - published_factors)
    checks.append(BroadResearchReadinessCheck(
        code="COMPLETE_FACTOR_SET",
        passed=not missing_factors,
        observed=sorted(published_factors),
        required=factors,
        message=(
            "八个配置因子均已发布。"
            if not missing_factors
            else f"缺少因子：{', '.join(missing_factors)}"
        ),
    ))
    factor_target_mismatches = sorted(
        factor_id
        for factor_id, binding in (publication.get("factors") or {}).items()
        if str((binding or {}).get("date_end") or "") != str(target_session or "")
    )
    checks.append(BroadResearchReadinessCheck(
        code="FACTOR_TARGET_ALIGNMENT",
        passed=bool(target_session) and not factor_target_mismatches,
        observed=factor_target_mismatches,
        required=[],
        message=(
            "所有因子都截止到同一 target session。"
            if target_session and not factor_target_mismatches
            else "因子截止日与 factor-data publication 不一致。"
        ),
    ))

    publication_policy = str(
        publication.get("classification_policy") or ""
    ).upper()
    checks.append(BroadResearchReadinessCheck(
        code="PIT_CLASSIFICATION_POLICY",
        passed=bool(accepted) and publication_policy in accepted,
        observed=publication_policy or None,
        required=sorted(accepted),
        message=(
            "行业分类口径允许用于正式 PIT 研究。"
            if publication_policy in accepted
            else "当前行业是最新快照回填，禁止发布正式宽基 IC/置信结论。"
        ),
    ))

    coverage = _classification_snapshot_coverage(
        membership,
        classifications,
        accepted_policies=accepted,
    )
    failing_snapshots = [
        item
        for item in coverage["snapshots"]
        if float(item["coverage"]) < float(minimum_pit_industry_coverage)
    ]
    checks.append(BroadResearchReadinessCheck(
        code="PIT_INDUSTRY_COVERAGE",
        passed=(
            coverage["snapshot_count"] > 0
            and float(coverage["minimum_coverage"])
            >= float(minimum_pit_industry_coverage)
        ),
        observed={
            "minimum_coverage": coverage["minimum_coverage"],
            "snapshot_count": coverage["snapshot_count"],
            "failing_snapshots": failing_snapshots[:20],
            "missing_membership_columns": coverage[
                "missing_membership_columns"
            ],
            "missing_classification_columns": coverage[
                "missing_classification_columns"
            ],
        },
        required=float(minimum_pit_industry_coverage),
        message=(
            "每个 PIT membership 快照的可用行业覆盖均达到门槛。"
            if coverage["snapshot_count"] > 0 and not failing_snapshots
            else "至少一个 PIT membership 快照缺少足够的当时可知行业。"
        ),
    ))

    session_counts = _evaluable_sessions(
        preprocessing_audit,
        factor_ids=factors,
        minimum_cross_section=minimum_cross_section,
    )
    checks.append(BroadResearchReadinessCheck(
        code="EVALUABLE_HISTORY",
        passed=bool(session_counts) and all(
            count >= int(minimum_evaluable_sessions)
            for count in session_counts.values()
        ),
        observed=session_counts,
        required={
            "minimum_sessions_per_factor": int(minimum_evaluable_sessions),
            "minimum_cross_section": int(minimum_cross_section),
        },
        message=(
            "每个因子都有足够的宽基有效截面历史。"
            if session_counts and all(
                count >= int(minimum_evaluable_sessions)
                for count in session_counts.values()
            )
            else "至少一个因子的可评价宽基历史不足。"
        ),
    ))

    status = "READY" if all(check.passed for check in checks) else "BLOCKED"
    return BroadResearchReadiness(
        status=status,
        target_session=target_session,
        factor_data_publication_id=(
            str(publication["publication_id"])
            if publication.get("publication_id") else None
        ),
        checks=tuple(checks),
    )


__all__ = [
    "BroadResearchReadiness",
    "BroadResearchReadinessCheck",
    "assess_broad_research_readiness",
]
