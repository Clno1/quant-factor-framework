import pandas as pd
import pytest

from src.data.foundation import DataFoundationError, MarketDataCatalog, MarketDataReader
from src.data.broad_coverage import BroadCoverageStore
from src.data.security_availability import (
    REASON, build_availability, validate_availability, unavailable_ids,
    verify_consumer_availability,
)
from tests.test_broad_coverage import _bars, _security_generation, _price_semantics


def universe(n=1000):
    return pd.DataFrame({"security_id": [f"s{i}" for i in range(n)],
                         "ticker": [f"T{i}" for i in range(n)],
                         "name": [f"Test {i}" for i in range(n)],
                         "is_current_coverage": True, "coverage_role": "EQUITY",
                         "delisting_date": pd.NaT, "asset_type": "STOCK"})


def error(sid="s0", ticker="T0"):
    return {"security_id": sid, "ticker": ticker, "error_code": REASON,
            "missing_dates": ["2020-03-30"], "error": "missing history",
            "isolation_evidence": {"path": "/audit/failure.json", "sha256": "a" * 64,
                "record": {"error_code": REASON, "missing_dates": ["2020-03-30"],
                           "contract": {"security_id": sid}, "raw_artifacts": [{"sha256": "b" * 64}]}}}


def contract(scope=None, errors=None, **kwargs):
    return build_availability(universe() if scope is None else scope,
        target_session="2020-03-31", errors=[error()] if errors is None else errors,
        parent_version_id="parent", parent_manifest_sha256="c" * 64, **kwargs)


@pytest.mark.parametrize("n,count,allowed", [(999, 1, False), (1000, 1, True),
    (5295, 3, True), (5295, 6, False), (20000, 10, True), (20000, 11, False)])
def test_ratio_and_absolute_limits_are_both_enforced(n, count, allowed):
    scope = universe(n)
    errors = [error(f"s{i}", f"T{i}") for i in range(count)]
    if allowed:
        result = contract(scope, errors)
        assert result["expected_count"] == n
        assert len(validate_availability(result, scope)) == count
    else:
        with pytest.raises(DataFoundationError, match="budget"):
            contract(scope, errors)


@pytest.mark.parametrize("change", ["unknown", "evidence", "benchmark", "duplicate", "scope", "ratio", "count"])
def test_nonlocal_failures_or_weakened_policy_cannot_be_isolated(change):
    scope, errors, kwargs = universe(), [error()], {}
    if change == "unknown":
        errors[0].pop("error_code")
    elif change == "evidence":
        errors[0]["isolation_evidence"] = None
    elif change == "benchmark":
        scope.loc[0, "ticker"] = errors[0]["ticker"] = "SPY"
    elif change == "duplicate":
        scope.loc[1, "security_id"] = "s0"
    elif change == "scope":
        errors[0]["security_id"] = "foreign"
    elif change == "ratio":
        kwargs["max_ratio"] = .01
    else:
        kwargs["max_count"] = 11
    with pytest.raises(DataFoundationError):
        contract(scope, errors, **kwargs)


def test_isolation_persists_without_expiry_and_restoration_is_explicit():
    prior = contract()
    current = build_availability(universe(), target_session="2020-04-30", errors=[], previous=prior,
        parent_version_id="new-parent", parent_manifest_sha256="d" * 64)
    assert current["unavailable"] == prior["unavailable"]
    assert current["status"] == "DEGRADED"
    restored = contract(errors=[], previous=current, restored_ids=["s0"])
    assert restored["status"] == "READY" and restored["unavailable"] == []
    assert restored["restored_security_ids"] == ["s0"]


def test_reader_rejects_contract_tampering_and_shrunken_denominator():
    value = contract()
    with pytest.raises(DataFoundationError, match="scope"):
        validate_availability(value, universe().iloc[1:])
    value["unavailable"] = []
    with pytest.raises(DataFoundationError, match="hash"):
        validate_availability(value, universe())


def test_consumer_must_bind_exact_isolation_ledger():
    value = contract()
    parent = {"quality_lineage": {"security_availability": value}}
    verify_consumer_availability(parent, {"security_availability": value})
    with pytest.raises(DataFoundationError, match="contract mismatch"):
        verify_consumer_availability(parent, {})
    verify_consumer_availability({}, {})


