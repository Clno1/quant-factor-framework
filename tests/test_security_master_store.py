from __future__ import annotations

from pathlib import Path
import tempfile

import pandas as pd
import pytest

from src.data.security_master import CLASSIFICATION_POLICY
from src.data.security_master_store import (
    SecurityMasterStore,
    build_security_master_candidate,
)


def _profiles() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "ticker": "OLD",
            "name": "Renamed Software",
            "asset_type": "STOCK",
            "exchange": "NASDAQ",
            "country": "US",
            "currency": "USD",
            "cik": "0000123456",
            "isin": "US0000000001",
            "cusip": "000000001",
            "listing_date": "2019-01-02",
            "sector": "Technology",
            "sub_industry": "Software",
            "trading_status": "INACTIVE",
            "is_active": False,
        },
        {
            "ticker": "NEW",
            "name": "Renamed Software",
            "asset_type": "STOCK",
            "exchange": "NASDAQ",
            "country": "US",
            "currency": "USD",
            "cik": "0000123456",
            "isin": "US0000000001",
            "cusip": "000000001",
            "listing_date": "2019-01-02",
            "sector": "Technology",
            "sub_industry": "Software",
            "trading_status": "ACTIVE",
            "is_active": True,
        },
        {
            "ticker": "OTHER",
            "name": "Other Industrial",
            "asset_type": "STOCK",
            "exchange": "NYSE",
            "country": "US",
            "currency": "USD",
            "cik": "0000654321",
            "isin": "US0000000002",
            "cusip": "000000002",
            "listing_date": "2018-04-03",
            "sector": "Industrials",
            "sub_industry": "Machinery",
            "trading_status": "ACTIVE",
            "is_active": True,
        },
    ])


def _candidate() -> object:
    return build_security_master_candidate(
        _profiles(),
        symbol_changes=pd.DataFrame([{
            "date": "2024-06-03",
            "old_ticker": "OLD",
            "new_ticker": "NEW",
            "company_name": "Renamed Software",
        }]),
        delisted_companies=pd.DataFrame(columns=[
            "ticker", "name", "exchange", "ipo_date", "delisted_date",
        ]),
        target_session="2026-08-11",
        minimum_active_stocks=2,
    )


def test_ticker_change_collapses_to_one_security_and_dated_aliases():
    candidate = _candidate()

    assert candidate.quality["status"] == "PASS"
    renamed = candidate.master.loc[candidate.master["current_ticker"].eq("NEW")]
    assert len(renamed) == 1
    security_id = renamed.iloc[0]["security_id"]
    aliases = candidate.symbols.loc[
        candidate.symbols["security_id"].eq(security_id)
    ].set_index("ticker")
    assert set(aliases.index) == {"OLD", "NEW"}
    assert aliases.loc["OLD", "effective_to"] == pd.Timestamp("2024-06-02")
    assert aliases.loc["NEW", "effective_from"] == pd.Timestamp("2024-06-03")
    assert candidate.quality["ticker_interval_conflicts"] == []


def test_provider_slash_ticker_normalizes_to_profile_dash_ticker():
    profiles = _profiles().loc[
        lambda frame: frame["ticker"].isin(["OLD", "NEW"])
    ].copy()
    profiles.loc[profiles["ticker"].eq("OLD"), "ticker"] = "JW-B"
    profiles.loc[profiles["ticker"].eq("NEW"), "ticker"] = "WLYB"
    candidate = build_security_master_candidate(
        profiles,
        symbol_changes=pd.DataFrame([{
            "date": "2024-01-02",
            "old_ticker": "JW/B",
            "new_ticker": "WLYB",
            "company_name": "John Wiley & Sons",
        }]),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-11",
        minimum_active_stocks=1,
    )

    assert candidate.quality["status"] == "PASS"
    assert set(candidate.symbols["ticker"]) == {"JW-B", "WLYB"}
    assert candidate.master["security_id"].nunique() == 1


