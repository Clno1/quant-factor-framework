from __future__ import annotations

import pandas as pd

from src.factors.broad_research_gate import assess_broad_research_readiness


def _publication(policy: str) -> dict:
    return {
        "publication_mode": "FACTOR_DATA",
        "publication_id": "factor-data-v1",
        "target_session": "2026-08-10",
        "classification_policy": policy,
        "factors": {
            "MOM_1M": {"date_end": "2026-08-10"},
            "VOL_20D": {"date_end": "2026-08-10"},
        },
    }


def _membership() -> pd.DataFrame:
    return pd.DataFrame([
        {"date": "2020-01-31", "security_id": "sec_a", "active": True},
        {"date": "2020-01-31", "security_id": "sec_b", "active": True},
    ])


def _audit(session_count: int) -> pd.DataFrame:
    dates = pd.bdate_range("2020-01-02", periods=session_count)
    return pd.DataFrame([
        {"factor_id": factor, "date": day, "clean_non_null": 600}
        for factor in ("MOM_1M", "VOL_20D")
        for day in dates
    ])


def test_latest_known_industry_blocks_formal_broad_research():
    result = assess_broad_research_readiness(
        publication=_publication("LATEST_KNOWN_BACKFILL_NOT_PIT"),
        membership=_membership(),
        classifications=pd.DataFrame([
            {
                "security_id": security_id,
                "sector": "Technology",
                "effective_from": "2010-01-01",
                "effective_to": None,
                "knowledge_date": "2026-08-10",
                "classification_policy": "LATEST_KNOWN_BACKFILL_NOT_PIT",
            }
            for security_id in ("sec_a", "sec_b")
        ]),
        preprocessing_audit=_audit(5),
        expected_factor_ids=["MOM_1M", "VOL_20D"],
        minimum_evaluable_sessions=5,
        minimum_cross_section=500,
        minimum_pit_industry_coverage=0.95,
        accepted_classification_policies=["PIT_EFFECTIVE_DATED"],
    )

    assert result.status == "BLOCKED"
    assert "PIT_CLASSIFICATION_POLICY" in result.blockers
    assert "PIT_INDUSTRY_COVERAGE" in result.blockers
    assert "EVALUABLE_HISTORY" not in result.blockers


def test_strict_pit_industry_and_sufficient_history_pass_readiness():
    result = assess_broad_research_readiness(
        publication=_publication("PIT_EFFECTIVE_DATED"),
        membership=_membership(),
        classifications=pd.DataFrame([
            {
                "security_id": security_id,
                "sector": "Technology",
                "effective_from": "2019-01-01",
                "effective_to": None,
                "knowledge_date": "2019-01-01",
                "classification_policy": "PIT_EFFECTIVE_DATED",
            }
            for security_id in ("sec_a", "sec_b")
        ]),
        preprocessing_audit=_audit(5),
        expected_factor_ids=["MOM_1M", "VOL_20D"],
        minimum_evaluable_sessions=5,
        minimum_cross_section=500,
        minimum_pit_industry_coverage=0.95,
        accepted_classification_policies=["PIT_EFFECTIVE_DATED"],
    )

    assert result.status == "READY"
    assert result.blockers == ()