def test_degraded_publication_keeps_denominator_and_bad_security_has_no_bars(tmp_path):
    scope = universe()
    available = pd.concat([_bars(f"s{i}", f"T{i}", ["2020-03-31"]) for i in range(1, 1000)])
    catalog = MarketDataCatalog(tmp_path / "catalog.duckdb")
    store = BroadCoverageStore(catalog=catalog, lake_dir=tmp_path / "lake")
    publication = store.publish_frames([available], security_universe=scope,
        target_session="2020-03-31", security_master=_security_generation(), price_semantics=_price_semantics(),
        quality_lineage={"security_availability": contract(scope)})
    assert publication.statistics["current_security_count"] == 1000
    assert publication.statistics["target_covered_count"] == 999
    assert publication.statistics["target_coverage"] == .999
    assert publication.statistics["availability_status"] == "DEGRADED"
    reader = MarketDataReader(catalog=catalog)
    manifest = reader.verify_version(publication.version)
    assert manifest["availability_status"] == "DEGRADED"
    assert len(reader.load_universe("US_EQUITY_COVERAGE", current_only=False, version=publication.version)) == 1000
    from types import SimpleNamespace as NS
    from src.data.universe_publication import DerivedUniverseStore
    from src.breakouts.broad_daily_data import _contract
    pit = DerivedUniverseStore(catalog=catalog, snapshot_root=tmp_path / "pit", market_reader=reader)
    members = pd.DataFrame([{"date": "2020-03-31", "security_id": "s1", "ticker": "T1", "active": True,
        "selection_price": 10., "adv20_usd": 10_000_000., "valid_sessions_20d": 20,
        "asset_type_pass": True, "price_pass": True, "liquidity_pass": True, "reason_codes": "ELIGIBLE",
        "snapshot_type": "MONTH_END", "source_data_version_id": publication.version.version_id}])
    eligibility = pd.DataFrame([{"date": "2020-03-31", "security_id": "s0", "ticker": "T0", "eligible": False,
        "reason_codes": "UPSTREAM_SECURITY_ISOLATED", "source_data_version_id": publication.version.version_id}])
    options = dict(universe="US_LIQUID_5M", parent_version=publication.version,
        security_master=_security_generation(), membership=members, eligibility=eligibility,
        methodology_version="US_LIQUID_5M_PIT_V3_NOMINAL_PRICE", checks=[])
    pit_version = pit.publish(**options)
    assert pit.verify(pit_version)["security_availability"] == contract(scope)
    candidate_contract = _contract(requested_universe="US_ACTIVE", parent=publication.version,
        parent_manifest=manifest, universe_version=pit_version, coverage=NS(to_dict=lambda: {"passed": True}))
    assert candidate_contract.to_dict()["coverage"]["security_availability"]["expected_count"] == 1000
    assert candidate_contract.to_dict()["coverage"]["security_availability"]["unavailable"][0]["security_id"] == "s0"
    with pytest.raises(DataFoundationError, match="cannot enter PIT"):
        pit.publish(**{**options, "membership": members.assign(security_id="s0", ticker="T0")})
    with pytest.raises(DataFoundationError, match="cannot omit"):
        pit.publish(**{**options, "eligibility": eligibility.iloc[:0]})
    with pytest.raises(DataFoundationError, match="isolated_security_bar_absence"):
        store.publish_frames([available, _bars("s0", "T0", ["2020-03-30"])], security_universe=scope,
            target_session="2020-03-31", security_master=_security_generation(), price_semantics=_price_semantics(),
            quality_lineage={"security_availability": contract(scope)})
    scope.loc[0, "ticker"] = "SPY"
    with pytest.raises(DataFoundationError, match="required_benchmark_target_presence"):
        store.publish_frames([available], security_universe=scope,
            target_session="2020-03-31", security_master=_security_generation(), price_semantics=_price_semantics())


def test_missing_history_has_structured_code_but_generic_errors_do_not(tmp_path):
    from src.data.broad_history_repair import collect_replacements
    from src.data.security_availability import MissingAuthenticatedHistory
    def replace(sid, _):
        if sid == "s0":
            exc = MissingAuthenticatedHistory(sid, ["2020-03-30"])
            exc.evidence = error()["isolation_evidence"]
            raise exc
        raise DataFoundationError("price semantics or identity mismatch")
    _, report = collect_replacements(failures=[{"security_id": f"s{i}", "ticker": f"T{i}"} for i in range(2)],
        load_previous=lambda ids: pd.DataFrame({"security_id": ids}), replace_security=replace,
        report_path=tmp_path / "repair.json", contract={})
    assert report["errors"][0]["error_code"] == REASON
    assert "error_code" not in report["errors"][1]
    with pytest.raises(DataFoundationError, match="non-isolatable"):
        contract(errors=report["errors"])


