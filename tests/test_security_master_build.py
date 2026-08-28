from __future__ import annotations

from pathlib import Path
import tempfile
from unittest.mock import patch

import pandas as pd
import pytest

from scripts.build_security_master import (
    apply_reviewed_provider_identifier_conflicts,
    apply_reviewed_symbol_transitions,
    _load_delisted_history,
    _load_provider_sources,
    _prepare_research_scope,
    _write_provider_sources,
    _source_failures,
    _write_candidate,
    load_security_master_corrections,
)
from src.data.research_history_policy import load_research_history_policy
from src.data.fmp import infer_us_security_asset_type
from src.data.security_master_store import build_security_master_candidate


def _delisted_page(*rows: tuple[str, str]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "ticker": ticker,
            "name": ticker,
            "exchange": "NASDAQ",
            "ipo_date": "2010-01-01",
            "delisted_date": session,
        }
        for ticker, session in rows
    ])


def test_asset_type_recognizes_compact_nasdaq_preferred_suffix():
    assert infer_us_security_asset_type(
        ticker="OCCIP",
        name="OFS Credit Company, Inc.",
    ) == "PREFERRED"
    assert infer_us_security_asset_type(
        ticker="GOOGL",
        name="Alphabet Inc.",
    ) == "STOCK"
    assert infer_us_security_asset_type(
        ticker="SKYH-WT",
        name="Sky Harbour Group Corporation",
    ) == "WARRANT"
    assert infer_us_security_asset_type(
        ticker="WTGU",
        name="Wintergreen Acquisition Corp.",
    ) == "UNIT"
    assert infer_us_security_asset_type(
        ticker="BPOPN",
        name="Popular Capital Trust I PFD 6.70% GTD",
    ) == "PREFERRED"
    assert infer_us_security_asset_type(
        ticker="DTJ",
        name="DTE Energy Company JR SUB DEB 76",
    ) == "NOTE"
    assert infer_us_security_asset_type(
        ticker="GMTA",
        name="GATX Corporation SR NT 2066",
    ) == "NOTE"
    assert infer_us_security_asset_type(
        ticker="INBKL",
        name="First Internet Bancorp SB NT FXD FLG MA",
    ) == "NOTE"


@patch("scripts.build_security_master.get_delisted_companies")
def test_delisted_loader_proves_history_boundary_without_retaining_older_rows(fetch):
    fetch.side_effect = [
        _delisted_page(("NEW1", "2026-01-02"), ("NEW2", "2025-01-02")),
        _delisted_page(("OLD1", "2018-12-31"), ("OLD2", "2018-01-02")),
    ]

    frame, diagnostics = _load_delisted_history(
        history_start=pd.Timestamp("2019-01-01"),
        target_session=pd.Timestamp("2026-08-11"),
        page_size=2,
        max_pages=5,
    )

    assert frame["ticker"].tolist() == ["NEW1", "NEW2"]
    assert diagnostics["history_boundary_reached"] is True
    assert diagnostics["stop_reason"] == "history_start_reached"
    assert fetch.call_count == 2


@patch("scripts.build_security_master.get_delisted_companies")
def test_delisted_loader_fails_closed_when_page_budget_is_exhausted(fetch):
    fetch.return_value = _delisted_page(
        ("NEW1", "2026-01-02"),
        ("NEW2", "2025-01-02"),
    )

    _, diagnostics = _load_delisted_history(
        history_start=pd.Timestamp("2019-01-01"),
        target_session=pd.Timestamp("2026-08-11"),
        page_size=2,
        max_pages=1,
    )

    assert diagnostics["history_boundary_reached"] is False
    failures = _source_failures(
        profiles=pd.DataFrame({"ticker": ["AAPL"]}),
        changes=pd.DataFrame({"date": [pd.Timestamp("2010-01-01")]}),
        delisted_diagnostics=diagnostics,
        history_start=pd.Timestamp("2019-01-01"),
    )
    assert failures == ["delisted history does not reach history_start"]