def test_transitive_immediate_predecessor_wins_over_stronger_ancestor_match():
    base = _profiles().iloc[[0]].copy()
    profiles = pd.concat([
        base.assign(
            ticker="SPHA",
            name="Shepherd Ave Capital Acquisition Corporation",
            cusip="G8089R100",
            isin="KYG8089R1002",
            listing_date="2024-12-06",
            is_active=False,
            trading_status="INACTIVE",
        ),
        base.assign(
            ticker="AIFE",
            name="Aifeex Nexus Acquisition Corporation",
            cusip="",
            isin="",
            listing_date="2024-12-06",
            is_active=False,
            trading_status="INACTIVE",
        ),
        base.assign(
            ticker="PGAC",
            name="Pantages Capital Acquisition Corporation",
            cusip="G8089R100",
            isin="KYG8089R1002",
            listing_date="2025-01-28",
            is_active=True,
            trading_status="ACTIVE",
        ),
    ], ignore_index=True)
    candidate = build_security_master_candidate(
        profiles,
        symbol_changes=pd.DataFrame([
            {"date": "2025-03-12", "old_ticker": "SPHA", "new_ticker": "AIFE"},
            {"date": "2025-08-08", "old_ticker": "SPHA", "new_ticker": "PGAC"},
            {"date": "2025-08-08", "old_ticker": "AIFE", "new_ticker": "PGAC"},
        ]),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-11",
        minimum_active_stocks=1,
    )

    assert candidate.quality["status"] == "PASS"
    aliases = candidate.symbols.sort_values("effective_from")
    assert aliases["ticker"].tolist() == ["SPHA", "AIFE", "PGAC"]
    assert aliases["effective_to"].iloc[:2].tolist() == [
        pd.Timestamp("2025-03-11"),
        pd.Timestamp("2025-08-07"),
    ]


def test_reused_ticker_keeps_old_delisted_listing_separate():
    profile = _profiles().loc[lambda frame: frame["ticker"].eq("OTHER")].copy()
    profile.loc[:, "ticker"] = "REUSE"
    profile.loc[:, "listing_date"] = "2024-01-02"
    candidate = build_security_master_candidate(
        profile,
        symbol_changes=pd.DataFrame(),
        delisted_companies=pd.DataFrame([{
            "ticker": "REUSE",
            "name": "Old Reuse Co",
            "exchange": "NYSE",
            "ipo_date": "2010-01-04",
            "delisted_date": "2020-05-01",
        }]),
        target_session="2026-08-11",
        minimum_active_stocks=1,
    )

    assert candidate.quality["status"] == "PASS"
    assert candidate.master["security_id"].nunique() == 2
    assert set(candidate.master["trading_status"]) == {"ACTIVE", "DELISTED"}
    assert candidate.quality["ticker_interval_conflicts"] == []


def test_symbol_change_delisting_does_not_create_a_duplicate_security():
    candidate = build_security_master_candidate(
        _profiles().loc[lambda frame: frame["ticker"].isin(["OLD", "NEW"])],
        symbol_changes=pd.DataFrame([{
            "date": "2024-06-03",
            "old_ticker": "OLD",
            "new_ticker": "NEW",
            "company_name": "Renamed Software",
        }]),
        delisted_companies=pd.DataFrame([{
            "ticker": "OLD",
            "name": "Renamed Software",
            "exchange": "NASDAQ",
            "ipo_date": "2019-01-02",
            "delisted_date": "2024-06-03",
        }]),
        target_session="2026-08-11",
        minimum_active_stocks=1,
    )

    assert candidate.quality["status"] == "PASS"
    assert candidate.master["security_id"].nunique() == 1
    assert set(candidate.symbols["ticker"]) == {"OLD", "NEW"}


