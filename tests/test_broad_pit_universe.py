from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile

import pandas as pd
import pytest

from scripts.build_us_liquid_pit import _incremental_inputs_match

from src.data.derived_universe import (
    build_liquid_5m_candidate,
    historical_pit_bar_coverage_check,
    roll_forward_liquid_5m_candidate,
)
from src.data.foundation import (
    DataFoundationError,
    MarketDataCatalog,
    MarketDataReader,
    MarketDataWriter,
)
from src.data.membership_state import resolve_membership_asof
from src.data.security_master_store import SecurityMasterGeneration
from src.data.universe_publication import DerivedUniverseStore


def _sessions(start: str, end: str) -> pd.DatetimeIndex:
    import exchange_calendars as xcals

    values = xcals.get_calendar("XNYS").sessions_in_range(start, end)
    if values.tz is not None:
        values = values.tz_localize(None)
    return pd.DatetimeIndex(values).normalize()


def _master() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "security_id": "sec_aaa",
            "current_ticker": "AAA",
            "asset_type": "STOCK",
            "primary_exchange": "NYSE",
            "listing_date": "2010-01-04",
            "delisting_date": None,
            "trading_status": "ACTIVE",
        },
        {
            "security_id": "sec_ipo",
            "current_ticker": "IPO",
            "asset_type": "STOCK",
            "primary_exchange": "NASDAQ",
            "listing_date": "2020-02-03",
            "delisting_date": None,
            "trading_status": "ACTIVE",
        },
        {
            "security_id": "sec_old",
            "current_ticker": "OLD",
            "asset_type": "STOCK",
            "primary_exchange": "NYSE",
            "listing_date": "2012-01-03",
            "delisting_date": "2020-02-14",
            "trading_status": "INACTIVE",
        },
        {
            "security_id": "sec_adr",
            "current_ticker": "ADR",
            "asset_type": "ADR",
            "primary_exchange": "NASDAQ",
            "listing_date": "2011-01-03",
            "delisting_date": None,
            "trading_status": "ACTIVE",
        },
        {
            "security_id": "sec_pref",
            "current_ticker": "PREF-PA",
            "asset_type": "PREFERRED",
            "primary_exchange": "NYSE",
            "listing_date": "2011-01-03",
            "delisting_date": None,
            "trading_status": "ACTIVE",
        },
    ])


def _bars() -> pd.DataFrame:
    sessions = _sessions("2019-11-01", "2020-03-31")
    rows: list[dict] = []
    for security_id, ticker, first, last in (
        ("sec_aaa", "AAA", sessions.min(), sessions.max()),
        ("sec_ipo", "IPO", pd.Timestamp("2020-02-03"), sessions.max()),
        ("sec_old", "OLD", sessions.min(), pd.Timestamp("2020-02-14")),
        ("sec_adr", "ADR", sessions.min(), sessions.max()),
        ("sec_pref", "PREF-PA", sessions.min(), sessions.max()),
    ):
        for session in sessions[(sessions >= first) & (sessions <= last)]:
            rows.append({
                "date": session,
                "security_id": security_id,
                "ticker": ticker,
                "close": 10.0,
                "unadjusted_close": 10.0,
                "volume": 1_000_000.0,
            })
    return pd.DataFrame(rows)


