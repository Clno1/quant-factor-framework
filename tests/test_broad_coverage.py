from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import tempfile

import pandas as pd
import pytest

from src.data.broad_coverage import (
    BroadCoverageReader,
    BroadCoverageStore,
    COVERAGE_PARTITION_FREQUENCY,
    coverage_bar_quarantine_checks,
    coverage_alias_intervals,
    fetch_coverage_history_delta,
    map_eod_bulk_to_security_ids,
    normalize_coverage_bars,
    select_coverage_securities,
    split_coverage_bar_quality,
)
from src.data.foundation import DataFoundationError, MarketDataCatalog, MarketDataReader
from src.data.price_semantics import build_price_semantics_contract
from src.data.security_master_store import SecurityMasterGeneration


def _price_semantics() -> dict:
    return build_price_semantics_contract(
        source="TEST_CANONICAL_FIXTURE",
        history_mode="FULL_REBUILD",
    )


def _security_generation() -> SecurityMasterGeneration:
    return SecurityMasterGeneration(
        generation_id="security-v1",
        target_session=date(2020, 3, 31),
        created_at=datetime.now(timezone.utc),
        status="PUBLISHED",
        row_count=3,
        active_count=2,
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


def _universe() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "security_id": "sec_aaa",
            "current_ticker": "AAA",
            "ticker": "AAA",
            "name": "Alpha",
            "asset_type": "STOCK",
            "primary_exchange": "NYSE",
            "listing_date": pd.Timestamp("2010-01-04"),
            "delisting_date": pd.NaT,
            "trading_status": "ACTIVE",
            "coverage_role": "EQUITY",
            "is_current_coverage": True,
        },
        {
            "security_id": "sec_bbb",
            "current_ticker": "BBB",
            "ticker": "BBB",
            "name": "Beta",
            "asset_type": "STOCK",
            "primary_exchange": "NASDAQ",
            "listing_date": pd.Timestamp("2012-01-03"),
            "delisting_date": pd.NaT,
            "trading_status": "ACTIVE",
            "coverage_role": "EQUITY",
            "is_current_coverage": True,
        },
        {
            "security_id": "sec_old",
            "current_ticker": "OLD",
            "ticker": "OLD",
            "name": "Old Co",
            "asset_type": "STOCK",
            "primary_exchange": "NYSE",
            "listing_date": pd.Timestamp("2011-01-03"),
            "delisting_date": pd.Timestamp("2020-03-30"),
            "trading_status": "INACTIVE",
            "coverage_role": "EQUITY",
            "is_current_coverage": False,
        },
    ])


def _bars(security_id: str, ticker: str, dates: list[str]) -> pd.DataFrame:
    rows = []
    for value in dates:
        rows.append({
            "date": value,
            "security_id": security_id,
            "ticker": ticker,
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "adj_close": 10.0,
            "volume": 1_000_000.0,
        })
    return pd.DataFrame(rows)


@pytest.mark.parametrize("price_scale, volume_scale, adjusted_scale", [(.5, 2., .5), (1., 1., .9)])
def test_incremental_reconciliation_preserves_total_return_across_months(
    tmp_path, price_scale, volume_scale, adjusted_scale,
):
    from scripts.update_us_equity_coverage import _coverage_rebase_audit, _rebase_coverage_partition
    from src.data.price_semantics import PriceSemantics

    old = _bars("sec_aaa", "OLD_ALIAS", ["2024-01-30", "2024-01-31"])
    old["unadjusted_close"] = old["close"]
    fresh = _bars("sec_aaa", "AAA", ["2024-01-30", "2024-01-31", "2024-02-01"])
    fresh.loc[:, ["open", "high", "low", "close"]] *= price_scale
    fresh["adj_close"] *= adjusted_scale
    fresh["volume"] *= volume_scale
    old["date"] = pd.to_datetime(old.date)
    fresh["date"] = pd.to_datetime(fresh.date)
    audit = _coverage_rebase_audit(old, fresh, parent_security_ids={"sec_aaa"})
    # Model separate old/new monthly partitions; both must share new units.
    january = _rebase_coverage_partition(old, audit)
    pd.testing.assert_series_equal(january.unadjusted_close, old.unadjusted_close)
    assert (january.close * january.volume).to_list() == pytest.approx((old.close * old.volume).to_list())
    february = fresh.loc[fresh.date.eq("2024-02-01")].copy()
    paths = []
    for name, frame in (("jan", january), ("feb", february)):
        path = tmp_path / f"{name}.parquet"
        normalize_coverage_bars(frame, target_session="2024-02-01", ingestion_run_id="new").to_parquet(path, index=False)
        paths.append(path)
    catalog = MarketDataCatalog(tmp_path / "catalog.duckdb")
    publication = BroadCoverageStore(catalog=catalog, lake_dir=tmp_path / "lake").publish_partitions(
        paths, security_universe=_universe().iloc[:1], target_session="2024-02-01",
        security_master=_security_generation(), price_semantics=_price_semantics())
    loaded = BroadCoverageReader(market_reader=MarketDataReader(catalog=catalog)).load_bars(version=publication.version)
    wide = {c: loaded.pivot(index="date", columns="security_id", values=c) for c in ("open", "close", "adj_close", "volume")}
    assert PriceSemantics.from_wide(wide).total_returns.iloc[-1, 0] == pytest.approx(0.)