def test_ticker_can_return_in_disjoint_historical_intervals():
    base = _profiles().loc[lambda frame: frame["ticker"].eq("OTHER")].copy()
    profile = pd.concat(
        [base.assign(ticker=ticker) for ticker in ("ROUND", "MIDDLE", "FINAL")],
        ignore_index=True,
    )
    profile.loc[:, "listing_date"] = "2019-01-02"
    profile.loc[:, "is_active"] = profile["ticker"].eq("FINAL")
    profile.loc[:, "trading_status"] = profile["is_active"].map(
        {True: "ACTIVE", False: "INACTIVE"}
    )
    candidate = build_security_master_candidate(
        profile,
        symbol_changes=pd.DataFrame([
            {
                "date": "2020-01-02",
                "old_ticker": "ROUND",
                "new_ticker": "MIDDLE",
                "company_name": "Round Trip",
            },
            {
                "date": "2022-01-03",
                "old_ticker": "MIDDLE",
                "new_ticker": "ROUND",
                "company_name": "Round Trip",
            },
            {
                "date": "2024-01-02",
                "old_ticker": "ROUND",
                "new_ticker": "FINAL",
                "company_name": "Round Trip",
            },
        ]),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-11",
        minimum_active_stocks=1,
    )

    assert candidate.quality["status"] == "PASS"
    aliases = candidate.symbols.sort_values("effective_from", na_position="first")
    assert aliases["ticker"].tolist() == [
        "ROUND", "MIDDLE", "ROUND", "FINAL",
    ]
    assert candidate.quality["ticker_interval_conflicts"] == []


def test_cik_only_event_does_not_merge_conflicting_issue_identifiers():
    profiles = _profiles().loc[lambda frame: frame["ticker"].isin(["OLD", "NEW"])].copy()
    profiles.loc[profiles["ticker"].eq("NEW"), "cusip"] = "999999999"
    profiles.loc[profiles["ticker"].eq("NEW"), "isin"] = "US9999999999"
    candidate = build_security_master_candidate(
        profiles,
        symbol_changes=pd.DataFrame([{
            "date": "2024-06-03",
            "old_ticker": "OLD",
            "new_ticker": "NEW",
            "company_name": "Renamed Software",
        }]),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-11",
        minimum_active_stocks=1,
    )

    assert candidate.quality["status"] == "PASS"
    assert candidate.master["security_id"].nunique() == 2
    diagnostics = candidate.quality["symbol_change_diagnostics"]
    assert diagnostics["verified_event_count"] == 0
    assert diagnostics["unverified_event_count"] == 1


def test_verified_cusip_event_survives_cik_change():
    profiles = _profiles().loc[lambda frame: frame["ticker"].isin(["OLD", "NEW"])].copy()
    profiles.loc[profiles["ticker"].eq("NEW"), "cik"] = "0000999999"
    candidate = build_security_master_candidate(
        profiles,
        symbol_changes=pd.DataFrame([{
            "date": "2024-06-03",
            "old_ticker": "OLD",
            "new_ticker": "NEW",
            "company_name": "Renamed Software",
        }]),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-11",
        minimum_active_stocks=1,
    )

    assert candidate.quality["status"] == "PASS"
    assert candidate.master["security_id"].nunique() == 1


def test_shared_issue_identifiers_without_a_date_fail_closed_on_overlap():
    profiles = _profiles().loc[
        lambda frame: frame["ticker"].isin(["OLD", "NEW"])
    ].copy()
    candidate = build_security_master_candidate(
        profiles,
        symbol_changes=pd.DataFrame(),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-11",
        minimum_active_stocks=1,
    )

    assert candidate.quality["status"] == "FAIL"
    assert candidate.master["security_id"].nunique() == 1
    assert set(candidate.symbols["ticker"]) == {"OLD", "NEW"}
    assert len(candidate.quality["shared_issue_identity_groups"]) == 1
    assert candidate.quality["ticker_interval_conflicts"] == [
        "overlapping aliases for security "
        f"{candidate.master.iloc[0]['security_id']}: OLD and NEW"
    ]


def test_approved_prospective_policy_replaces_unverifiable_history():
    profiles = _profiles().loc[
        lambda frame: frame["ticker"].isin(["OLD", "NEW"])
    ].copy()
    failed = build_security_master_candidate(
        profiles,
        symbol_changes=pd.DataFrame(),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-11",
        minimum_active_stocks=1,
    )
    security_id = str(failed.master.iloc[0]["security_id"])
    candidate = build_security_master_candidate(
        profiles,
        symbol_changes=pd.DataFrame(),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-11",
        minimum_active_stocks=1,
        research_history_policy={
            "decision": {"basis": "approved provider limitation"},
            "entries": [{
                "security_id": security_id,
                "current_ticker": "NEW",
                "name": "Renamed Software",
                "trading_status": "ACTIVE",
                "policy": "PROSPECTIVE_ONLY",
                "effective_from": "2026-08-11",
                "reason_codes": ["OVERLAPPING_TICKER_INTERVALS"],
            }],
        },
    )

    assert candidate.quality["status"] == "PASS"
    assert candidate.quality["prospective_only_count"] == 1
    assert candidate.quality["ticker_interval_conflicts"] == []
    assert candidate.symbols[["ticker", "event_type"]].to_dict("records") == [{
        "ticker": "NEW",
        "event_type": "PROSPECTIVE_ONLY_START",
    }]
    assert candidate.history_policy.iloc[0]["security_id"] == security_id