def test_research_scope_excludes_global_funds_and_old_irrelevant_profiles():
    profiles = pd.DataFrame([
        {"ticker": "AAPL", "asset_type": "STOCK", "exchange": "NASDAQ", "is_active": True, "listing_date": "1980-12-12"},
        {"ticker": "OLD", "asset_type": "STOCK", "exchange": "NASDAQ", "is_active": False, "listing_date": "2020-01-02"},
        {"ticker": "ANCIENT", "asset_type": "STOCK", "exchange": "NYSE", "is_active": False, "listing_date": "1990-01-02"},
        {"ticker": "QQQ", "asset_type": "ETF", "exchange": "NASDAQ", "is_active": True, "listing_date": "1999-03-10"},
        {"ticker": "GLOBAL", "asset_type": "FUND", "exchange": "LSE", "is_active": True, "listing_date": "2025-01-02"},
        {"ticker": "SPACU", "name": "Example Acquisition Corp Unit", "asset_type": "STOCK", "exchange": "NASDAQ", "is_active": True, "listing_date": "2025-01-02"},
    ])
    profiles["name"] = profiles.get("name", pd.Series(index=profiles.index)).fillna(
        profiles["ticker"]
    )
    changes = pd.DataFrame([{
        "date": "2024-01-02",
        "old_ticker": "OLD",
        "new_ticker": "AAPL",
    }])
    scoped_profiles, scoped_changes, diagnostics = _prepare_research_scope(
        profiles,
        changes,
        _delisted_page(("OLD", "2025-01-02")),
        history_start=pd.Timestamp("2019-01-01"),
        target_session=pd.Timestamp("2026-08-11"),
    )

    assert set(scoped_profiles["ticker"]) == {"AAPL", "OLD", "QQQ"}
    assert len(scoped_changes) == 1
    assert diagnostics["profile_rows_before_scope"] == 6
    assert diagnostics["profile_rows_after_scope"] == 3
    assert diagnostics["excluded_instrument_rows"] == 1


def test_candidate_artifacts_are_immutable_inputs_with_hashable_files():
    profiles = pd.DataFrame([{
        "ticker": "AAPL",
        "name": "Apple",
        "asset_type": "STOCK",
        "exchange": "NASDAQ",
        "country": "US",
        "currency": "USD",
        "cik": "320193",
        "isin": "US0378331005",
        "cusip": "037833100",
        "listing_date": "1980-12-12",
        "sector": "Technology",
        "sub_industry": "Consumer Electronics",
        "trading_status": "ACTIVE",
        "is_active": True,
    }])
    candidate = build_security_master_candidate(
        profiles,
        symbol_changes=pd.DataFrame(),
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-11",
        minimum_active_stocks=1,
    )
    with tempfile.TemporaryDirectory() as temporary:
        destination = Path(temporary) / "candidate"
        artifacts = _write_candidate(candidate, directory=destination)

        assert set(artifacts) == {
            "master", "symbols", "classifications", "identity_keys",
            "history_policy",
        }
        assert all(Path(value["path"]).exists() for value in artifacts.values())
        assert all(len(value["sha256"]) == 64 for value in artifacts.values())