def test_incremental_reconciliation_rejects_missing_and_inconsistent_anchors():
    from scripts.update_us_equity_coverage import _coverage_rebase_audit
    old = _bars("sec_aaa", "AAA", ["2024-01-30", "2024-01-31"])
    fresh = _bars("sec_aaa", "AAA", ["2024-02-01"])
    with pytest.raises(DataFoundationError, match="overlap anchor"):
        _coverage_rebase_audit(old, fresh, parent_security_ids={"sec_aaa"})
    fresh = old.copy()
    fresh.loc[0, "adj_close"] *= .5
    with pytest.raises(DataFoundationError, match="non-uniform"):
        _coverage_rebase_audit(old, fresh, parent_security_ids={"sec_aaa"})


def test_new_month_end_fetches_nominal_prices_without_rewriting_return_prices():
    from scripts.update_us_equity_coverage import _attach_month_end_nominal_close
    bars = _bars("sec_aaa", "AAA", ["2024-01-30", "2024-01-31", "2024-02-01"])
    bars["date"] = pd.to_datetime(bars.date)
    bars["unadjusted_close"] = float("nan")
    calls = []
    def fetcher(symbol, start, end):
        calls.append((symbol, start, end))
        return pd.Series([100.], index=pd.to_datetime(["2024-01-31"]))
    result = _attach_month_end_nominal_close(bars, after=pd.Timestamp("2024-01-29"), target=pd.Timestamp("2024-02-01"), fetcher=fetcher)
    assert calls == [("AAA", "2024-01-31", "2024-01-31")]
    assert result.loc[result.date.eq("2024-01-31"), "unadjusted_close"].iloc[0] == 100.
    pd.testing.assert_series_equal(result.close, bars.close)


def test_coverage_selection_keeps_adr_but_excludes_non_equity_instruments():
    master = pd.DataFrame([
        {
            "security_id": f"sec_{ticker}",
            "current_ticker": ticker,
            "name": ticker,
            "asset_type": asset_type,
            "primary_exchange": "NASDAQ",
            "listing_date": "2019-01-02",
            "delisting_date": None,
            "trading_status": "ACTIVE",
        }
        for ticker, asset_type in (
            ("AAA", "STOCK"),
            ("ADR", "ADR"),
            ("PREF-PA", "PREFERRED"),
            ("QQQ", "ETF"),
        )
    ])
    selected = select_coverage_securities(
        master,
        history_start="2019-01-01",
        target_session="2020-03-31",
        history_policy=pd.DataFrame([{
            "security_id": "sec_AAA",
            "policy": "PROSPECTIVE_ONLY",
            "effective_from": "2020-03-30",
        }]),
    )
    assert set(selected["ticker"]) == {"AAA", "ADR", "QQQ"}
    assert selected.set_index("ticker").loc["QQQ", "coverage_role"] == "BENCHMARK_ONLY"


