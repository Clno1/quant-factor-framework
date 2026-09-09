from __future__ import annotations

import pandas as pd
import pytest

from src.market_regime_research.models import (
    PointInTimeReconstructionError,
)
from src.market_regime_research.pit import (
    normalize_fmp_sp500_changes,
    publish_validated_membership,
    reconstruct_sp500_snapshots,
)


def _raw_changes() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2026-01-08",
                "symbol": "D",
                "addedSecurity": "Delta",
                "removedTicker": "B",
                "removedSecurity": "Beta",
                "reason": "replacement",
            },
            {
                "date": "2026-01-05",
                "symbol": "C",
                "addedSecurity": "Gamma",
                "removedTicker": "A",
                "removedSecurity": "Alpha",
                "reason": "replacement",
            },
        ]
    )


def test_normalizer_does_not_treat_removal_only_symbol_as_an_addition():
    raw = pd.DataFrame(
        [
            {
                "date": "2026-01-05",
                "symbol": "OLD",
                "addedSecurity": "",
                "removedTicker": "OLD",
                "removedSecurity": "Old Company",
                "reason": "removal",
            }
        ]
    )
    result = normalize_fmp_sp500_changes(raw)

    assert pd.isna(result.loc[0, "added_ticker"])
    assert result.loc[0, "removed_ticker"] == "OLD"
    assert result.loc[0, "quality_status"] == "OK"


def test_normalizer_preserves_but_flags_an_unnamed_replacement_addition():
    raw = pd.DataFrame(
        [
            {
                "date": "2026-01-05",
                "symbol": "NEW",
                "addedSecurity": "",
                "removedTicker": "OLD",
                "removedSecurity": "",
                "reason": "replacement",
            }
        ]
    )
    result = normalize_fmp_sp500_changes(raw)

    assert result.loc[0, "added_ticker"] == "NEW"
    assert result.loc[0, "removed_ticker"] == "OLD"
    assert result.loc[0, "quality_status"] == "WARNING"
    assert "ADDITION_INFERRED_WITHOUT_SECURITY_NAME" in result.loc[0, "reason_codes"]


def test_clean_events_reconstruct_complete_post_event_snapshots():
    result = reconstruct_sp500_snapshots(
        ["C", "D"],
        _raw_changes(),
        start="2026-01-01",
        asof="2026-01-10",
        min_snapshot_members=1,
        max_snapshot_members=10,
        strict=True,
    )

    snapshots = {
        date: set(group["ticker"])
        for date, group in result.membership.groupby("date")
    }
    assert snapshots[pd.Timestamp("2026-01-01")] == {"A", "B"}
    assert snapshots[pd.Timestamp("2026-01-05")] == {"B", "C"}
    assert snapshots[pd.Timestamp("2026-01-08")] == {"C", "D"}
    assert snapshots[pd.Timestamp("2026-01-10")] == {"C", "D"}
    assert result.diagnostics["quality_status"] == "PASS"


def test_inconsistent_event_fails_closed_in_strict_mode():
    with pytest.raises(
        PointInTimeReconstructionError,
        match="cannot produce a clean PIT universe",
    ):
        reconstruct_sp500_snapshots(
            ["D"],
            _raw_changes(),
            start="2026-01-01",
            asof="2026-01-10",
            min_snapshot_members=1,
            max_snapshot_members=10,
            strict=True,
        )


def test_inconsistent_event_is_visible_in_diagnostic_candidate():
    result = reconstruct_sp500_snapshots(
        ["D"],
        _raw_changes(),
        start="2026-01-01",
        asof="2026-01-10",
        min_snapshot_members=1,
        max_snapshot_members=10,
        strict=False,
    )

    assert result.diagnostics["quality_status"] == "FAIL"
    assert result.diagnostics["inconsistency_count"] > 0
    assert any(
        item["type"] == "ADDITION_ABSENT_FROM_LATER_STATE"
        for item in result.diagnostics["inconsistencies"]
    )


def test_events_after_current_snapshot_asof_fail_closed():
    changes = _raw_changes()
    with pytest.raises(
        PointInTimeReconstructionError,
        match="cannot produce a clean PIT universe",
    ):
        reconstruct_sp500_snapshots(
            ["C", "D"],
            changes,
            start="2026-01-01",
            asof="2026-01-07",
            min_snapshot_members=1,
            max_snapshot_members=10,
            strict=True,
        )


def test_serialized_normalized_reason_codes_remain_a_list():
    normalized = normalize_fmp_sp500_changes(_raw_changes())
    normalized["reason_codes"] = normalized["reason_codes"].map(
        lambda values: "[]"
    )

    result = reconstruct_sp500_snapshots(
        ["C", "D"],
        normalized,
        start="2026-01-01",
        asof="2026-01-10",
        min_snapshot_members=1,
        max_snapshot_members=10,
        strict=True,
    )

    assert result.diagnostics["quality_status"] == "PASS"


def test_same_ticker_added_and_removed_across_same_day_rows_fails_closed():
    raw = pd.DataFrame(
        [
            {
                "date": "2026-01-05",
                "symbol": "A",
                "addedSecurity": "Alpha",
                "removedTicker": "",
                "removedSecurity": "",
                "reason": "addition",
            },
            {
                "date": "2026-01-05",
                "symbol": "A",
                "addedSecurity": "",
                "removedTicker": "A",
                "removedSecurity": "Alpha",
                "reason": "removal",
            },
        ]
    )

    with pytest.raises(
        PointInTimeReconstructionError,
        match="cannot produce a clean PIT universe",
    ):
        reconstruct_sp500_snapshots(
            ["A"],
            raw,
            start="2026-01-01",
            asof="2026-01-10",
            min_snapshot_members=1,
            max_snapshot_members=10,
            strict=True,
        )


def test_non_strict_candidate_cannot_be_published(tmp_path):
    result = reconstruct_sp500_snapshots(
        ["C", "D"],
        _raw_changes(),
        start="2026-01-01",
        asof="2026-01-10",
        min_snapshot_members=1,
        max_snapshot_members=10,
        strict=False,
    )

    with pytest.raises(PointInTimeReconstructionError, match="strict"):
        publish_validated_membership(result, tmp_path / "SP500.parquet")
