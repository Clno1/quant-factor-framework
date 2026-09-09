from __future__ import annotations

from pathlib import Path

import pytest

from src.research_universes.cross_universe import (
    CrossUniverseVerdict,
    EvidenceStatus,
    UniverseFactorEvidence,
    assess_factor_across_universes,
)
import src.research_universes.publication as publication
from src.research_universes.models import (
    FactorPublicationMode,
    MembershipType,
    ResearchUniverse,
    ResearchUniverseRole,
    UniversePurpose,
)


def _universe(universe_id: str, role: ResearchUniverseRole) -> ResearchUniverse:
    return ResearchUniverse(
        universe_id=universe_id,
        display_name=universe_id,
        purpose=UniversePurpose.VALIDATION,
        role=role,
        membership_type=MembershipType.PIT,
        factor_publication_mode=FactorPublicationMode.FULL_RESEARCH,
        benchmark="SPY" if role == ResearchUniverseRole.PRIMARY else "QQQ",
        confidence_enabled=True,
        cross_universe_enabled=True,
        minimum_cross_section=60,
        minimum_industry_coverage=0.95,
    )


SP500 = _universe("SP500", ResearchUniverseRole.PRIMARY)
NASDAQ100 = _universe("NASDAQ100", ResearchUniverseRole.SECONDARY)
UNIVERSES = [SP500, NASDAQ100]


def _evidence(
    universe: ResearchUniverse,
    *,
    verdict: str = "PASS",
    ic_mean: float = 0.03,
    q_value: float = 0.05,
) -> UniverseFactorEvidence:
    return UniverseFactorEvidence(
        universe_id=universe.universe_id,
        role=universe.role,
        status=EvidenceStatus.AVAILABLE,
        target_session="2026-08-07",
        verdict=verdict,
        dataset_version_id=f"data-{universe.universe_id}",
        research_publication_id=f"research-{universe.universe_id}",
        factor_generation_id=f"factor-{universe.universe_id}",
        confidence_sha256=f"sha-{universe.universe_id}",
        direction_sign=1,
        n_obs=500,
        ic_mean=ic_mean,
        ic_ir=0.2,
        t_stat=2.5,
        p_value=q_value,
        q_value=q_value,
        ci95_low=ic_mean - 0.01,
        ci95_high=ic_mean + 0.01,
        long_short_ann_return=0.04,
        long_short_sharpe=0.6,
        cost_bps_per_year_avg=80.0,
    )


def _assess(primary, secondary):
    return assess_factor_across_universes(
        factor_id="MOM_12M",
        target_session="2026-08-07",
        research_universes=UNIVERSES,
        evidence={"SP500": primary, "NASDAQ100": secondary},
    )


@pytest.mark.parametrize(
    ("primary_verdict", "secondary_verdict", "expected"),
    [
        ("PASS", "PASS", CrossUniverseVerdict.ROBUST),
        ("PASS", "WATCH", CrossUniverseVerdict.PRIMARY_ONLY),
        ("WATCH", "PASS", CrossUniverseVerdict.SEGMENT_SPECIFIC),
        ("FAIL", "FAIL", CrossUniverseVerdict.REJECT),
        ("WATCH", "WATCH", CrossUniverseVerdict.INSUFFICIENT),
    ],
)
def test_cross_universe_verdict_table(
    primary_verdict,
    secondary_verdict,
    expected,
):
    result = _assess(
        _evidence(SP500, verdict=primary_verdict),
        _evidence(NASDAQ100, verdict=secondary_verdict),
    )
    assert result.verdict == expected
    assert result.direction_consistent is True


def test_significant_opposite_secondary_is_conflict():
    result = _assess(
        _evidence(SP500, verdict="PASS", ic_mean=0.03),
        _evidence(NASDAQ100, verdict="FAIL", ic_mean=-0.03, q_value=0.04),
    )
    assert result.verdict == CrossUniverseVerdict.CONFLICT
    assert result.direction_consistent is False


def test_missing_required_universe_is_insufficient():
    missing = UniverseFactorEvidence(
        universe_id="NASDAQ100",
        role=ResearchUniverseRole.SECONDARY,
        status=EvidenceStatus.MISSING,
        reason="RESEARCH_PUBLICATION_MISSING",
    )
    result = _assess(_evidence(SP500), missing)
    assert result.verdict == CrossUniverseVerdict.INSUFFICIENT
    assert result.direction_consistent is None


def test_cross_universe_generation_is_hash_bound(monkeypatch, tmp_path):
    monkeypatch.setattr(publication, "cross_universe_root", lambda: tmp_path)
    assessment = _assess(_evidence(SP500), _evidence(NASDAQ100))
    pointer = publication.publish_cross_universe_generation(
        target_session="2026-08-07",
        assessments=[assessment],
        source_bindings={"SP500": {"status": "AVAILABLE"}},
    )

    loaded_pointer, frame, manifest = publication.load_cross_universe_publication()
    assert loaded_pointer["generation_id"] == pointer["generation_id"]
    assert frame.loc[0, "verdict"] == "ROBUST"
    assert manifest["factor_count"] == 1

    assessments_path = Path(pointer["factor_assessments_path"])
    assessments_path.write_bytes(assessments_path.read_bytes() + b"tamper")
    with pytest.raises(
        publication.CrossUniversePublicationError,
        match="checksum failed",
    ):
        publication.load_cross_universe_publication()