def test_research_history_policy_registry_is_strict_and_hash_bound(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text(
        """schema_version: 1
universe: US_SECURITY_MASTER
activation_session: '2026-08-14'
decision:
  approved_at: '2026-08-16'
  approved_by: project_owner
  basis: FMP cannot prove complete historical coverage
entries:
  - security_id: sec_00000000000000000000000000000000
    current_ticker: TEST
    name: Test Inc.
    trading_status: ACTIVE
    policy: PROSPECTIVE_ONLY
    effective_from: '2026-08-14'
    reason_codes: [FMP_HISTORY_UNVERIFIABLE]
""",
        encoding="utf-8",
    )

    payload, resolved, digest = load_research_history_policy(path)

    assert resolved == path.resolve()
    assert len(digest) == 64
    assert payload["entries"][0]["policy"] == "PROSPECTIVE_ONLY"


def test_provider_sources_require_matching_contract_and_hashes():
    profiles = pd.DataFrame({"ticker": ["AAPL"], "listing_date": ["1980-12-12"]})
    changes = pd.DataFrame({
        "date": ["2024-01-02"],
        "old_ticker": ["OLD"],
        "new_ticker": ["NEW"],
    })
    delisted = _delisted_page(("OLD", "2025-01-02"))
    target = pd.Timestamp("2026-08-11")
    start = pd.Timestamp("2019-01-01")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "run"
        root.mkdir()
        record = _write_provider_sources(
            directory=root,
            profiles=profiles,
            changes=changes,
            delisted=delisted,
            delisted_diagnostics={"history_boundary_reached": True},
            target_session=target,
            history_start=start,
        )

        loaded = _load_provider_sources(
            Path(record["path"]),
            target_session=target,
            history_start=start,
        )
        assert loaded[0]["ticker"].tolist() == ["AAPL"]
        assert loaded[3]["history_boundary_reached"] is True

        path = Path(record["path"]) / "profiles.parquet"
        path.write_bytes(path.read_bytes() + b"tamper")
        try:
            _load_provider_sources(
                Path(record["path"]),
                target_session=target,
                history_start=start,
            )
        except RuntimeError as exc:
            assert "hash verification failed" in str(exc)
        else:
            raise AssertionError("tampered provider source was accepted")


def _reviewed_spac_profiles() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "ticker": "VIACA",
            "name": "Paramount Global",
            "asset_type": "STOCK",
            "exchange": "NASDAQ",
            "country": "US",
            "currency": "USD",
            "cik": "0000813828",
            "isin": "US92556H1077",
            "cusip": "92556H107",
            "listing_date": "2019-12-05",
            "sector": "Communication Services",
            "sub_industry": "Entertainment",
            "trading_status": "INACTIVE",
            "is_active": False,
        },
        {
            "ticker": "PARAA",
            "name": "Paramount Global",
            "asset_type": "STOCK",
            "exchange": "NASDAQ",
            "country": "US",
            "currency": "USD",
            "cik": "0000813828",
            "isin": "US92556H1077",
            "cusip": "92556H107",
            "listing_date": "2019-12-05",
            "sector": "Communication Services",
            "sub_industry": "Entertainment",
            "trading_status": "INACTIVE",
            "is_active": False,
        },
        {
            "ticker": "UCBI",
            "name": "United Community Banks, Inc.",
            "asset_type": "STOCK",
            "exchange": "NASDAQ",
            "country": "US",
            "currency": "USD",
            "cik": "0000857855",
            "isin": "US90984P3038",
            "cusip": "90984P303",
            "listing_date": "2002-03-21",
            "sector": "Financial Services",
            "sub_industry": "Banks",
            "trading_status": "INACTIVE",
            "is_active": False,
        },
        {
            "ticker": "UCB",
            "name": "United Community Banks, Inc.",
            "asset_type": "STOCK",
            "exchange": "NYSE",
            "country": "US",
            "currency": "USD",
            "cik": "0000857855",
            "isin": "US90984P3038",
            "cusip": "90984P303",
            "listing_date": "2002-03-18",
            "sector": "Financial Services",
            "sub_industry": "Banks",
            "trading_status": "ACTIVE",
            "is_active": True,
        },
        {
            "ticker": "HSPT",
            "name": "Horizon Space Acquisition II Corp.",
            "asset_type": "STOCK",
            "exchange": "NASDAQ",
            "country": "US",
            "currency": "USD",
            "cik": "",
            "isin": "KYG8191L1169",
            "cusip": "G8191L116",
            "listing_date": "2025-02-05",
            "sector": "Financial Services",
            "sub_industry": "Shell Companies",
            "trading_status": "INACTIVE",
            "is_active": False,
        },
        {
            "ticker": "SLBT",
            "name": "SL Science Holding Ltd",
            "asset_type": "STOCK",
            "exchange": "NASDAQ",
            "country": "US",
            "currency": "USD",
            "cik": "0002070534",
            "isin": "KYG8191L1169",
            "cusip": "G8191L116",
            "listing_date": "2025-02-05",
            "sector": "Healthcare",
            "sub_industry": "Biotechnology",
            "trading_status": "ACTIVE",
            "is_active": True,
        },
        {
            "ticker": "VACH",
            "name": "Voyager Acquisition Corp.",
            "asset_type": "STOCK",
            "exchange": "NASDAQ",
            "country": "KY",
            "currency": "USD",
            "cik": "",
            "isin": "CH1476899161",
            "cusip": "",
            "listing_date": "2024-09-30",
            "sector": "Financial Services",
            "sub_industry": "Shell Companies",
            "trading_status": "INACTIVE",
            "is_active": False,
        },
        {
            "ticker": "VRXA",
            "name": "Veraxa Biotech Holding AG",
            "asset_type": "STOCK",
            "exchange": "NASDAQ",
            "country": "DE",
            "currency": "USD",
            "cik": "0002079109",
            "isin": "CH1476899161",
            "cusip": "",
            "listing_date": "2026-06-09",
            "sector": "Healthcare",
            "sub_industry": "Biotechnology",
            "trading_status": "ACTIVE",
            "is_active": True,
        },
    ])