def test_inactive_security_missing_from_provider_is_carried_forward_for_policy():
    previous_profiles = pd.concat([
        _profiles().loc[lambda frame: frame["ticker"].isin(["NEW", "OTHER"])],
        pd.DataFrame([{
            "ticker": "GONE",
            "name": "Gone Historical Corp",
            "asset_type": "STOCK",
            "exchange": "NASDAQ",
            "country": "US",
            "currency": "USD",
            "cik": "0000777777",
            "isin": "US0000000777",
            "cusip": "000000777",
            "listing_date": "2020-01-02",
            "sector": "Industrials",
            "sub_industry": "Machinery",
            "trading_status": "INACTIVE",
            "is_active": False,
        }]),
    ], ignore_index=True)
    previous = build_security_master_candidate(
        previous_profiles,
        symbol_changes=pd.DataFrame(),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-14",
        minimum_active_stocks=2,
    )
    gone = previous.master.loc[
        previous.master["current_ticker"].eq("GONE")
    ].iloc[0]
    security_id = str(gone["security_id"])

    current = build_security_master_candidate(
        previous_profiles.loc[previous_profiles["is_active"]].copy(),
        symbol_changes=pd.DataFrame(),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-19",
        previous_identity_keys=previous.identity_keys,
        previous_master=previous.master,
        previous_symbols=previous.symbols,
        previous_classifications=previous.classifications,
        minimum_active_stocks=2,
        research_history_policy={
            "decision": {"basis": "approved provider limitation"},
            "entries": [{
                "security_id": security_id,
                "current_ticker": "GONE",
                "name": "Gone Historical Corp",
                "trading_status": "INACTIVE",
                "policy": "EXCLUDED_UNVERIFIABLE_HISTORY",
                "effective_from": "2026-08-14",
                "reason_codes": ["FMP_HISTORY_UNVERIFIABLE"],
            }],
        },
    )

    assert current.quality["status"] == "PASS"
    assert current.quality["carried_forward_excluded_security_count"] == 1
    assert current.quality["carried_forward_excluded_security_ids"] == [security_id]
    assert current.master["security_id"].eq(security_id).sum() == 1
    assert not current.symbols["security_id"].eq(security_id).any()
    assert current.classifications["security_id"].eq(security_id).any()
    assert current.identity_keys["security_id"].eq(security_id).any()
    assert current.history_policy.iloc[0]["security_id"] == security_id


def test_active_security_missing_from_provider_remains_fail_closed():
    previous = build_security_master_candidate(
        _profiles().loc[lambda frame: frame["ticker"].isin(["NEW", "OTHER"])],
        symbol_changes=pd.DataFrame(),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-14",
        minimum_active_stocks=2,
    )
    active = previous.master.loc[
        previous.master["current_ticker"].eq("NEW")
    ].iloc[0]
    security_id = str(active["security_id"])

    with pytest.raises(ValueError, match="observed 0"):
        build_security_master_candidate(
            _profiles().loc[lambda frame: frame["ticker"].eq("OTHER")],
            symbol_changes=pd.DataFrame(),
            delisted_companies=pd.DataFrame(),
            target_session="2026-08-19",
            previous_identity_keys=previous.identity_keys,
            previous_master=previous.master,
            previous_symbols=previous.symbols,
            previous_classifications=previous.classifications,
            minimum_active_stocks=1,
            research_history_policy={
                "decision": {"basis": "approved provider limitation"},
                "entries": [{
                    "security_id": security_id,
                    "current_ticker": "NEW",
                    "name": "Renamed Software",
                    "trading_status": "ACTIVE",
                    "policy": "PROSPECTIVE_ONLY",
                    "effective_from": "2026-08-14",
                    "reason_codes": ["FMP_HISTORY_UNVERIFIABLE"],
                }],
            },
        )


