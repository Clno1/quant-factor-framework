from __future__ import annotations

import json

import pandas as pd

from scripts.propose_research_history_policy import build_proposal


def test_proposal_includes_invalid_interval_ticker_and_scoped_provider_failure(
    tmp_path,
):
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    master = pd.DataFrame([
        {
            "security_id": "sec_11111111111111111111111111111111",
            "current_ticker": "BAD",
            "name": "Bad Active",
            "asset_type": "STOCK",
            "primary_exchange": "NASDAQ",
            "listing_date": "2020-01-02",
            "delisting_date": None,
            "trading_status": "ACTIVE",
        },
        {
            "security_id": "sec_22222222222222222222222222222222",
            "current_ticker": "OLD",
            "name": "Old Inactive",
            "asset_type": "STOCK",
            "primary_exchange": "NYSE",
            "listing_date": "2019-01-02",
            "delisting_date": "2024-01-02",
            "trading_status": "INACTIVE",
        },
        {
            "security_id": "sec_33333333333333333333333333333333",
            "current_ticker": "NOTE",
            "name": "Out of Scope Note",
            "asset_type": "NOTE",
            "primary_exchange": "NYSE",
            "listing_date": "2019-01-02",
            "delisting_date": "2024-01-02",
            "trading_status": "INACTIVE",
        },
    ])
    symbols = pd.DataFrame([
        {
            "security_id": "sec_11111111111111111111111111111111",
            "ticker": "BAD",
        },
        {
            "security_id": "sec_22222222222222222222222222222222",
            "ticker": "OLD",
        },
        {
            "security_id": "sec_33333333333333333333333333333333",
            "ticker": "NOTE",
        },
    ])
    master.to_parquet(candidate_dir / "master.parquet", index=False)
    symbols.to_parquet(candidate_dir / "symbols.parquet", index=False)
    (candidate_dir / "audit.json").write_text(json.dumps({
        "target_session": "2026-08-14",
        "quality": {
            "ticker_interval_conflicts": [
                "invalid interval BAD 2026-08-14..2026-08-13",
            ],
        },
    }))
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({
        "target_session": "2026-08-14",
        "history_start": "2019-01-01",
        "alias_failures": [
            {"security_id": "sec_22222222222222222222222222222222"},
            {"security_id": "sec_33333333333333333333333333333333"},
        ],
    }))

    payload, diagnostics = build_proposal(
        candidate_dir=candidate_dir,
        coverage_checkpoint=checkpoint,
        activation_session="2026-08-14",
        approved_at="2026-08-16",
        approved_by="project_owner",
        existing_policy={
            "entries": [{
                "security_id": "sec_11111111111111111111111111111111",
                "policy": "PROSPECTIVE_ONLY",
                "effective_from": "2026-08-14",
                "reason_codes": ["PRIOR_PROVIDER_REVIEW"],
            }],
        },
    )

    entries = {item["current_ticker"]: item for item in payload["entries"]}
    assert set(entries) == {"BAD", "OLD"}
    assert entries["BAD"]["policy"] == "PROSPECTIVE_ONLY"
    assert entries["BAD"]["reason_codes"] == [
        "PRIOR_PROVIDER_REVIEW",
        "UNVERIFIABLE_TICKER_INTERVAL",
    ]
    assert entries["OLD"]["policy"] == "EXCLUDED_UNVERIFIABLE_HISTORY"
    assert entries["OLD"]["reason_codes"] == ["FMP_HISTORY_UNVERIFIABLE"]
    assert diagnostics["proposal_count"] == 2
    assert diagnostics["retained_existing_policy_count"] == 1
    assert diagnostics["new_policy_identity_count"] == 1
    assert diagnostics["checkpoint_failures_outside_current_scope"] == [
        "sec_33333333333333333333333333333333"
    ]