def test_alias_intervals_are_clipped_to_history_and_target():
    universe = _universe().iloc[[0]].copy()
    symbols = pd.DataFrame([
        {
            "security_id": "sec_aaa",
            "ticker": "OLDNAME",
            "effective_from": "2010-01-04",
            "effective_to": "2020-01-31",
        },
        {
            "security_id": "sec_aaa",
            "ticker": "AAA",
            "effective_from": "2020-02-03",
            "effective_to": None,
        },
    ])
    aliases = coverage_alias_intervals(
        universe,
        symbols,
        history_start="2019-01-01",
        target_session="2020-03-31",
    )
    assert aliases[["ticker", "fetch_start", "fetch_end"]].to_dict("records") == [
        {
            "ticker": "OLDNAME",
            "fetch_start": pd.Timestamp("2019-01-02"),
            "fetch_end": pd.Timestamp("2020-01-31"),
        },
        {
            "ticker": "AAA",
            "fetch_start": pd.Timestamp("2020-02-03"),
            "fetch_end": pd.Timestamp("2020-03-31"),
        },
    ]


def test_history_delta_fetches_every_dated_alias_and_preserves_identity():
    universe = _universe().iloc[[0]].copy()
    symbols = pd.DataFrame([
        {
            "security_id": "sec_aaa",
            "ticker": "OLDNAME",
            "effective_from": "2019-01-01",
            "effective_to": "2020-02-28",
        },
        {
            "security_id": "sec_aaa",
            "ticker": "AAA",
            "effective_from": "2020-03-01",
            "effective_to": None,
        },
    ])
    calls = []

    def fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
        calls.append((ticker, start, end))
        return pd.DataFrame(
            {
                "open": [10.0],
                "high": [11.0],
                "low": [9.0],
                "close": [10.0],
                "adj_close": [10.0],
                "volume": [1_000_000.0],
            },
            index=pd.DatetimeIndex([start], name="date"),
        )

    bars, failures, fallbacks = fetch_coverage_history_delta(
        universe,
        symbols,
        security_ids=["sec_aaa"],
        history_start="2020-02-01",
        target_session="2020-03-31",
        fetcher=fetcher,
    )

    assert [call[0] for call in calls] == ["OLDNAME", "AAA"]
    assert set(bars["security_id"]) == {"sec_aaa"}
    assert bars["ticker"].tolist() == ["OLDNAME", "AAA"]
    assert failures == []
    assert fallbacks == []


def test_history_policy_excludes_unverifiable_and_clips_prospective_start():
    master = _universe().copy()
    policy = pd.DataFrame([
        {
            "security_id": "sec_aaa",
            "policy": "PROSPECTIVE_ONLY",
            "effective_from": "2020-03-30",
        },
        {
            "security_id": "sec_old",
            "policy": "EXCLUDED_UNVERIFIABLE_HISTORY",
            "effective_from": "2020-03-30",
        },
    ])
    selected = select_coverage_securities(
        master,
        history_start="2019-01-01",
        target_session="2020-03-31",
        history_policy=policy,
    )
    assert set(selected["security_id"]) == {"sec_aaa", "sec_bbb"}
    prospective = selected.set_index("security_id").loc["sec_aaa"]
    assert prospective["research_history_policy"] == "PROSPECTIVE_ONLY"
    assert prospective["coverage_start"] == pd.Timestamp("2020-03-30")

    symbols = pd.DataFrame([
        {
            "security_id": "sec_aaa",
            "ticker": "AAA",
            "effective_from": "2010-01-04",
            "effective_to": None,
        },
        {
            "security_id": "sec_bbb",
            "ticker": "BBB",
            "effective_from": "2012-01-03",
            "effective_to": None,
        },
    ])
    aliases = coverage_alias_intervals(
        selected,
        symbols,
        history_start="2019-01-01",
        target_session="2020-03-31",
    )
    aaa = aliases.loc[aliases["security_id"].eq("sec_aaa")].iloc[0]
    assert aaa["fetch_start"] == pd.Timestamp("2020-03-30")