def test_liquid_membership_is_month_end_pit_and_forces_delisting_exit():
    candidate = build_liquid_5m_candidate(
        _bars(),
        _master(),
        parent_version_id="coverage-v1",
        target_session="2020-03-31",
        history_start="2019-11-01",
        research_start="2020-01-01",
    )

    assert candidate.passed
    membership = candidate.membership
    december = membership.loc[membership["date"].eq("2019-12-31")]
    assert set(december["ticker"]) == {"AAA", "OLD"}
    assert "IPO" not in set(membership.loc[membership["date"].lt("2020-02-03"), "ticker"])
    # Listing is a Security Master/coverage event, not an immediate universe
    # admission.  The IPO enters only at the next month-end reconstitution
    # after it has accumulated the required valid ADV observations.
    february_month_end = pd.Timestamp("2020-02-28")
    assert "IPO" not in set(
        membership.loc[
            membership["date"].between("2020-02-03", "2020-02-27"),
            "ticker",
        ]
    )
    assert "IPO" in set(
        membership.loc[membership["date"].eq(february_month_end), "ticker"]
    )
    assert "ADR" not in set(membership["ticker"])
    assert "PREF-PA" not in set(membership["ticker"])

    forced_date = _sessions("2020-02-15", "2020-02-20")[0]
    forced = membership.loc[membership["date"].eq(forced_date)]
    assert len(forced) == 1
    assert set(forced["ticker"]) == {"OLD"}
    assert not forced["active"].any()
    assert set(forced["snapshot_type"]) == {"FORCED_EXIT"}
    state = resolve_membership_asof(membership, forced_date)
    assert "OLD" not in set(state["ticker"])
    assert len(membership) < 20


def test_month_end_snapshot_is_rebuilt_exactly_from_eligibility_audit():
    candidate = build_liquid_5m_candidate(
        _bars(),
        _master(),
        parent_version_id="coverage-v1",
        target_session="2020-03-31",
        history_start="2019-11-01",
        research_start="2020-01-01",
    )
    monthly = candidate.membership.loc[
        candidate.membership["snapshot_type"].eq("MONTH_END")
    ][["date", "security_id"]]
    expected = candidate.eligibility.loc[
        candidate.eligibility["snapshot_type"].eq("MONTH_END")
        & candidate.eligibility["eligible"]
    ][["date", "security_id"]]
    pd.testing.assert_frame_equal(
        monthly.sort_values(["date", "security_id"]).reset_index(drop=True),
        expected.sort_values(["date", "security_id"]).reset_index(drop=True),
    )


def test_midmonth_roll_forward_without_events_retains_the_baseline():
    bars = _bars()
    common = dict(history_start="2019-11-01", research_start="2020-01-01")
    previous = build_liquid_5m_candidate(
        bars, _master(), parent_version_id="old", target_session="2020-02-20", **common)
    rolled = roll_forward_liquid_5m_candidate(
        previous.membership, previous.eligibility, _master(),
        parent_version_id="new", previous_target_session="2020-02-20",
        target_session="2020-02-21", refresh_start="2020-02-19",
        bar_loader=lambda start, end: bars.loc[bars.date.between(start, end)], **common)
    rebuilt = build_liquid_5m_candidate(
        bars, _master(), parent_version_id="new", target_session="2020-02-21", **common)
    assert rolled.passed
    pd.testing.assert_frame_equal(rolled.membership, rebuilt.membership)
    assert set(resolve_membership_asof(rolled.membership, pd.Timestamp("2020-02-21")).ticker) == {"AAA"}


def test_future_split_cannot_change_historical_nominal_price_eligibility():
    bars = _bars()
    bars.loc[bars.security_id.eq("sec_aaa"), "close"] = 2.
    bars.loc[bars.security_id.eq("sec_aaa"), "unadjusted_close"] = 2.
    bars.loc[bars.security_id.eq("sec_aaa"), "volume"] = 3_000_000.
    common = dict(parent_version_id="test", target_session="2020-03-31",
                  history_start="2019-11-01", research_start="2020-01-01")
    before = build_liquid_5m_candidate(bars, _master(), **common)
    bars.loc[bars.security_id.eq("sec_aaa"), "close"] /= 10
    bars.loc[bars.security_id.eq("sec_aaa"), "volume"] *= 10
    after = build_liquid_5m_candidate(bars, _master(), **common)
    assert "AAA" in set(after.membership.ticker)
    pd.testing.assert_frame_equal(before.membership, after.membership)
    assert after.eligibility.loc[after.eligibility.security_id.eq("sec_aaa"), "selection_price"].eq(2.).all()
    with pytest.raises(DataFoundationError, match="unadjusted_close"):
        build_liquid_5m_candidate(bars.drop(columns="unadjusted_close"), _master(), **common)
    bars.loc[bars.date.eq("2020-01-31"), "unadjusted_close"] = float("nan")
    with pytest.raises(DataFoundationError, match="unadjusted_close"):
        build_liquid_5m_candidate(bars, _master(), **common)