def test_writer_isolates_persists_and_only_restores_with_complete_history(tmp_path, monkeypatch):
    from dataclasses import replace
    from datetime import date
    from types import SimpleNamespace as NS
    from scripts import update_us_equity_coverage as writer
    from src.data.broad_coverage import BroadCoverageReader
    scope = universe()
    symbols = scope[["security_id", "ticker"]].assign(effective_from="2024-01-30", effective_to=None)
    generation = replace(_security_generation(), target_session=date(2024, 2, 6))
    catalog = MarketDataCatalog(tmp_path / "catalog.duckdb")
    store = BroadCoverageStore(catalog=catalog, lake_dir=tmp_path / "lake")
    def bars(dates):
        return pd.concat([scope[["security_id", "ticker"]].assign(date=pd.Timestamp(d),
            open=10., high=11., low=9., close=10., adj_close=10., volume=1_000_000., unadjusted_close=10.)
            for d in dates], ignore_index=True)
    parent = store.publish_frames([bars(["2024-01-30", "2024-01-31", "2024-02-01"])],
        security_universe=scope, target_session="2024-02-01", security_master=generation,
        price_semantics=_price_semantics()).version
    config = NS(data=NS(foundation=NS(catalog_path=str(catalog.path), lake_dir=str(tmp_path / "lake")),
        security_master=NS(snapshot_dir=str(tmp_path / "master")),
        broad_coverage=NS(history_start="2024-01-30", allowed_asset_types=["STOCK"], benchmark_tickers=[],
            max_bar_quarantine_ratio=.0005, max_target_bar_quarantine_ratio=.005, min_target_coverage=.98,
            security_isolation_max_ratio=.001, security_isolation_max_count=10),
        fmp=NS(bulk_request_interval_seconds=0.)), abs_path=lambda p:p)
    monkeypatch.setattr(writer, "CONFIG", config)
    monkeypatch.setattr(writer, "load_repair_rules", lambda *a, **kw: ([], None, {"fixture": True}))
    monkeypatch.setattr(writer.SecurityMasterStore, "load_published", lambda _: (generation, {"master": scope, "symbols": symbols}))
    monkeypatch.setattr(writer, "select_coverage_securities", lambda *a, **kw: scope)
    def identity_delta(**kw):
        assert kw["security_ids"] == []
        return pd.DataFrame(), [], [], True
    monkeypatch.setattr(writer, "_load_or_fetch_history_delta", identity_delta)
    def bulk(**kw):
        frame = bars([kw["session"]])
        if str(kw["session"].date()) == "2024-01-30":
            frame.loc[frame.security_id.eq("s0"), "volume"] = 999900
        return frame.drop(columns="security_id"), True, {}
    monkeypatch.setattr(writer, "_load_or_fetch_eod_bulk_session", bulk)
    complete, requests = False, []
    def full(ticker, start, end):
        requests.append(ticker)
        assert ticker == "T0"
        frame = bars(pd.bdate_range(start, end))
        frame = frame.loc[frame.security_id.eq("s0")].copy()
        if not complete:
            frame = frame.loc[frame.date.ne(pd.Timestamp("2024-01-30"))]
        return frame.set_index("date").drop(columns=["security_id", "ticker"])
    monkeypatch.setattr(writer, "get_coverage_historical_ohlcv", full)
    args = NS(target_session="2024-02-02", overlap_calendar_days=2,
        output_dir=str(tmp_path / "incremental"), publish=False, repair_only=True, repair_full_history=True)
    prepared, code = writer.run(args)
    assert code == 0 and prepared["status"] == "PREPARED_DEGRADED"
    assert catalog.latest_version("US_EQUITY_COVERAGE").version_id == parent.version_id
    import json
    from pathlib import Path
    args.expected_scope_sha256 = json.loads(Path(prepared["report_path"]).read_text())["contract"]["scope_sha256"]
    args.publish, args.repair_only, args.repair_cache_only = True, False, True
    first, code = writer.run(args)
    assert code == 0 and first["status"] == "PUBLISHED_DEGRADED"
    assert first["security_availability"]["expected_count"] == 1000
    assert first["security_availability"]["unavailable"][0]["last_good_version_id"] == parent.version_id
    reader = MarketDataReader(catalog=catalog)
    version = reader.require_latest("US_EQUITY_COVERAGE")
    assert BroadCoverageReader(market_reader=reader).load_bars(security_ids=["s0"], version=version).empty
    assert len(reader.load_universe("US_EQUITY_COVERAGE", current_only=False, version=version)) == 1000
    args.target_session = "2024-02-05"
    args.repair_cache_only, args.expected_scope_sha256 = False, None
    second, code = writer.run(args)
    assert code == 0 and second["status"] == "PUBLISHED_DEGRADED"
    assert requests == ["T0"]
    assert second["security_availability"]["unavailable"] == first["security_availability"]["unavailable"]
    complete = True
    args.restore_isolated_security = ["s0"]
    final, code = writer.run(args)
    assert code == 0 and final["status"] == "PUBLISHED"
    assert final["security_availability"]["unavailable"] == []
    version = reader.require_latest("US_EQUITY_COVERAGE")
    restored = BroadCoverageReader(market_reader=reader).load_bars(security_ids=["s0"], version=version)
    assert set(restored.date) == set(pd.bdate_range("2024-01-30", "2024-02-05"))
