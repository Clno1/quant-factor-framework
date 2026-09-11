import pandas as pd
import pytest

from src.data.broad_history_repair import (
    audit_overlap, fetch_replacement, replace_month, verify_repaired_rows, inherit_quarantine,
    refresh_canonical_sources,
    load_repair_rules,
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


def test_next_increment_uses_canonical_source_and_still_authenticates_history(tmp_path):
    kw = args(tmp_path)
    old = bars(); bulk = bars(); bulk.loc[0, "volume"] = 100.01
    mapped, proofs = refresh_canonical_sources(
        mapped=bulk, previous_overlap=old.iloc[:2], security_ids={"a"},
        cache_dir=tmp_path, contract={"parent": "repaired"},
        universe=kw["universe"], symbols=kw["symbols"], refresh_start="2024-01-30",
        target=kw["target"], fetcher=lambda *_: raw())
    assert mapped.volume.eq(100).all()
    passed, failed = audit_overlap(old, mapped, {"a"})
    assert not failed and len(passed) == 1
    assert proofs[0]["bulk_conflict_counts"]["volume"] == 1
    assert proofs[0]["selection_scope"] == "AUTHENTICATED_RECENT_WINDOW_SAME_CANONICAL_SOURCE"
    mapped.loc[0, "volume"] = 99
    assert audit_overlap(old, mapped, {"a"})[1]  # Genuine canonical revision still fails.


def test_retired_canonical_security_does_not_request_outside_alias_window(tmp_path):
    kw = args(tmp_path)
    kw["symbols"]["effective_to"] = "2024-01-31"
    kw["universe"]["delisting_date"] = pd.Timestamp("2024-01-31")
    def forbidden(*_):
        pytest.fail("no approved alias overlaps this refresh window")
    options = dict(security_ids={"a"}, cache_dir=tmp_path, contract={"parent": "v"},
                   universe=kw["universe"], symbols=kw["symbols"],
                   refresh_start="2024-02-01", target=kw["target"], fetcher=forbidden)
    out, proof = refresh_canonical_sources(mapped=bars().iloc[:0],
                                            previous_overlap=bars().iloc[:0], **options)
    assert out.empty and not proof
    with pytest.raises(DataFoundationError, match="outside approved alias window"):
        refresh_canonical_sources(mapped=bars().iloc[-1:],
                                  previous_overlap=bars().iloc[:0], **options)


def test_exact_quarantine_inheritance_preserves_evidence_and_valid_parent(tmp_path):
    from src.data.broad_coverage import split_coverage_bar_quality
    f = bars()
    f.loc[0, "open"] = 12
    clean, approved = split_coverage_bar_quality(f)
    inherited, bad = inherit_quarantine(f, approved=approved, previous=clean)
    pd.testing.assert_frame_equal(inherited, clean)
    assert len(bad) == 1 and bad.iloc[0].open == 12
    with pytest.raises(DataFoundationError, match="authenticated parent"):
        inherit_quarantine(f, approved=approved, previous=bars())
    f.loc[0, "volume"] += .01
    with pytest.raises(DataFoundationError, match="exactly match"):
        inherit_quarantine(f, approved=approved, previous=clean)


def test_prior_quarantine_is_evidence_not_legacy_price_authorization(tmp_path):
    import yaml
    from types import SimpleNamespace
    from src.data.broad_history_repair import file_sha256
    from src.data.broad_coverage import split_coverage_bar_quality
    f = bars(); f.loc[0, "high"] = 1
    _, bad = split_coverage_bar_quality(f)
    bad.to_parquet(tmp_path / "bad.parquet", index=False)
    source = {"version_id": "v", "manifest_sha256": "m", "quarantine_sha256": file_sha256(tmp_path / "bad.parquet"),
              "rows": [{"security_id": "a", "ticker": "A", "date": "2024-01-30"}]}
    policy = tmp_path / "rules.yaml"
    policy.write_text(yaml.safe_dump({"schema_version": 1, "query_mappings": [], "prior_quarantine": source}))
    version = SimpleNamespace(manifest_checksum_sha256="m", manifest_path=str(tmp_path / "manifest.json"))
    catalog = SimpleNamespace(get_version=lambda *a, **kw: version)
    calls = []
    def verify(selected, *, require_price_semantics):
        calls.append(require_price_semantics)
        return {"bar_quarantine_path": "bad.parquet", "bar_quarantine_sha256": source["quarantine_sha256"]}
    reader = SimpleNamespace(verify_version=verify)
    _, selected, _ = load_repair_rules(policy, catalog=catalog, market_reader=reader)
    assert len(selected) == 1 and calls == [False]
    version.manifest_checksum_sha256 = "changed"
    with pytest.raises(DataFoundationError, match="manifest mismatch"):
        load_repair_rules(policy, catalog=catalog, market_reader=reader)


def test_quarantine_survives_cache_and_hash_is_checked(tmp_path):
    from src.data.broad_coverage import split_coverage_bar_quality
    f = bars(); f.loc[0, "high"] = 1
    clean, approved = split_coverage_bar_quality(f)
    kw = args(tmp_path)
    kw.update(previous=clean.iloc[:1], recent=clean, approved_quarantine=approved,
              rules_contract={"source_hash": "fixture"})
    fetch = lambda *_: f.set_index("date").drop(columns=["security_id", "ticker"])
    path, proof = fetch_replacement(**kw, fetcher=fetch)
    assert proof["quarantined_rows"] == 1 and len(pd.read_parquet(path)) == 2
    _, proof2 = fetch_replacement(**kw, fetcher=fetch)
    assert proof2["cache_hit"] and proof2["quarantined_rows"] == 1
    from pathlib import Path
    Path(proof["quarantine_path"]).write_bytes(b"bad")
    with pytest.raises(DataFoundationError, match="quarantine cache hash"):
        fetch_replacement(**kw, fetcher=fetch)


def test_policy_revalidation_reuses_raw_bytes_not_old_pass_or_fail(tmp_path):
    from src.data.broad_coverage import split_coverage_bar_quality
    f = bars(); f.loc[0, "high"] = 1
    clean, approved = split_coverage_bar_quality(f)
    kw = args(tmp_path)
    kw.update(previous=clean, recent=clean, rules_contract={"policy": "old"})
    fetch = lambda *_: f.set_index("date").drop(columns=["security_id", "ticker"])
    with pytest.raises(DataFoundationError, match="without reviewed quarantine"):
        fetch_replacement(**kw, fetcher=fetch)
    failure = next(tmp_path.rglob("failure.json"))
    failure_bytes = failure.read_bytes()
    def forbidden(*_):
        pytest.fail("must use the authenticated frozen response")
    kw.update(approved_quarantine=approved, rules_contract={"policy": "reviewed"}, reuse_frozen_inputs=True)
    path, proof = fetch_replacement(**kw, fetcher=forbidden)
    assert len(pd.read_parquet(path)) == 2 and proof["raw_inputs_reused"]
    assert proof["frozen_source_proof"]["record_path"] == str(failure)
    assert failure.read_bytes() == failure_bytes
    _, cached = fetch_replacement(**kw, fetcher=forbidden)
    assert cached["cache_hit"] and cached["raw_inputs_reused"]
    kw["rules_contract"] = {"policy": "not_matching"}
    kw["approved_quarantine"] = approved.assign(volume=12345.)
    with pytest.raises(DataFoundationError, match="exactly match"):
        fetch_replacement(**kw, fetcher=forbidden)


def test_frozen_raw_reuse_requires_exact_input_contract_and_hash(tmp_path):
    import json
    from pathlib import Path
    kw = args(tmp_path)
    kw["rules_contract"] = {"policy": "one"}
    _, proof = fetch_replacement(**kw, fetcher=lambda *_: raw())
    kw.update(rules_contract={"policy": "two"}, reuse_frozen_inputs=True)
    calls = []
    kw["contract"] = {"parent": "different"}
    fetch_replacement(**kw, fetcher=lambda *a: calls.append(a) or raw())
    assert len(calls) == 1  # A different parent cannot borrow the frozen source.
    kw["contract"] = args(tmp_path)["contract"]
    manifest = Path(proof["manifest_path"])
    meta = json.loads(manifest.read_text())
    (manifest.parent / meta["raw_artifacts"][0]["path"]).write_bytes(b"damaged")
    with pytest.raises(DataFoundationError, match="raw input hash mismatch"):
        fetch_replacement(**kw, fetcher=lambda *_: pytest.fail("must fail closed"))


def test_different_frozen_responses_are_ambiguous_not_silently_selected(tmp_path):
    f = raw(); f.iloc[0, f.columns.get_loc("high")] = 1
    kw = args(tmp_path)
    for volume in [100., 101.]:
        with pytest.raises(DataFoundationError, match="without reviewed quarantine"):
            fetch_replacement(**kw, fetcher=lambda *_: f.assign(volume=volume))
    with pytest.raises(DataFoundationError, match="multiple different frozen"):
        fetch_replacement(**kw, reuse_frozen_inputs=True, fetcher=lambda *_: pytest.fail("must fail closed"))


def test_bounded_query_mapping_preserves_historical_identity(tmp_path):
    kw = args(tmp_path)
    kw["symbols"] = pd.DataFrame([
        {"security_id": "a", "ticker": "OLD", "effective_from": "2024-01-30", "effective_to": "2024-01-31"},
        {"security_id": "a", "ticker": "A", "effective_from": "2024-02-01", "effective_to": None}])
    kw["query_mappings"] = [{"security_id": "a", "historical_ticker": "OLD", "query_ticker": "A",
                             "start": "2024-01-31", "end": "2024-01-31", "next_alias_start": "2024-02-01"}]
    calls = []
    def fetch(t, start, end):
        calls.append((t, start, end)); return raw().loc[start:end]
    path, proof = fetch_replacement(**kw, fetcher=fetch)
    assert calls == [("OLD", "2024-01-30", "2024-01-30"), ("A", "2024-01-31", "2024-01-31"),
                     ("A", "2024-02-01", "2024-02-01")]
    assert pd.read_parquet(path).ticker.tolist() == ["OLD", "OLD", "A"]
    kw["symbols"].loc[0, "effective_to"] = "2024-02-01"
    with pytest.raises(DataFoundationError, match="alias contract drifted"):
        fetch_replacement(**kw, fetcher=fetch)


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
    monkeypatch.setattr(writer, "load_repair_rules", lambda *a, **kw: ([], None, {"fixture": True}))
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

    # The next ordinary update must retain the canonical source without another
    # full-history repair, while still authenticating the overlap exactly.
    generation = replace(generation, target_session=date(2024, 2, 5))
    calls = []
    def canonical_increment(ticker, start, end):
        calls.append((ticker, start, end))
        f = _bars("sec_aaa", ticker, dates + ["2024-02-02", "2024-02-05"])
        f["volume"] = 800000.
        f["unadjusted_close"] = f.close
        f.date = pd.to_datetime(f.date)
        return f.set_index("date").loc[start:end].drop(columns=["security_id", "ticker"])
    monkeypatch.setattr(writer, "get_coverage_historical_ohlcv", canonical_increment)
    args.target_session = "2024-02-05"
    args.repair_full_history = False
    updated, code = writer.run(args)
    assert code == 0 and updated["publication"]["version_id"] != report["publication"]["version_id"]
    assert calls == [("AAA", "2024-01-31", "2024-02-05")]
    reader = MarketDataReader(catalog=catalog)
    manifest = reader.verify_version(catalog.latest_version("US_EQUITY_COVERAGE"))
    lineage = manifest["quality_lineage"]
    assert lineage["canonical_history_security_ids"] == ["sec_aaa"]
    assert lineage["full_security_history_repair"]["security_count"] == 0
    assert lineage["canonical_overlap_refresh"][0]["bulk_conflict_counts"]["volume"] > 0
    final = BroadCoverageReader(market_reader=reader).load_bars()
    assert final.loc[final.security_id.eq("sec_aaa"), "volume"].tolist() == [800000.] * 5
    assert final.loc[final.security_id.eq("sec_bbb"), "volume"].tolist() == [1000000.] * 5