def test_alias_interval_without_an_xnys_session_is_not_requested():
    universe = _universe().iloc[[0]].copy()
    symbols = pd.DataFrame([
        {
            "security_id": "sec_aaa",
            "ticker": "HOLIDAY",
            "effective_from": "2019-01-01",
            "effective_to": "2019-01-01",
        },
        {
            "security_id": "sec_aaa",
            "ticker": "AAA",
            "effective_from": "2019-01-02",
            "effective_to": None,
        },
    ])

    aliases = coverage_alias_intervals(
        universe,
        symbols,
        history_start="2019-01-01",
        target_session="2019-01-04",
    )

    assert aliases[["ticker", "fetch_start", "fetch_end"]].to_dict("records") == [{
        "ticker": "AAA",
        "fetch_start": pd.Timestamp("2019-01-02"),
        "fetch_end": pd.Timestamp("2019-01-04"),
    }]


def test_bulk_eod_mapping_uses_dated_symbol_identity_and_rejects_ambiguity():
    universe = pd.DataFrame([
        {"security_id": "sec_old", "ticker": "NEW"},
        {"security_id": "sec_reuse", "ticker": "OLD"},
    ])
    symbols = pd.DataFrame([
        {
            "security_id": "sec_old",
            "ticker": "OLD",
            "effective_from": "2019-01-01",
            "effective_to": "2020-01-31",
        },
        {
            "security_id": "sec_old",
            "ticker": "NEW",
            "effective_from": "2020-02-01",
            "effective_to": None,
        },
        {
            "security_id": "sec_reuse",
            "ticker": "OLD",
            "effective_from": "2021-01-01",
            "effective_to": None,
        },
    ])
    bulk = pd.DataFrame([
        {
            "date": date_value,
            "ticker": ticker,
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "adj_close": 10.0,
            "volume": 100.0,
        }
        for date_value, ticker in (
            ("2020-01-31", "OLD"),
            ("2020-02-03", "NEW"),
            ("2021-01-04", "OLD"),
        )
    ])
    mapped = map_eod_bulk_to_security_ids(bulk, symbols, universe)
    assert mapped["security_id"].tolist() == ["sec_old", "sec_old", "sec_reuse"]

    symbols.loc[2, "effective_from"] = "2020-01-01"
    with pytest.raises(DataFoundationError, match="multiple security_ids"):
        map_eod_bulk_to_security_ids(bulk.iloc[[0]], symbols, universe)


def test_provider_bad_bars_are_quarantined_with_deterministic_reasons():
    source = normalize_coverage_bars(
        pd.concat([
            _bars("sec_aaa", "AAA", ["2020-03-30", "2020-03-31"]),
            _bars("sec_bbb", "BBB", ["2020-03-31"]),
        ], ignore_index=True),
        target_session="2020-03-31",
        ingestion_run_id="quality-test",
    )
    source.loc[source["date"].eq(pd.Timestamp("2020-03-30")), "adj_close"] = 0.0
    source.loc[source["security_id"].eq("sec_bbb"), "low"] = 10.5

    accepted, quarantine = split_coverage_bar_quality(source)

    assert len(accepted) == 1
    assert quarantine[["security_id", "quality_reasons"]].to_dict("records") == [
        {"security_id": "sec_aaa", "quality_reasons": "NONPOSITIVE_PRICE"},
        {"security_id": "sec_bbb", "quality_reasons": "INVALID_OHLC_BOUNDS"},
    ]
    checks = coverage_bar_quarantine_checks(
        quarantine,
        source_row_count=len(source),
        security_universe=_universe(),
        target_session="2020-03-31",
        max_ratio=1.0,
        max_target_ratio=1.0,
    )
    assert all(check.passed for check in checks)