def test_historical_pit_daily_bar_coverage_is_measured_from_latest_snapshot():
    sessions = _sessions("2020-01-02", "2020-01-08")
    membership = pd.DataFrame([
        {
            "date": sessions[0],
            "security_id": security_id,
            "active": True,
        }
        for security_id in ("sec_aaa", "sec_bbb")
    ])
    bars = pd.DataFrame([
        {
            "date": session,
            "security_id": security_id,
            "ticker": security_id,
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 10.0,
            "adj_close": 10.0,
            "volume": 100.0,
        }
        for session in sessions
        for security_id in ("sec_aaa", "sec_bbb")
    ])
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "bars.parquet"
        bars.to_parquet(path, index=False)
        passing, diagnostics = historical_pit_bar_coverage_check(
            membership,
            [path],
            start=sessions.min(),
            end=sessions.max(),
            minimum_coverage=0.95,
        )
        assert passing.passed
        assert diagnostics["minimum_daily_coverage"] == 1.0

        missing = bars.loc[
            ~(
                bars["date"].eq(sessions[2])
                & bars["security_id"].eq("sec_bbb")
            )
        ]
        missing.to_parquet(path, index=False)
        failing, diagnostics = historical_pit_bar_coverage_check(
            membership,
            [path],
            start=sessions.min(),
            end=sessions.max(),
            minimum_coverage=0.95,
        )
        assert not failing.passed
        assert diagnostics["minimum_daily_coverage"] == 0.5
        assert diagnostics["failing_session_count"] == 1


def test_historical_coverage_replays_compact_removal_events():
    sessions = _sessions("2020-01-02", "2020-01-08")
    membership = pd.DataFrame([
        {
            "date": sessions[0],
            "security_id": security_id,
            "active": True,
            "snapshot_type": "MONTH_END",
        }
        for security_id in ("sec_aaa", "sec_bbb")
    ] + [{
        "date": sessions[2],
        "security_id": "sec_bbb",
        "active": False,
        "snapshot_type": "FORCED_EXIT",
    }])
    bars = pd.DataFrame([
        {
            "date": session,
            "security_id": security_id,
        }
        for session in sessions
        for security_id in (
            ("sec_aaa", "sec_bbb")
            if session < sessions[2]
            else ("sec_aaa",)
        )
    ])
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "bars.parquet"
        bars.to_parquet(path, index=False)
        check, diagnostics = historical_pit_bar_coverage_check(
            membership,
            [path],
            start=sessions.min(),
            end=sessions.max(),
            minimum_coverage=0.95,
        )

    assert check.passed
    assert diagnostics["minimum_daily_coverage"] == 1.0


