from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import src.data.nasdaq100_pit as nasdaq100_pit
from src.data.nasdaq100_pit import (
    MINIMUM_VERIFIED_EVENT_GROUPS,
    apply_nasdaq100_current_corrections,
    apply_nasdaq100_event_corrections,
    compare_current_constituents,
    current_constituent_diagnostics,
    load_nasdaq100_correction_registry,
    load_nasdaq100_verification_registry,
    normalize_fmp_nasdaq100_changes,
    verify_nasdaq100_event_groups,
)
from src.market_regime_research.models import DataContractError
from src.market_regime_research.pit import reconstruct_sp500_snapshots


def _changes() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2026-06-21",
                "dateAdded": "June 22, 2026",
                "symbol": "ALAB",
                "addedSecurity": "Astera Labs",
                "removedTicker": "INSM",
                "removedSecurity": "Insmed",
                "reason": "Quarterly rebalance",
            },
            {
                "date": "2025-05-18",
                "dateAdded": "May 19, 2025",
                "symbol": "SHOP",
                "addedSecurity": "Shopify",
                "removedTicker": "MDB",
                "removedSecurity": "MongoDB",
                "reason": "Minimum weight",
            },
        ]
    )


def _correction_registry() -> dict:
    return {
        "reviewed_events": [
            {
                "id": "hona",
                "effective_date": "2026-06-29",
                "added_ticker": "HONA",
                "removed_ticker": None,
                "added_security": "Honeywell Aerospace Inc.",
                "current_metadata": {
                    "sector": "Industrials",
                    "sub_industry": "Aerospace & Defense",
                },
                "sources": ["https://example.test/hona"],
            },
            {
                "id": "ea",
                "effective_date": "2026-08-05",
                "added_ticker": None,
                "removed_ticker": "EA",
                "removed_security": "Electronic Arts Inc.",
                "sources": ["https://example.test/ea"],
            },
        ]
    }


def test_nasdaq100_normalization_uses_effective_date_not_provider_date():
    normalized = normalize_fmp_nasdaq100_changes(_changes())

    row = normalized.loc[normalized["added_ticker"].eq("ALAB")].iloc[0]
    assert row["provider_date"] == pd.Timestamp("2026-06-21")
    assert row["effective_date"] == pd.Timestamp("2026-06-22")
    assert row["effective_date_source"] == "dateAdded"
    assert "PROVIDER_DATE_DIFFERS_FROM_EFFECTIVE_DATE" in row["reason_codes"]


def test_nasdaq100_normalized_events_reconstruct_cleanly():
    normalized = normalize_fmp_nasdaq100_changes(_changes())

    result = reconstruct_sp500_snapshots(
        ["ALAB", "SHOP"],
        normalized,
        asof="2026-06-30",
        start="2025-01-01",
        min_snapshot_members=1,
        max_snapshot_members=10,
        strict=True,
    )

    assert result.diagnostics["quality_status"] == "PASS"
    assert result.diagnostics["source_warning_events"] == 2


def test_current_constituents_require_exact_ticker_match():
    provider = pd.DataFrame({"ticker": ["AAPL", "MSFT"]})
    official = pd.DataFrame({"ticker": ["AAPL", "NVDA"]})

    with pytest.raises(DataContractError, match="current constituents differ"):
        compare_current_constituents(provider, official)
    diagnostics = current_constituent_diagnostics(provider, official)
    assert diagnostics["provider_only"] == ["MSFT"]
    assert diagnostics["official_only"] == ["NVDA"]


def test_reviewed_corrections_reconcile_current_and_historical_events():
    provider = pd.DataFrame(
        {
            "ticker": ["EA", "HON"],
            "name": ["Electronic Arts", "Honeywell Technologies"],
            "sector": ["Technology", "Industrials"],
            "sub_industry": ["Games", "Industrial Conglomerates"],
        }
    )
    official = pd.DataFrame(
        {
            "ticker": ["HON", "HONA"],
            "name": ["Honeywell Technologies", "Honeywell Aerospace"],
        }
    )
    normalized = normalize_fmp_nasdaq100_changes(_changes()).iloc[0:0].copy()

    corrected_current, current_audit, raw, corrected = (
        apply_nasdaq100_current_corrections(
            provider,
            official,
            _correction_registry(),
            asof="2026-08-10",
        )
    )
    corrected_events, event_audit = apply_nasdaq100_event_corrections(
        normalized,
        _correction_registry(),
        asof="2026-08-10",
    )

    assert raw["provider_only"] == ["EA"]
    assert raw["official_only"] == ["HONA"]
    assert corrected["quality_status"] == "PASS"
    assert corrected_current["ticker"].tolist() == ["HON", "HONA"]
    hona = corrected_current.loc[corrected_current["ticker"].eq("HONA")].iloc[0]
    assert hona["sector"] == "Industrials"
    assert {action for item in current_audit for action in item["actions"]} == {
        "official_current_addition_applied",
        "official_current_removal_applied",
    }
    assert {item["action"] for item in event_audit} == {
        "synthetic_event_added"
    }

    result = reconstruct_sp500_snapshots(
        corrected_current,
        corrected_events,
        asof="2026-08-10",
        start="2026-06-01",
        min_snapshot_members=1,
        max_snapshot_members=10,
        strict=True,
    )
    snapshots = result.membership.loc[result.membership["active"]]
    before_spin = set(
        snapshots.loc[
            snapshots["date"].eq(pd.Timestamp("2026-06-01")), "ticker"
        ]
    )
    after_spin = set(
        snapshots.loc[
            snapshots["date"].eq(pd.Timestamp("2026-06-29")), "ticker"
        ]
    )
    after_ea = set(
        snapshots.loc[
            snapshots["date"].eq(pd.Timestamp("2026-08-05")), "ticker"
        ]
    )
    assert before_spin == {"EA", "HON"}
    assert after_spin == {"EA", "HON", "HONA"}
    assert after_ea == {"HON", "HONA"}