def test_provider_off_session_bars_are_quarantined_and_cannot_publish():
    source = normalize_coverage_bars(
        pd.concat([
            _bars("sec_aaa", "AAA", ["2020-03-29", "2020-03-30", "2020-03-31"]),
            _bars("sec_bbb", "BBB", ["2020-03-30", "2020-03-31"]),
            _bars("sec_old", "OLD", ["2020-03-30"]),
        ], ignore_index=True),
        target_session="2020-03-31",
        ingestion_run_id="calendar-quality-test",
    )

    accepted, quarantine = split_coverage_bar_quality(source)

    assert set(accepted["date"].dt.date.astype(str)) == {"2020-03-30", "2020-03-31"}
    assert quarantine[["date", "security_id", "quality_reasons"]].to_dict("records") == [{
        "date": pd.Timestamp("2020-03-29"),
        "security_id": "sec_aaa",
        "quality_reasons": "NON_XNYS_SESSION",
    }]
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = BroadCoverageStore(
            catalog=MarketDataCatalog(root / "catalog.duckdb"),
            lake_dir=root / "lake",
        )
        with pytest.raises(DataFoundationError, match="xnys_session_calendar"):
            store.publish_frames(
                [source],
                security_universe=_universe(),
                target_session="2020-03-31",
                security_master=_security_generation(),
                price_semantics=_price_semantics(),
                min_target_coverage=1.0,
            )


def test_published_quarantine_is_authenticated_by_the_manifest():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        catalog = MarketDataCatalog(root / "catalog.duckdb")
        store = BroadCoverageStore(catalog=catalog, lake_dir=root / "lake")
        source = normalize_coverage_bars(
            pd.concat([
                _bars("sec_aaa", "AAA", ["2020-03-30", "2020-03-31"]),
                _bars("sec_bbb", "BBB", ["2020-03-31"]),
                _bars("sec_old", "OLD", ["2020-03-30"]),
            ], ignore_index=True),
            target_session="2020-03-31",
            ingestion_run_id="quality-test",
        )
        source.loc[
            source["date"].eq(pd.Timestamp("2020-03-30"))
            & source["security_id"].eq("sec_aaa"),
            "adj_close",
        ] = 0.0
        accepted, quarantine = split_coverage_bar_quality(source)
        quarantine_path = root / "bar_quarantine.parquet"
        quarantine.to_parquet(quarantine_path, index=False)
        publication = store.publish_frames(
            [accepted],
            security_universe=_universe(),
            target_session="2020-03-31",
            security_master=_security_generation(),
            price_semantics=_price_semantics(),
            min_target_coverage=1.0,
            bar_quarantine_path=quarantine_path,
            quality_lineage={"policy": "PROVIDER_BAD_BAR_QUARANTINE_V1"},
        )
        reader = MarketDataReader(catalog=catalog)
        manifest = reader.verify_version(publication.version)
        published_quarantine = (
            Path(publication.version.manifest_path).parent
            / manifest["bar_quarantine_path"]
        )
        assert manifest["bar_quarantine_rows"] == 1
        published_quarantine.write_bytes(
            published_quarantine.read_bytes() + b"tampered"
        )
        with pytest.raises(DataFoundationError, match="checksum mismatch"):
            reader.verify_version(publication.version)