def test_reviewed_security_master_transitions_are_source_backed_and_exact():
    registry, path, digest = load_security_master_corrections(
        "configs/security_master_corrections.yaml"
    )
    corrected, audit = apply_reviewed_symbol_transitions(
        _reviewed_spac_profiles(),
        pd.DataFrame(columns=[
            "date", "old_ticker", "new_ticker", "company_name",
        ]),
        registry,
        target_session=pd.Timestamp("2026-08-12"),
    )

    assert path.name == "security_master_corrections.yaml"
    assert len(digest) == 64
    assert set(zip(corrected["old_ticker"], corrected["new_ticker"])) == {
        ("VIACA", "PARAA"),
        ("UCBI", "UCB"),
        ("HSPT", "SLBT"),
        ("VACH", "VRXA"),
    }
    assert set(corrected["date"].dt.date.astype(str)) == {
        "2022-02-17", "2024-08-06", "2026-06-11", "2026-06-15",
    }
    assert {item["action"] for item in audit} == {"REVIEWED_EVENT_ADDED"}
    assert all(item["sources"] for item in audit)

    candidate = build_security_master_candidate(
        _reviewed_spac_profiles(),
        symbol_changes=corrected,
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-12",
        minimum_active_stocks=2,
    )
    assert candidate.quality["status"] == "PASS"
    assert candidate.quality["identity_security_coverage"] == 1.0
    for old_ticker, new_ticker in (
        ("VIACA", "PARAA"),
        ("UCBI", "UCB"),
        ("HSPT", "SLBT"),
        ("VACH", "VRXA"),
    ):
        aliases = candidate.symbols.loc[
            candidate.symbols["ticker"].isin([old_ticker, new_ticker])
        ]
        assert aliases["security_id"].nunique() == 1


def test_reviewed_security_master_transition_fails_on_provider_drift():
    registry, _path, _digest = load_security_master_corrections(
        "configs/security_master_corrections.yaml"
    )
    drifted = _reviewed_spac_profiles()
    drifted.loc[drifted["ticker"].eq("SLBT"), "isin"] = "DRIFTED"
    with pytest.raises(ValueError, match="provider isin drifted"):
        apply_reviewed_symbol_transitions(
            drifted,
            pd.DataFrame(columns=[
                "date", "old_ticker", "new_ticker", "company_name",
            ]),
            registry,
            target_session=pd.Timestamp("2026-08-12"),
        )


def _reviewed_identifier_conflict_profiles() -> pd.DataFrame:
    common = {
        "asset_type": "STOCK",
        "exchange": "NASDAQ",
        "country": "US",
        "currency": "USD",
        "cik": "0001907223",
        "listing_date": "2022-04-29",
        "sector": "Basic Materials",
        "sub_industry": "Other Precious Metals",
    }
    return pd.DataFrame([
        {
            **common,
            "ticker": "KLTO",
            "name": "Klotho Neurosciences, Inc.",
            "isin": "US49876K1034",
            "cusip": "49876K103",
            "trading_status": "INACTIVE",
            "is_active": False,
        },
        {
            **common,
            "ticker": "GRML",
            "name": "Greenland Mines Ltd.",
            "isin": "US49876K2024",
            "cusip": "49876K202",
            "trading_status": "ACTIVE",
            "is_active": True,
        },
    ])


