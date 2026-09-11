import pandas as pd
import pytest

from src.data.broad_history_repair import (
    audit_overlap, fetch_replacement, replace_month, verify_repaired_rows,
)
from src.data.broad_coverage import normalize_coverage_bars
from src.data.foundation import DataFoundationError


def bars(sid="a", dates=("2024-01-30", "2024-01-31", "2024-02-01")):
    return normalize_coverage_bars(pd.DataFrame({
        "date": pd.to_datetime(dates), "security_id": sid, "ticker": sid.upper(),
        "open": 10., "high": 11., "low": 9., "close": 10.,
        "adj_close": 10., "volume": 100., "unadjusted_close": 10.,
    }), target_session="2024-02-01", ingestion_run_id="fixture")


def test_scope_collects_all_errors_and_does_not_weaken_authentication():
    old = pd.concat([bars(s) for s in ("a", "b", "c", "d")], ignore_index=True)
    new = old.copy()
    new.loc[0, "volume"] = 100.01
    new.loc[3, "close"] = 10.1
    new.loc[6, "volume"] = -1
    passed, failures = audit_overlap(old, new, {"a", "b", "c", "d"})
    assert [v["security_id"] for v in passed] == ["d"]
    assert [v["security_id"] for v in failures] == ["a", "b", "c"]
    assert [v["recoverable"] for v in failures] == [True, True, False]


def test_repaired_history_forces_pit_rebuild_even_without_master_change():
    from types import SimpleNamespace
    from scripts.build_us_liquid_pit import _incremental_inputs_match
    previous = SimpleNamespace(security_master_generation_id="same", security_master_manifest_sha256="hash")
    generation = SimpleNamespace(generation_id="same", manifest_sha256="hash")
    assert _incremental_inputs_match(previous, generation, {})
    assert not _incremental_inputs_match(previous, generation, {
        "quality_lineage": {"full_security_history_repair": {"security_count": 1}}
    })


def args(tmp_path):
    return dict(
        cache_dir=tmp_path, contract={"parent": "v1", "security_master": "s1"},
        security_id="a", universe=pd.DataFrame([{
            "security_id": "a", "current_ticker": "A", "listing_date": pd.Timestamp("2024-01-30"),
            "delisting_date": pd.NaT, "coverage_start": pd.Timestamp("2024-01-30"),
        }]),
        symbols=pd.DataFrame([{"security_id": "a", "ticker": "A",
                               "effective_from": "2024-01-30", "effective_to": None}]),
        previous=bars().iloc[:2], recent=bars().iloc[1:],
        history_start="2024-01-30", target=pd.Timestamp("2024-02-01"),
    )


def raw():
    return bars().set_index("date").drop(columns=["security_id", "ticker"])


def test_full_fetch_binding_cache_exact_dates_and_bulk_conflict_audit(tmp_path):
    calls = []
    def fetch(ticker, start, end):
        calls.append((ticker, start, end))
        f = raw()
        f["volume"] = 99.8
        return f
    kw = args(tmp_path)
    path, proof = fetch_replacement(**kw, fetcher=fetch)
    assert calls == [("A", "2024-01-30", "2024-02-01")]
    assert proof["rows"] == 3 and proof["bulk_conflict_counts"]["volume"] == 2
    _, cached = fetch_replacement(**kw, fetcher=fetch)
    assert cached["cache_hit"] and len(calls) == 1
    kw["contract"] = {"parent": "v2", "security_master": "s1"}
    path2, _ = fetch_replacement(**kw, fetcher=fetch)
    assert path2 != path and len(calls) == 2
    path2.write_bytes(path2.read_bytes() + b"corruption")
    with pytest.raises(DataFoundationError, match="hash mismatch"):
        fetch_replacement(**kw, fetcher=fetch)