def test_partitioned_coverage_is_readable_and_child_hashes_are_enforced():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        catalog = MarketDataCatalog(root / "catalog.duckdb")
        store = BroadCoverageStore(catalog=catalog, lake_dir=root / "lake")
        publication = store.publish_frames(
            [
                pd.concat([
                    _bars("sec_aaa", "AAA", ["2020-03-30", "2020-03-31"]),
                    _bars("sec_bbb", "BBB", ["2020-03-30", "2020-03-31"]),
                ], ignore_index=True),
                _bars("sec_old", "OLD", ["2020-03-30"]),
            ],
            security_universe=_universe(),
            target_session="2020-03-31",
            security_master=_security_generation(),
            price_semantics=_price_semantics(),
            min_target_coverage=1.0,
        )
        reader = MarketDataReader(catalog=catalog)
        assert reader.require_latest("US_EQUITY_COVERAGE") == publication.version
        manifest = reader.verify_version(
            publication.version,
            require_price_semantics=True,
        )
        assert manifest["schema_version"] == 5
        assert manifest["price_semantics"]["history_mode"] == "FULL_REBUILD"
        assert manifest["price_semantics_parent_version_id"] is None
        broad = BroadCoverageReader(market_reader=reader)
        rows = broad.load_bars(
            security_ids=["sec_aaa"],
            start="2020-03-31",
            end="2020-03-31",
        )
        assert rows[["security_id", "ticker"]].to_dict("records") == [
            {"security_id": "sec_aaa", "ticker": "AAA"}
        ]
        unordered = broad.load_bars(
            start="2020-03-30",
            end="2020-03-31",
            columns=["date", "security_id", "ticker", "adj_close"],
            ordered=False,
        )
        assert set(unordered["security_id"]) == {
            "sec_aaa", "sec_bbb", "sec_old"
        }
        assert len(unordered) == 5
        generic = reader.load_bars(
            "US_EQUITY_COVERAGE",
            tickers=["BBB"],
            start="2020-03-31",
        )
        assert generic["ticker"].tolist() == ["BBB"]
        with pytest.raises(DataFoundationError, match="unrestricted Pandas wide"):
            reader.load_wide_tables("US_EQUITY_COVERAGE")
        subset_wide = reader.load_wide_tables(
            "US_EQUITY_COVERAGE", tickers=["AAA"]
        )
        assert list(subset_wide["close"].columns) == ["AAA"]

        index = Path(publication.version.bars_path)
        payload = __import__("json").loads(index.read_text(encoding="utf-8"))
        child = index.parent / payload["partitions"][0]["file"]
        child.write_bytes(child.read_bytes() + b"tampered")
        # Metadata-only web reads authenticate the immutable partition index
        # without re-hashing the entire historical lake on every request.
        assert reader.require_latest(
            "US_EQUITY_COVERAGE",
            verify_partition_children=False,
        ) == publication.version
        # Production and shadow checks keep the full default verification and
        # therefore still detect any changed child immediately.
        with pytest.raises(DataFoundationError, match="checksum mismatch"):
            reader.require_latest("US_EQUITY_COVERAGE")


def test_incremental_coverage_authenticates_price_semantics_parent():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        catalog = MarketDataCatalog(root / "catalog.duckdb")
        store = BroadCoverageStore(catalog=catalog, lake_dir=root / "lake")
        parent = store.publish_frames(
            [
                pd.concat([
                    _bars("sec_aaa", "AAA", ["2020-03-30", "2020-03-31"]),
                    _bars("sec_bbb", "BBB", ["2020-03-30", "2020-03-31"]),
                    _bars("sec_old", "OLD", ["2020-03-30"]),
                ], ignore_index=True)
            ],
            security_universe=_universe(),
            target_session="2020-03-31",
            security_master=_security_generation(),
            min_target_coverage=1.0,
        )
        contract = build_price_semantics_contract(
            source="TEST_INCREMENTAL_PROVIDER",
            history_mode="INCREMENTAL_FROM_AUTHENTICATED_PARENT",
        )
        child = store.publish_frames(
            [
                pd.concat([
                    _bars("sec_aaa", "AAA", ["2020-03-30", "2020-03-31"]),
                    _bars("sec_bbb", "BBB", ["2020-03-30", "2020-03-31"]),
                    _bars("sec_old", "OLD", ["2020-03-30"]),
                ], ignore_index=True)
            ],
            security_universe=_universe(),
            target_session="2020-03-31",
            security_master=_security_generation(),
            price_semantics=contract,
            price_semantics_parent_version_id=parent.version.version_id,
            quality_lineage={
                "parent_dataset_version_id": parent.version.version_id,
            },
            min_target_coverage=1.0,
        )
        manifest = MarketDataReader(catalog=catalog).verify_version(
            child.version,
            require_price_semantics=True,
        )
        assert manifest["price_semantics"] == contract
        assert (
            manifest["price_semantics_parent_version_id"]
            == parent.version.version_id
        )

        with pytest.raises(DataFoundationError, match="quality lineage parent"):
            store.publish_frames(
                [_bars("sec_aaa", "AAA", ["2020-03-31"])],
                security_universe=_universe().iloc[[0]].copy(),
                target_session="2020-03-31",
                security_master=_security_generation(),
                price_semantics=contract,
                price_semantics_parent_version_id="wrong-parent",
                quality_lineage={
                    "parent_dataset_version_id": parent.version.version_id,
                },
                min_target_coverage=1.0,
            )