def test_ambiguous_provider_issue_key_is_quarantined_not_merged():
    profiles = _profiles().loc[
        lambda frame: frame["ticker"].isin(["NEW", "OTHER"])
    ].copy()
    profiles.loc[profiles["ticker"].eq("OTHER"), "cusip"] = "000000001"
    candidate = build_security_master_candidate(
        profiles,
        symbol_changes=pd.DataFrame(),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-11",
        minimum_active_stocks=2,
    )

    assert candidate.quality["status"] == "PASS"
    assert candidate.master["security_id"].nunique() == 2
    assert not candidate.identity_keys.loc[
        candidate.identity_keys["key_type"].eq("CUSIP"), "key_value"
    ].eq("000000001").any()
    assert candidate.quality["quarantined_identity_keys"] == [{
        "key_type": "CUSIP",
        "key_value": "000000001",
        "security_ids": sorted(candidate.master["security_id"].astype(str)),
    }]


def test_shared_issue_identity_is_idempotent_across_generations():
    profiles = _profiles().loc[
        lambda frame: frame["ticker"].isin(["OLD", "NEW"])
    ].copy()
    profiles.loc[profiles["ticker"].eq("NEW"), "listing_date"] = "2024-06-03"
    first = build_security_master_candidate(
        profiles,
        symbol_changes=pd.DataFrame(),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-11",
        minimum_active_stocks=1,
    )
    second = build_security_master_candidate(
        profiles,
        symbol_changes=pd.DataFrame(),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-11",
        previous_identity_keys=first.identity_keys,
        minimum_active_stocks=1,
    )

    assert second.quality["status"] == "PASS"
    assert set(second.master["security_id"]) == set(first.master["security_id"])
    assert second.quality["previous_ambiguous_identity_keys"] == []
    pd.testing.assert_frame_equal(second.master, first.master)
    assert set(first.master["updated_at"]) == {
        pd.Timestamp("2026-08-11", tz="UTC")
    }


def test_shared_issue_aliases_use_current_ticker_listing_boundary():
    profiles = _profiles().loc[
        lambda frame: frame["ticker"].isin(["OLD", "NEW"])
    ].copy()
    profiles.loc[:, "is_active"] = True
    profiles.loc[:, "trading_status"] = "ACTIVE"
    profiles.loc[profiles["ticker"].eq("OLD"), "listing_date"] = "2019-01-02"
    profiles.loc[profiles["ticker"].eq("NEW"), "listing_date"] = "2024-06-03"
    candidate = build_security_master_candidate(
        profiles,
        symbol_changes=pd.DataFrame(),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-11",
        minimum_active_stocks=1,
    )

    assert candidate.quality["status"] == "PASS"
    aliases = candidate.symbols.set_index("ticker")
    assert aliases.loc["OLD", "effective_to"] == pd.Timestamp("2024-06-02")
    assert aliases.loc["NEW", "effective_from"] == pd.Timestamp("2024-06-03")


def test_ambiguous_previous_key_is_ignored_and_reported():
    profiles = _profiles().loc[
        lambda frame: frame["ticker"].isin(["NEW", "OTHER"])
    ].copy()
    first = build_security_master_candidate(
        profiles,
        symbol_changes=pd.DataFrame(),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-11",
        minimum_active_stocks=2,
    )
    poisoned = pd.concat([
        first.identity_keys,
        pd.DataFrame([
            {
                "security_id": security_id,
                "key_type": "CUSIP",
                "key_value": "POISONED",
                "source": "TEST",
                "source_asof": pd.Timestamp("2026-08-11"),
            }
            for security_id in first.master["security_id"]
        ]),
    ], ignore_index=True)
    second = build_security_master_candidate(
        profiles,
        symbol_changes=pd.DataFrame(),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-11",
        previous_identity_keys=poisoned,
        minimum_active_stocks=2,
    )

    assert second.quality["status"] == "PASS"
    assert len(second.quality["previous_ambiguous_identity_keys"]) == 1
    assert "CUSIP:POISONED" in second.quality[
        "previous_ambiguous_identity_keys"
    ][0]