def test_reviewed_event_is_not_duplicated_after_provider_catches_up():
    normalized = normalize_fmp_nasdaq100_changes(_changes())
    provider_event = pd.DataFrame(
        [
            {
                "effective_date": pd.Timestamp("2026-06-29"),
                "provider_date": pd.Timestamp("2026-06-29"),
                "effective_date_source": "dateAdded",
                "added_ticker": "HONA",
                "removed_ticker": None,
                "added_security": "Honeywell Aerospace Inc.",
                "removed_security": None,
                "reason": "Spin-off",
                "source_row": 99,
                "quality_status": "OK",
                "reason_codes": [],
            }
        ]
    )
    normalized = pd.concat([normalized, provider_event], ignore_index=True)

    corrected, audit = apply_nasdaq100_event_corrections(
        normalized,
        {"reviewed_events": [_correction_registry()["reviewed_events"][0]]},
        asof="2026-08-10",
    )

    assert int(corrected["added_ticker"].eq("HONA").sum()) == 1
    assert audit[0]["action"] == "provider_event_present"


def test_reviewed_event_fails_closed_on_conflicting_provider_shape():
    normalized = normalize_fmp_nasdaq100_changes(_changes())
    normalized = pd.concat(
        [
            normalized,
            pd.DataFrame(
                [
                    {
                        "effective_date": pd.Timestamp("2026-06-29"),
                        "provider_date": pd.Timestamp("2026-06-29"),
                        "effective_date_source": "dateAdded",
                        "added_ticker": "HONA",
                        "removed_ticker": "WRONG",
                        "added_security": "Honeywell Aerospace Inc.",
                        "removed_security": "Wrong Inc.",
                        "reason": "Wrong paired event",
                        "source_row": 99,
                        "quality_status": "OK",
                        "reason_codes": [],
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    with pytest.raises(DataContractError, match="conflicts with provider events"):
        apply_nasdaq100_event_corrections(
            normalized,
            {"reviewed_events": [_correction_registry()["reviewed_events"][0]]},
            asof="2026-08-10",
        )


def test_reviewed_provider_event_corrections_fix_exact_malformed_rows():
    normalized = pd.DataFrame(
        [
            {
                "effective_date": pd.Timestamp("2025-11-06"),
                "provider_date": pd.Timestamp("2025-11-05"),
                "effective_date_source": "dateAdded",
                "added_ticker": "SOLS",
                "removed_ticker": "SOLS",
                "added_security": "Solstice Advanced Materials",
                "removed_security": "Solstice Advanced Materials",
                "reason": "Did not meet minimum monthly weight requirements",
                "source_row": 1,
                "quality_status": "ERROR",
                "reason_codes": [
                    "SAME_TICKER_ADDED_AND_REMOVED",
                    "PROVIDER_DATE_DIFFERS_FROM_EFFECTIVE_DATE",
                ],
            },
            {
                "effective_date": pd.Timestamp("2023-12-18"),
                "provider_date": pd.Timestamp("2023-12-18"),
                "effective_date_source": "dateAdded",
                "added_ticker": None,
                "removed_ticker": "SGEN",
                "added_security": None,
                "removed_security": "Seagen Inc",
                "reason": "Annual Re-ranking",
                "source_row": 2,
                "quality_status": "OK",
                "reason_codes": [],
            },
        ]
    )
    registry = {
        "reviewed_events": [
            {
                "id": "sols",
                "effective_date": "2025-11-06",
                "added_ticker": None,
                "removed_ticker": "SOLS",
                "removed_security": "Solstice Advanced Materials",
                "provider_event": {
                    "added_ticker": "SOLS",
                    "removed_ticker": "SOLS",
                    "reason_contains": "minimum monthly weight requirements",
                },
                "sources": ["https://example.test/sols"],
            },
            {
                "id": "ttwo",
                "effective_date": "2023-12-18",
                "added_ticker": "TTWO",
                "removed_ticker": "SGEN",
                "added_security": "Take-Two Interactive Software, Inc.",
                "removed_security": "Seagen Inc.",
                "provider_event": {
                    "added_ticker": None,
                    "removed_ticker": "SGEN",
                    "reason_contains": "Annual Re-ranking",
                },
                "sources": ["https://example.test/ttwo"],
            },
        ]
    }

    corrected, audit = apply_nasdaq100_event_corrections(
        normalized,
        registry,
        asof="2026-08-10",
    )

    sols = corrected.loc[corrected["effective_date"].eq("2025-11-06")].iloc[0]
    ttwo = corrected.loc[corrected["effective_date"].eq("2023-12-18")].iloc[0]
    assert pd.isna(sols["added_ticker"])
    assert sols["removed_ticker"] == "SOLS"
    assert "SAME_TICKER_ADDED_AND_REMOVED" not in sols["reason_codes"]
    assert ttwo["added_ticker"] == "TTWO"
    assert ttwo["removed_ticker"] == "SGEN"
    assert {item["action"] for item in audit} == {"provider_event_corrected"}


def test_reviewed_provider_event_correction_fails_on_provider_drift():
    normalized = pd.DataFrame(
        [
            {
                "effective_date": pd.Timestamp("2023-12-18"),
                "provider_date": pd.Timestamp("2023-12-18"),
                "effective_date_source": "dateAdded",
                "added_ticker": None,
                "removed_ticker": "SGEN",
                "added_security": None,
                "removed_security": "Different Company",
                "reason": "Annual Re-ranking",
                "source_row": 1,
                "quality_status": "OK",
                "reason_codes": [],
            }
        ]
    )
    registry = {
        "reviewed_events": [
            {
                "id": "ttwo",
                "effective_date": "2023-12-18",
                "added_ticker": "TTWO",
                "removed_ticker": "SGEN",
                "added_security": "Take-Two Interactive Software, Inc.",
                "removed_security": "Seagen Inc.",
                "provider_event": {
                    "added_ticker": None,
                    "removed_ticker": "SGEN",
                    "reason_contains": "Annual Re-ranking",
                    "removed_security_contains": "Seagen",
                },
                "sources": ["https://example.test/ttwo"],
            }
        ]
    }

    with pytest.raises(DataContractError, match="expected exactly one"):
        apply_nasdaq100_event_corrections(
            normalized,
            registry,
            asof="2026-08-10",
        )


def test_reviewed_provider_event_can_correct_reason_without_changing_pair():
    normalized = pd.DataFrame(
        [
            {
                "effective_date": pd.Timestamp("2022-02-22"),
                "provider_date": pd.Timestamp("2022-02-22"),
                "effective_date_source": "dateAdded",
                "added_ticker": "AZN",
                "removed_ticker": "XLNX",
                "added_security": "AstraZeneca plc (ADR)",
                "removed_security": "Xilinx Inc",
                "reason": "Annual Re-ranking",
                "source_row": 1,
                "quality_status": "OK",
                "reason_codes": [],
            }
        ]
    )
    corrected_reason = "Xilinx was acquired by AMD in an all-stock merger."
    registry = {
        "reviewed_events": [
            {
                "id": "xlnx",
                "effective_date": "2022-02-22",
                "added_ticker": "AZN",
                "removed_ticker": "XLNX",
                "added_security": "AstraZeneca plc (ADR)",
                "removed_security": "Xilinx Inc",
                "corrected_reason": corrected_reason,
                "provider_event": {
                    "added_ticker": "AZN",
                    "removed_ticker": "XLNX",
                    "reason_contains": "Annual Re-ranking",
                    "removed_security_contains": "Xilinx",
                },
                "sources": ["https://example.test/xlnx"],
            }
        ]
    }

    corrected, audit = apply_nasdaq100_event_corrections(
        normalized,
        registry,
        asof="2026-08-10",
    )
    repeated, repeated_audit = apply_nasdaq100_event_corrections(
        corrected,
        registry,
        asof="2026-08-10",
    )

    assert corrected.iloc[0]["reason"] == corrected_reason
    assert audit[0]["action"] == "provider_event_corrected"
    assert repeated.iloc[0]["reason"] == corrected_reason
    assert repeated_audit[0]["action"] == "provider_event_already_corrected"


def test_current_correction_does_not_hide_unreviewed_difference():
    provider = pd.DataFrame({"ticker": ["EA", "HON", "MSFT"]})
    official = pd.DataFrame({"ticker": ["HON", "HONA", "NVDA"]})

    _frame, _audit, _raw, corrected = apply_nasdaq100_current_corrections(
        provider,
        official,
        _correction_registry(),
        asof="2026-08-10",
    )

    assert corrected["quality_status"] == "FAIL"
    assert corrected["provider_only"] == ["MSFT"]
    assert corrected["official_only"] == ["NVDA"]


def test_verified_event_group_fails_closed_on_provider_drift():
    normalized = normalize_fmp_nasdaq100_changes(_changes())
    registry = {
        "verified_event_groups": [
            {
                "id": "shop",
                "effective_date": "2025-05-19",
                "additions": ["SHOP"],
                "removals": ["WRONG"],
                "sources": ["https://example.test/shop"],
            }
        ]
    }

    with pytest.raises(DataContractError, match="shop.*drifted"):
        verify_nasdaq100_event_groups(
            normalized,
            registry,
            asof="2026-06-30",
        )


def test_production_registry_has_required_official_event_coverage():
    registry, _path, _checksum = load_nasdaq100_verification_registry()

    groups = registry["verified_event_groups"]
    assert len(groups) >= MINIMUM_VERIFIED_EVENT_GROUPS
    assert all(group["sources"] for group in groups)


def test_production_correction_registry_is_source_backed():
    registry, _path, _checksum = load_nasdaq100_correction_registry()

    events = registry["reviewed_events"]
    assert {(event.get("added_ticker"), event.get("removed_ticker")) for event in events} >= {
        ("TTWO", "SGEN"),
        (None, "SOLS"),
        ("HONA", None),
        (None, "EA"),
    }
    assert all(event["sources"] for event in events)


def test_formal_pit_publication_preserves_strict_gate(tmp_path, monkeypatch):
    event_dates = [
        "2020-02-03",
        "2020-03-02",
        "2020-04-01",
        "2020-05-01",
        "2020-06-01",
        "2020-07-01",
        "2020-08-03",
        "2020-09-01",
        "2020-10-01",
        "2020-11-02",
    ]
    changes = pd.DataFrame(
        [
            {
                "date": effective_date,
                "dateAdded": effective_date,
                "symbol": f"N{position}",
                "addedSecurity": f"New {position}",
                "removedTicker": f"N{position - 1}",
                "removedSecurity": f"New {position - 1}",
                "reason": "Test replacement",
            }
            for position, effective_date in enumerate(event_dates, start=1)
        ]
    )
    current = pd.DataFrame(
        {
            "ticker": ["ANCHOR", "N10"],
            "name": ["Anchor", "New 10"],
        }
    )
    verification = {
        "schema_version": 1,
        "universe": "NASDAQ100",
        "provider_contract": {},
        "official_current": {
            "url": "https://example.test/current",
            "minimum_members": 1,
            "maximum_members": 3,
            "maximum_staleness_calendar_days": 7,
        },
        "verified_event_groups": [
            {
                "id": f"event-{position}",
                "effective_date": effective_date,
                "additions": [f"N{position}"],
                "removals": [f"N{position - 1}"],
                "sources": [f"https://example.test/event-{position}"],
            }
            for position, effective_date in enumerate(event_dates, start=1)
        ],
    }
    corrections = {
        "schema_version": 1,
        "universe": "NASDAQ100",
        "reviewed_events": [],
    }
    verification_path = tmp_path / "verification.yaml"
    corrections_path = tmp_path / "corrections.yaml"
    verification_path.write_text(json.dumps(verification), encoding="utf-8")
    corrections_path.write_text(json.dumps(corrections), encoding="utf-8")

    def project_path(value):
        path = Path(value)
        return path if path.is_absolute() else tmp_path / path

    membership_path = tmp_path / "pit" / "NASDAQ100.parquet"
    monkeypatch.setattr(nasdaq100_pit, "_project_path", project_path)
    monkeypatch.setattr(
        nasdaq100_pit,
        "_membership_target",
        lambda: membership_path,
    )

    result = nasdaq100_pit.build_main_nasdaq100_pit(
        target_session="2024-01-02",
        start="2020-01-02",
        verification_path=verification_path,
        corrections_path=corrections_path,
        current_frame=current,
        official_frame=current,
        official_asof="2024-01-02",
        changes_frame=changes,
    )

    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    diagnostics = json.loads(result.diagnostics_path.read_text(encoding="utf-8"))
    assert result.status == "PUBLISHED"
    assert result.diagnostics["strict"] is True
    assert diagnostics["strict"] is True
    assert metadata["strict"] is True
    assert metadata["diagnostics"]["strict"] is True