@pytest.mark.parametrize("damage, message", [
    ("truncate", "loses"), ("future", "outside"), ("invalid", "invalid bars"),
    ("duplicate", "duplicate"), ("nominal", "nominal"), ("empty", "empty full"),
    ("holiday", "outside"),
])
def test_bad_replacement_preserves_failure_evidence_and_never_completes_cache(tmp_path, damage, message):
    def fetch(*_):
        f = raw()
        if damage == "truncate": f = f.iloc[1:]
        if damage == "future": f.index = pd.to_datetime(["2024-01-30", "2024-01-31", "2024-02-02"])
        if damage == "invalid": f.iloc[0, f.columns.get_loc("high")] = 1
        if damage == "duplicate": f = pd.concat([f, f.iloc[:1]])
        if damage == "nominal": f["unadjusted_close"] = float("nan")
        if damage == "empty": return None
        if damage == "holiday": f.index = pd.to_datetime(["2024-01-28", "2024-01-31", "2024-02-01"])
        f.index.name = "date"
        return f
    with pytest.raises(DataFoundationError, match=message):
        fetch_replacement(**args(tmp_path), fetcher=fetch)
    assert not list(tmp_path.rglob("manifest.json"))
    assert list(tmp_path.rglob("failure.json"))


def test_each_alias_is_requested_without_fallback(tmp_path):
    kw = args(tmp_path)
    kw["symbols"] = pd.DataFrame([
        {"security_id": "a", "ticker": "OLD", "effective_from": "2024-01-30", "effective_to": "2024-01-30"},
        {"security_id": "a", "ticker": "A", "effective_from": "2024-01-31", "effective_to": None},
    ])
    calls = []
    def fetch(ticker, start, end):
        calls.append((ticker, start, end))
        return raw().loc[start:end]
    path, _ = fetch_replacement(**kw, fetcher=fetch)
    assert calls == [("OLD", "2024-01-30", "2024-01-30"), ("A", "2024-01-31", "2024-02-01")]
    assert pd.read_parquet(path).ticker.tolist() == ["OLD", "A", "A"]


def test_provider_contract_error_is_recorded_as_failed_not_successful_cache(tmp_path):
    def fetch(*_):
        raise ValueError("nominal endpoint does not cover canonical dates")
    with pytest.raises(DataFoundationError, match="provider contract failed"):
        fetch_replacement(**args(tmp_path), fetcher=fetch)
    assert list(tmp_path.rglob("failure.json"))
    assert not list(tmp_path.rglob("manifest.json"))


def test_replace_all_months_no_old_prefix_and_exact_validation():
    old = pd.concat([bars(), bars("b")], ignore_index=True)
    delta = old.iloc[[1, 2]].copy()
    fresh = bars()
    fresh["volume"] = 91.25
    for period in ("2024-01", "2024-02"):
        take = lambda f: f.loc[f.date.dt.to_period("M").astype(str).eq(period)]
        expected = take(fresh)
        actual = replace_month(take(old), take(delta), expected, {"a"})
        verify_repaired_rows(actual, expected, {"a"})
        pd.testing.assert_frame_equal(actual.loc[actual.security_id.eq("b")].reset_index(drop=True),
                                      take(old.loc[old.security_id.eq("b")]).reset_index(drop=True))
        actual.loc[actual.security_id.eq("a"), "volume"] = 100
        with pytest.raises(DataFoundationError, match="exactly"):
            verify_repaired_rows(actual, expected, {"a"})


def test_failed_final_publication_or_stale_parent_never_moves_pointer(tmp_path):
    from src.data.broad_coverage import BroadCoverageStore
    from src.data.foundation import MarketDataCatalog, QualityCheck
    from test_broad_coverage import _security_generation, _universe, _bars, _price_semantics
    catalog = MarketDataCatalog(tmp_path / "catalog.duckdb")
    store = BroadCoverageStore(catalog=catalog, lake_dir=tmp_path / "lake")
    options = dict(security_universe=_universe().iloc[:1], target_session="2024-02-01",
                   security_master=_security_generation(), price_semantics=_price_semantics())
    parent = store.publish_frames([_bars("sec_aaa", "AAA", ["2024-02-01"])], **options).version
    with pytest.raises(DataFoundationError, match="candidate rejected"):
        store.publish_frames([_bars("sec_aaa", "AAA", ["2024-02-01"])], **options,
                             external_checks=[QualityCheck("injected", False, 1, 0, "test failure")])
    assert catalog.latest_version("US_EQUITY_COVERAGE").version_id == parent.version_id
    with pytest.raises(DataFoundationError, match="parent changed"):
        store.publish_frames([_bars("sec_aaa", "AAA", ["2024-02-01"])], **options,
                             expected_current_version_id="stale")
    assert catalog.latest_version("US_EQUITY_COVERAGE").version_id == parent.version_id