def test_incremental_pit_roll_forward_matches_full_rebuild():
    bars = _bars()
    previous = build_liquid_5m_candidate(
        bars.loc[bars["date"].le("2020-02-28")],
        _master(),
        parent_version_id="coverage-old",
        target_session="2020-02-28",
        history_start="2019-11-01",
        research_start="2020-01-01",
    )

    def load_window(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        return bars.loc[bars["date"].between(start, end)].copy()

    rolled = roll_forward_liquid_5m_candidate(
        previous.membership,
        previous.eligibility,
        _master(),
        parent_version_id="coverage-new",
        previous_target_session="2020-02-28",
        target_session="2020-03-31",
        refresh_start="2020-03-01",
        history_start="2019-11-01",
        research_start="2020-01-01",
        bar_loader=load_window,
    )
    rebuilt = build_liquid_5m_candidate(
        bars,
        _master(),
        parent_version_id="coverage-new",
        target_session="2020-03-31",
        history_start="2019-11-01",
        research_start="2020-01-01",
    )
    assert rolled.passed
    pd.testing.assert_frame_equal(
        rolled.membership.reset_index(drop=True),
        rebuilt.membership.reset_index(drop=True),
        check_dtype=False,
    )
    pd.testing.assert_frame_equal(
        rolled.eligibility.reset_index(drop=True),
        rebuilt.eligibility.reset_index(drop=True),
        check_dtype=False,
    )


def test_incremental_pit_requires_exact_security_master_binding():
    generation = SimpleNamespace(
        generation_id="security-v2",
        manifest_sha256="manifest-v2",
    )
    matching = SimpleNamespace(
        security_master_generation_id="security-v2",
        security_master_manifest_sha256="manifest-v2",
    )
    old_generation = SimpleNamespace(
        security_master_generation_id="security-v1",
        security_master_manifest_sha256="manifest-v1",
    )
    changed_manifest = SimpleNamespace(
        security_master_generation_id="security-v2",
        security_master_manifest_sha256="different-manifest",
    )

    assert _incremental_inputs_match(matching, generation)
    assert not _incremental_inputs_match(old_generation, generation)
    assert not _incremental_inputs_match(changed_manifest, generation)


def _standard_bars(ticker: str, dates: list[str]) -> pd.DataFrame:
    index = pd.to_datetime(dates)
    return pd.DataFrame(
        {
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "adj_close": 10.0,
            "volume": 1_000_000.0,
        },
        index=index,
    )


def test_derived_universe_publication_binds_parent_and_rejects_tampering():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        catalog = MarketDataCatalog(root / "catalog.duckdb")
        writer = MarketDataWriter(
            catalog=catalog,
            lake_dir=root / "market",
            fetcher=lambda ticker, start, end: _standard_bars(
                ticker, ["2020-03-31"]
            ),
            fetcher_semantics_source="TEST_CANONICAL_FIXTURE",
        )
        parent = writer.update_universe(
            "US_EQUITY_COVERAGE",
            target_session="2020-03-31",
            initial_start="2020-03-31",
            universe_frame=pd.DataFrame({"ticker": ["AAA"], "name": ["AAA"]}),
        ).version
        assert parent is not None
        candidate = build_liquid_5m_candidate(
            _bars(),
            _master(),
            parent_version_id=parent.version_id,
            target_session="2020-03-31",
            history_start="2019-11-01",
            research_start="2020-01-01",
        )
        security_generation = SecurityMasterGeneration(
            generation_id="security-v1",
            target_session=date(2020, 3, 31),
            created_at=datetime.now(timezone.utc),
            status="PUBLISHED",
            row_count=5,
            active_count=3,
            master_path="master.parquet",
            symbols_path="symbols.parquet",
            classifications_path="classifications.parquet",
            identity_keys_path="keys.parquet",
            manifest_path="security-manifest.json",
            master_sha256="master",
            symbols_sha256="symbols",
            classifications_sha256="classifications",
            identity_keys_sha256="keys",
            manifest_sha256="security-manifest-sha",
        )
        store = DerivedUniverseStore(
            catalog=catalog,
            snapshot_root=root / "universes",
            market_reader=MarketDataReader(catalog=catalog),
        )
        version = store.publish(
            universe="US_LIQUID_5M",
            parent_version=parent,
            security_master=security_generation,
            membership=candidate.membership,
            eligibility=candidate.eligibility,
            methodology_version="US_LIQUID_5M_PIT_V1",
            checks=list(candidate.checks),
        )

        assert store.require_latest("US_LIQUID_5M") == version
        assert store.load_membership("US_LIQUID_5M")["security_id"].nunique() >= 2
        membership_path = Path(version.membership_path)
        membership_path.write_bytes(membership_path.read_bytes() + b"tampered")
        with pytest.raises(DataFoundationError, match="membership hash"):
            store.require_latest("US_LIQUID_5M")
