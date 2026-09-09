from datetime import datetime, timezone
import json

import pandas as pd
import pytest

from scripts.update_industry_risk import publish_snapshot
from src.risk.industry import build_industry_report, load_snapshot, TAXONOMY


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def fixture():
    account = {"id": "a", "name": "test", "last_equity": 100, "cash": 20, "last_mark_date": "2026-09-04", "industry_benchmark": "SPY"}
    positions = pd.DataFrame([{"ticker": "A", "market_value": 60}, {"ticker": "B", "market_value": 20}])
    snapshot = {"schema_version": 1, "observed_at": NOW.isoformat(), "taxonomy": TAXONOMY, "classifications": {"A": {"sector": "Technology"}, "B": {"sector": "Industrials"}}, "benchmarks": {"SPY": {"taxonomy": TAXONOMY, "observed_at": NOW.isoformat(), "source": "fixture", "weights": {"Technology": .3, "Industrials": .2, "Energy": .5}}}}
    return account, positions, snapshot


def test_nav_cash_equity_denominators_and_benchmark_only_sector():
    a, p, s = fixture()
    r = build_industry_report(a, p, s, now=NOW)
    rows = {r["sector"]: r for r in r["rows"]}
    assert r["status"] == "READY"
    assert rows["Technology"]["nav_weight"] == .6
    assert rows["Technology"]["equity_weight"] == .75
    assert rows["Technology"]["active_weight_pp"] == 30
    assert rows["Energy"]["active_weight_pp"] == -50
    assert rows["CASH"]["nav_weight"] == .2
    assert sum(x["nav_weight"] for x in r["rows"]) == pytest.approx(1)


def test_unknown_portfolio_preserved_and_no_false_active_weights():
    a, p, s = fixture()
    del s["classifications"]["B"]
    r = build_industry_report(a, p, s, now=NOW)
    assert r["unknown_weight"] == .2
    assert not r["comparable"]
    assert all(x["active_weight_pp"] is None for x in r["rows"])


@pytest.mark.parametrize("change,issue", [
    (lambda a, s: a.update(last_mark_date="2026-08-01"), "STALE_VALUATION"),
    (lambda a, s: s["benchmarks"]["SPY"].update(taxonomy="GICS"), "BENCHMARK_TAXONOMY_MISMATCH"),
    (lambda a, s: s.update(observed_at="2026-09-20"), "STALE_CLASSIFICATION"),
    (lambda a, s: s["benchmarks"]["SPY"].update(weights={"Technology": .3}), "BENCHMARK_WEIGHT_TOTAL_INVALID"),
    (lambda a, s: s.update(benchmarks={}), "BENCHMARK_SNAPSHOT_MISSING"),
])
def test_incomparable_inputs_never_produce_active_weights(change, issue):
    a, p, s = fixture()
    change(a, s)
    r = build_industry_report(a, p, s, now=NOW)
    assert issue in r["issues"]
    assert not r["comparable"]


def test_missing_benchmark_does_not_become_zero_weight_benchmark():
    a, p, s = fixture()
    s["benchmarks"] = {}
    r = build_industry_report(a, p, s, now=NOW)
    assert all(x["benchmark_weight"] is None for x in r["rows"])


def test_valuation_must_reconcile():
    a, p, s = fixture()
    a["cash"] = 0
    with pytest.raises(ValueError, match="reconcile"):
        build_industry_report(a, p, s, now=NOW)


def test_immutable_snapshot_hash_detects_tampering(tmp_path):
    _, _, snapshot = fixture()
    path = publish_snapshot(snapshot, tmp_path)
    assert load_snapshot(tmp_path) == snapshot
    path.write_text(json.dumps({**snapshot, "taxonomy": "changed"}))
    with pytest.raises(ValueError, match="checksum"):
        load_snapshot(tmp_path)


def test_refresh_reuses_complete_today_snapshot_without_provider_calls(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from scripts import update_industry_risk as writer
    from src.papertrading import store
    a, p, s = fixture()
    s["observed_at"] = datetime.now(timezone.utc).isoformat()
    s["benchmarks"]["SPY"]["observed_at"] = s["observed_at"]
    monkeypatch.setattr(store, "list_accounts", lambda: [{"id": "a"}])
    monkeypatch.setattr(store, "load_account", lambda aid: a)
    monkeypatch.setattr(store, "load_table", lambda aid, name: p)
    monkeypatch.setattr(writer, "load_snapshot", lambda: s)
    monkeypatch.setattr(writer, "collect_snapshot", lambda *args: pytest.fail("unnecessary provider request"))
    saved = []
    monkeypatch.setattr(writer, "write_report", lambda report, path: saved.append(report))
    writer.refresh_current_reports()
    assert len(saved) == 1


def test_refresh_fetches_missing_held_security_instead_of_reusing_partial_snapshot(monkeypatch):
    from scripts import update_industry_risk as writer
    from src.papertrading import store
    a, p, complete = fixture()
    complete["observed_at"] = datetime.now(timezone.utc).isoformat()
    partial = {**complete, "classifications": {}}
    monkeypatch.setattr(store, "list_accounts", lambda: [{"id": "a"}])
    monkeypatch.setattr(store, "load_account", lambda aid: a)
    monkeypatch.setattr(store, "load_table", lambda aid, name: p)
    monkeypatch.setattr(writer, "load_snapshot", lambda: partial)
    calls = []
    monkeypatch.setattr(writer, "collect_snapshot", lambda t,b: calls.append((t,b)) or complete)
    monkeypatch.setattr(writer, "publish_snapshot", lambda *args: None)
    monkeypatch.setattr(writer, "write_report", lambda *args: None)
    writer.refresh_current_reports()
    assert calls == [(["A", "B"], ["SPY"])]