def _reviewed_identifier_conflict_event() -> pd.DataFrame:
    return pd.DataFrame([{
        "date": "2026-03-12",
        "old_ticker": "KLTO",
        "new_ticker": "GRML",
        "company_name": "Greenland Mines Ltd. Common Stock",
    }])


def test_sec_reviewed_identifier_conflict_preserves_stable_security_id():
    registry, _path, _digest = load_security_master_corrections(
        "configs/security_master_corrections.yaml"
    )
    current_profiles = _reviewed_identifier_conflict_profiles()
    changes = _reviewed_identifier_conflict_event()
    reviewed_edges, audit = apply_reviewed_provider_identifier_conflicts(
        current_profiles,
        changes,
        registry,
        target_session=pd.Timestamp("2026-08-24"),
    )
    assert reviewed_edges == {
        ("KLTO", "GRML", pd.Timestamp("2026-03-12"))
    }
    assert audit[0]["action"] == "SEC_CONTINUITY_APPROVED"
    assert audit[0]["provider_values_preserved"] is True

    previous_profiles = current_profiles.copy()
    previous_profiles.loc[
        previous_profiles["ticker"].eq("GRML"), ["cusip", "isin"]
    ] = ["49876K103", "US49876K1034"]
    previous = build_security_master_candidate(
        previous_profiles,
        symbol_changes=changes,
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-21",
        minimum_active_stocks=1,
    )
    previous_row = previous.master.loc[
        previous.master["current_ticker"].eq("GRML")
    ].iloc[0]
    security_id = str(previous_row["security_id"])

    current = build_security_master_candidate(
        current_profiles,
        symbol_changes=changes,
        delisted_companies=pd.DataFrame(),
        target_session="2026-08-24",
        previous_identity_keys=previous.identity_keys,
        previous_master=previous.master,
        previous_symbols=previous.symbols,
        previous_classifications=previous.classifications,
        minimum_active_stocks=1,
        research_history_policy={
            "decision": {"basis": "approved provider limitation"},
            "entries": [{
                "security_id": security_id,
                "current_ticker": "GRML",
                "name": "Greenland Mines Ltd.",
                "trading_status": "ACTIVE",
                "policy": "PROSPECTIVE_ONLY",
                "effective_from": "2026-08-14",
                "reason_codes": ["UNVERIFIABLE_TICKER_INTERVAL"],
            }],
        },
        reviewed_identity_continuity=reviewed_edges,
    )

    assert current.quality["status"] == "PASS"
    assert current.master["security_id"].eq(security_id).sum() == 1
    row = current.master.loc[current.master["security_id"].eq(security_id)].iloc[0]
    assert row["current_ticker"] == "GRML"
    assert row["cusip"] == "49876K202"
    assert set(current.identity_keys.loc[
        current.identity_keys["security_id"].eq(security_id)
        & current.identity_keys["key_type"].eq("CUSIP"),
        "key_value",
    ]) == {"49876K103", "49876K202"}
    assert current.quality["symbol_change_diagnostics"][
        "reviewed_identity_continuity"
    ] == [{
        "old_ticker": "KLTO",
        "new_ticker": "GRML",
        "effective_date": "2026-03-12",
    }]


def test_sec_reviewed_identifier_conflict_fails_on_new_provider_drift():
    registry, _path, _digest = load_security_master_corrections(
        "configs/security_master_corrections.yaml"
    )
    drifted = _reviewed_identifier_conflict_profiles()
    drifted.loc[drifted["ticker"].eq("GRML"), "cusip"] = "DRIFTED"
    with pytest.raises(ValueError, match="GRML provider cusip drifted"):
        apply_reviewed_provider_identifier_conflicts(
            drifted,
            _reviewed_identifier_conflict_event(),
            registry,
            target_session=pd.Timestamp("2026-08-24"),
        )