def test_unverified_symbol_event_does_not_stitch_unrelated_profiles():
    profiles = _profiles().loc[lambda frame: frame["ticker"].isin(["NEW", "OTHER"])].copy()
    profiles.loc[profiles["ticker"].eq("OTHER"), "ticker"] = "OLD"
    candidate = build_security_master_candidate(
        profiles,
        symbol_changes=pd.DataFrame([{
            "date": "2024-06-03",
            "old_ticker": "OLD",
            "new_ticker": "NEW",
            "company_name": "Ambiguous Provider Event",
        }]),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-11",
        minimum_active_stocks=2,
    )

    assert candidate.quality["status"] == "PASS"
    assert candidate.master["security_id"].nunique() == 2
    diagnostics = candidate.quality["symbol_change_diagnostics"]
    assert diagnostics["verified_event_count"] == 0
    assert diagnostics["unverified_event_count"] == 1


def test_impossible_delisted_listing_date_becomes_explicit_unknown():
    candidate = build_security_master_candidate(
        _profiles().loc[lambda frame: frame["ticker"].eq("OTHER")],
        symbol_changes=pd.DataFrame(),
        delisted_companies=pd.DataFrame([{
            "ticker": "BADDATE",
            "name": "Bad Date Corp",
            "exchange": "NYSE",
            "ipo_date": "2025-01-02",
            "delisted_date": "2020-01-02",
        }]),
        target_session="2026-08-11",
        minimum_active_stocks=1,
    )

    assert candidate.quality["status"] == "PASS"
    bad = candidate.master.loc[
        candidate.master["current_ticker"].eq("BADDATE")
    ].iloc[0]
    assert pd.isna(bad["listing_date"])
    assert bad["delisting_date"] == pd.Timestamp("2020-01-02")
    assert candidate.quality["invalid_delisted_listing_dates"] == [{
        "ticker": "BADDATE",
        "provider_listing_date": "2025-01-02",
        "delisting_date": "2020-01-02",
        "resolution": "LISTING_DATE_SET_TO_UNKNOWN",
    }]


def test_store_publishes_all_frames_atomically_and_verifies_hashes():
    candidate = _candidate()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = SecurityMasterStore(
            root / "catalog.duckdb",
            root / "security_master",
        )

        generation = store.publish(candidate)
        loaded_generation, frames = store.load_published()

        assert loaded_generation.generation_id == generation.generation_id
        assert len(frames["master"]) == len(candidate.master)
        assert set(frames) == {
            "master", "symbols", "classifications", "identity_keys",
            "history_policy",
        }
        assert frames["history_policy"].empty
        assert set(frames["classifications"]["classification_policy"]) == {
            CLASSIFICATION_POLICY,
        }

        master_path = Path(generation.master_path)
        master_path.write_bytes(master_path.read_bytes() + b"tamper")
        with pytest.raises(RuntimeError, match="hash verification failed"):
            store.load_published()


def test_published_ticker_resolution_is_point_in_time_and_fail_closed():
    candidate = _candidate()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = SecurityMasterStore(root / "catalog.duckdb", root / "snapshots")
        generation = store.publish(candidate)

        old = store.resolve_ticker("OLD", asof="2024-06-02")
        new = store.resolve_ticker("NEW", asof="2024-06-03")
        assert old.security_id == new.security_id
        assert old.current_ticker == "NEW"
        assert new.generation_id == generation.generation_id
        assert new.to_dict()["asof"] == "2024-06-03"
        with pytest.raises(FileNotFoundError, match="not known"):
            store.resolve_ticker("OLD", asof="2024-06-03")
        with pytest.raises(ValueError, match="exceeds"):
            store.resolve_ticker("NEW", asof="2026-08-12")


def test_failed_candidate_cannot_advance_pointer():
    candidate = build_security_master_candidate(
        _profiles().assign(sector="", sub_industry=""),
        symbol_changes=pd.DataFrame(),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-11",
        minimum_active_stocks=2,
    )
    assert candidate.quality["status"] == "FAIL"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = SecurityMasterStore(root / "catalog.duckdb", root / "snapshots")
        with pytest.raises(RuntimeError, match="failed quality gates"):
            store.publish(candidate)