@pytest.mark.parametrize("truncate", [False, True])
def test_writer_end_to_end_full_history_or_no_publication(tmp_path, monkeypatch, truncate):
    from dataclasses import replace
    from datetime import date
    from types import SimpleNamespace as NS
    from scripts import update_us_equity_coverage as writer
    from src.data.broad_coverage import BroadCoverageStore, BroadCoverageReader
    from src.data.foundation import MarketDataCatalog, MarketDataReader
    from test_broad_coverage import _security_generation, _universe, _bars, _price_semantics
    catalog = MarketDataCatalog(tmp_path / "catalog.duckdb")
    store = BroadCoverageStore(catalog=catalog, lake_dir=tmp_path / "lake")
    universe = _universe().iloc[:2].copy()
    symbols = pd.DataFrame([{"security_id": s, "ticker": t, "effective_from": "2024-01-30",
                             "effective_to": None} for s, t in [("sec_aaa", "AAA"), ("sec_bbb", "BBB")]])
    generation = replace(_security_generation(), target_session=date(2024, 2, 2))
    dates = ["2024-01-30", "2024-01-31", "2024-02-01"]
    old = pd.concat([_bars(s, t, dates) for s, t in [("sec_aaa", "AAA"), ("sec_bbb", "BBB")]])
    old["unadjusted_close"] = old.close
    parent = store.publish_frames([old], security_universe=universe,
                                  target_session="2024-02-01", security_master=generation,
                                  price_semantics=_price_semantics()).version
    config = NS(data=NS(foundation=NS(catalog_path=str(catalog.path), lake_dir=str(tmp_path / "lake")),
                       security_master=NS(snapshot_dir=str(tmp_path / "master")),
                       broad_coverage=NS(history_start="2024-01-30", allowed_asset_types=["STOCK"],
                                         benchmark_tickers=[], max_bar_quarantine_ratio=.001,
                                         max_target_bar_quarantine_ratio=0., min_target_coverage=1.),
                       fmp=NS(bulk_request_interval_seconds=0.)), abs_path=lambda p:p)
    monkeypatch.setattr(writer, "CONFIG", config)
    monkeypatch.setattr(writer.SecurityMasterStore, "load_published", lambda _: (generation, {"master": universe, "symbols": symbols}))
    monkeypatch.setattr(writer, "select_coverage_securities", lambda *a, **kw: universe)
    monkeypatch.setattr(writer, "_load_or_fetch_history_delta", lambda **kw: (pd.DataFrame(), [], [], True))
    def bulk(**kw):
        d = str(kw["session"].date())
        f = pd.concat([_bars(s, t, [d]) for s, t in [("sec_aaa", "AAA"), ("sec_bbb", "BBB")]])
        f.date = pd.to_datetime(f.date)
        if d == "2024-01-30": f.loc[f.ticker.eq("AAA"), "volume"] = 999900
        return f.drop(columns=["security_id"]), True, {}
    monkeypatch.setattr(writer, "_load_or_fetch_eod_bulk_session", bulk)
    def full(ticker, start, end):
        assert (ticker, start, end) == ("AAA", "2024-01-30", "2024-02-02")
        f = _bars("sec_aaa", ticker, dates + ["2024-02-02"])
        f["volume"] = 800000.
        f["unadjusted_close"] = f.close
        f.date = pd.to_datetime(f.date)
        return (f.iloc[1:] if truncate else f).set_index("date").drop(columns=["security_id", "ticker"])
    monkeypatch.setattr(writer, "get_coverage_historical_ohlcv", full)
    args = NS(target_session="2024-02-02", overlap_calendar_days=2,
              output_dir=str(tmp_path / "incremental"), publish=True, repair_full_history=True)
    if truncate:
        with pytest.raises(DataFoundationError, match="full-history repair rejected"):
            writer.run(args)
        assert catalog.latest_version("US_EQUITY_COVERAGE").version_id == parent.version_id
        return
    report, code = writer.run(args)
    assert code == 0 and report["publication"]["version_id"] != parent.version_id
    result = BroadCoverageReader(market_reader=MarketDataReader(catalog=catalog)).load_bars()
    assert result.loc[result.security_id.eq("sec_aaa"), "volume"].tolist() == [800000.] * 4
    assert result.loc[result.security_id.eq("sec_bbb"), "volume"].tolist() == [1000000.] * 4
    original = BroadCoverageReader(market_reader=MarketDataReader(catalog=catalog)).load_bars(version=parent)
    assert original.volume.eq(1000000).all()