def test_published_coverage_compacts_candidate_batches_into_month_partitions():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        catalog = MarketDataCatalog(root / "catalog.duckdb")
        store = BroadCoverageStore(catalog=catalog, lake_dir=root / "lake")
        publication = store.publish_frames(
            [
                pd.concat([
                    _bars("sec_aaa", "AAA", ["2020-02-28", "2020-03-30"]),
                    _bars("sec_bbb", "BBB", ["2020-02-28", "2020-03-30"]),
                ], ignore_index=True),
                _bars("sec_old", "OLD", ["2020-02-28", "2020-03-30"]),
            ],
            security_universe=_universe(),
            target_session="2020-03-30",
            security_master=_security_generation(),
            price_semantics=_price_semantics(),
            min_target_coverage=1.0,
        )

        index = Path(publication.version.bars_path)
        payload = __import__("json").loads(index.read_text(encoding="utf-8"))
        assert payload["partition_frequency"] == COVERAGE_PARTITION_FREQUENCY
        assert [
            (entry["year"], entry["month"])
            for entry in payload["partitions"]
        ] == [(2020, 2), (2020, 3)]
        for entry in payload["partitions"]:
            frame = pd.read_parquet(index.parent / entry["file"])
            assert frame["date"].dt.to_period("M").nunique() == 1


def test_partition_validation_detects_duplicate_keys_across_input_files():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        catalog = MarketDataCatalog(root / "catalog.duckdb")
        store = BroadCoverageStore(catalog=catalog, lake_dir=root / "lake")
        first = root / "first.parquet"
        second = root / "second.parquet"
        normalize_coverage_bars(
            _bars("sec_aaa", "AAA", ["2020-02-28", "2020-03-30"]),
            target_session="2020-03-31",
            ingestion_run_id="first",
        ).to_parquet(first, index=False)
        normalize_coverage_bars(
            pd.concat([
                _bars("sec_aaa", "AAA", ["2020-03-30"]),
                _bars("sec_bbb", "BBB", ["2020-03-31"]),
                _bars("sec_old", "OLD", ["2020-02-28"]),
            ], ignore_index=True),
            target_session="2020-03-31",
            ingestion_run_id="second",
        ).to_parquet(second, index=False)

        checks, statistics = store._validate_partitions(
            [first, second],
            security_universe=_universe(),
            target_session=pd.Timestamp("2020-03-31"),
            min_target_coverage=1.0,
        )

        assert statistics["row_count"] == 5
        assert statistics["duplicate_count"] == 1
        assert not next(
            check for check in checks if check.name == "unique_date_security"
        ).passed


def test_bounded_reader_hashes_selected_month_without_rescanning_other_months():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        catalog = MarketDataCatalog(root / "catalog.duckdb")
        store = BroadCoverageStore(catalog=catalog, lake_dir=root / "lake")
        publication = store.publish_frames(
            [
                pd.concat([
                    _bars("sec_aaa", "AAA", ["2020-02-28", "2020-03-30"]),
                    _bars("sec_bbb", "BBB", ["2020-02-28", "2020-03-30"]),
                    _bars("sec_old", "OLD", ["2020-02-28", "2020-03-30"]),
                ], ignore_index=True),
            ],
            security_universe=_universe(),
            target_session="2020-03-30",
            security_master=_security_generation(),
            price_semantics=_price_semantics(),
            min_target_coverage=1.0,
        )
        reader = MarketDataReader(catalog=catalog)
        index = Path(publication.version.bars_path)
        payload = __import__("json").loads(index.read_text(encoding="utf-8"))
        february = next(
            entry for entry in payload["partitions"] if entry["month"] == 2
        )
        february_path = index.parent / february["file"]
        february_path.write_bytes(february_path.read_bytes() + b"tampered")

        march_paths = reader.partition_paths(
            publication.version,
            start="2020-03-01",
            end="2020-03-31",
        )
        assert len(march_paths) == 1
        assert "month=03" in str(march_paths[0])
        with pytest.raises(DataFoundationError, match="checksum mismatch"):
            reader.partition_paths(
                publication.version,
                start="2020-02-01",
                end="2020-02-29",
            )
        with pytest.raises(DataFoundationError, match="checksum mismatch"):
            reader.verify_version(publication.version)
