from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from src.group_analytics.rotation import SCHEMA_VERSION
from src.group_analytics.rotation.context import evaluate_context, price_response
from src.group_analytics.rotation.engine import analyze, basket_index, metric_frame, add_states
from src.group_analytics.rotation.service import run_rotation
from src.group_analytics.rotation.store import RotationStore
from src.group_analytics.rotation.themes import Theme, default_themes, required_symbols
from src.premarket_digest.rotation import attach_candidates, load_rotation_report, rotation_payload
from src.premarket_digest.models import SourceGateError
from src.premarket_digest.settings import PremarketDigestSettings


def sample(n=100):
    dates = pd.bdate_range("2026-01-05", periods=n)
    t = np.arange(n)
    curve = 100 * np.exp(.00002 * t * t)
    prices = pd.DataFrame({"QQQ": 100., "ETF": curve, **{f"S{i}": curve for i in range(5)}}, index=dates)
    volume = 1000 / prices
    volume.iloc[-1] *= 2
    themes = (Theme("etf", "ETF测试", "technology", "QQQ", proxy="ETF"),
              Theme("basket", "篮子测试", "technology", "QQQ", members=tuple(f"S{i}" for i in range(5))))
    return dates, prices, volume, themes


def test_injected_legacy_reader_never_reads_global_rotation_artifacts():
    from src.premarket_digest.groups import GroupArtifactDigestSource
    with patch("src.group_analytics.rotation.store.RotationStore",side_effect=AssertionError("global artifact leak")):
        source=GroupArtifactDigestSource(PremarketDigestSettings(),reader=SimpleNamespace())
    assert source.rotation_store is None


def snapshot():
    d, p, v, themes = sample()
    from src.group_analytics.artifacts import normalize_json_value
    return normalize_json_value({"schema_version": SCHEMA_VERSION, "source_session": str(d[-1].date()),
        "generated_at": "2026-09-09T00:00:00+00:00", "session_status": "FINAL",
        "valid_theme_count": 2, "total_theme_count": 2, "notes": [],
        "context": evaluate_context([], cutoff="2026-09-09T00:00:00+00:00"),
        "rows": analyze(p, v, d, themes)})


def test_registry_has_original_11_and_separate_sector_cohort():
    themes = default_themes()
    assert len(themes) == 22
    assert sum(t.cohort == "technology" for t in themes) == 11
    assert next(t for t in themes if t.id == "memory").members == ("MU", "WDC", "STX")
    assert len(required_symbols(themes)) == len(set(required_symbols(themes)))
    with pytest.raises(ValueError):
        Theme("bad", "bad", "x", "QQQ", proxy="../secret")


def test_public_golden_score_100_and_component_sum():
    d, p, v, themes = sample(80)
    result = metric_frame(themes[0], p, v, strict=False).iloc[-1]
    assert result["score"] == 100
    assert [result[x] for x in ("trend_points", "acceleration_points", "volume_points", "breadth_points", "extension_points")] == [30, 25, 20, 15, 10]
    assert result["source_state_code"] == 2
    assert result.rs5 == pytest.approx((p.ETF.iloc[-1] / p.ETF.iloc[-6] - 1) * 100)
    assert result.amount_ratio == pytest.approx(2000 / 1050)


def test_public_scores_cannot_be_97_99():
    totals = {t+a+v+b+e for t in (0,10,20,30) for a in (0,8,9,16,17,25)
              for v in (0,10,20) for b in (0,8,15) for e in (0,10)}
    assert not totals.intersection({97,99,79})


def test_etf_breadth_is_only_compat_proxy_and_score_missing_in_production():
    d, p, v, themes = sample()
    rows = analyze(p, v, d, themes)
    assert rows[0]["compatibility"]["breadth"] == 70
    assert pd.isna(rows[0]["production"]["breadth"])
    assert pd.isna(rows[0]["production"]["score"])
    assert rows[1]["production"]["breadth"] == 100
    assert pd.isna(rows[1]["production"]["amount_ratio"])


def test_real_breadth_and_verified_amount_enable_experimental_score():
    d, p, v, themes = sample(80)
    result = metric_frame(themes[1], p, v, strict=True, amount_verified=True).iloc[-1]
    assert result.score == 100
    assert result.breadth_n == 5


def test_daily_equal_weight_not_buy_and_hold():
    p = pd.DataFrame({"A":[100.,110,100],"B":[100.,100,110]})
    assert basket_index(p, strict=False).iloc[-1] == pytest.approx(105.477272727)


def test_missing_basket_never_looks_valid_in_production():
    d, p, v, themes = sample()
    p.loc[:, [f"S{i}" for i in range(5)]] = np.nan
    rows = analyze(p, v, d, themes)
    assert rows[1]["compatibility"]["valid"]
    assert not rows[1]["production"]["history_valid"]
    assert rows[1]["production"]["action"] == "unavailable"


def test_missing_middle_day_does_not_bridge_60d_window():
    d, p, v, themes = sample()
    p.loc[d[-20], "ETF"] = np.nan
    row = analyze(p, v, d, themes)[0]["production"]
    assert pd.isna(row["rs60"])
    assert row["action"] == "unavailable"


def test_future_rows_do_not_change_historical_metrics():
    d, p, v, themes = sample()
    expected = analyze(p, v, d[:-1], themes)[0]["production"]
    p.iloc[-1] = 99999
    actual = analyze(p, v, d[:-1], themes)[0]["production"]
    assert expected["rs20"] == actual["rs20"]


def test_duplicate_dates_rejected():
    d, p, v, themes = sample()
    with pytest.raises(ValueError):
        analyze(pd.concat([p,p.iloc[-1:]]), v, d, themes)


def test_source_distribution_and_extension_precedence():
    from src.group_analytics.rotation.engine import _source_state
    row = {"valid":True,"distribution":True,"extension":True,"score":90,"rs5":-1,"rs20":-1}
    assert _source_state(row) == -2
    row["distribution"] = False
    assert _source_state(row) == 3
    row.update(extension=False,score=44,rs20=1)
    assert _source_state(row) == 0


def test_state_confirmation_gap_reset_and_replay():
    d, p, v, themes = sample(80)
    p["ETF"] = p.ETF ** 1.5  # Clearly beyond acceleration dead band.
    frame = metric_frame(themes[0],p,v,strict=True)
    assert frame.iloc[60].state == "pending"
    assert frame.iloc[61].state == "leading"
    repeated = frame.copy(); add_states(repeated)
    assert frame.state.tolist() == repeated.state.tolist()
    broken = frame.copy(); broken.iloc[-2, broken.columns.get_loc("history_valid")] = False
    add_states(broken)
    assert broken.iloc[-1].state == "pending"


def test_state_exact_acceleration_boundary_is_stable():
    d, p, v, themes = sample(80)
    frame = metric_frame(themes[0], p, v, strict=True)
    assert frame.iloc[60:].boundary.all()
    assert set(frame.iloc[60:].state) == {"pending"}


def test_store_immutable_repeat_tamper_and_no_date_regression(tmp_path):
    store = RotationStore(tmp_path)
    s = snapshot()
    run = store.publish(s)
    assert store.publish(s) == run
    older = copy.deepcopy(s); older["source_session"] = "2026-01-01"
    store.publish(older)
    assert store.load()["run_id"] == run
    store.failure(s["source_session"], "failure")
    assert store.load()["run_id"] == run
    with pytest.raises(ValueError):
        store.load("../../secret")
    path = tmp_path / "runs" / (run + ".json")
    payload = json.loads(path.read_text()); payload["snapshot"]["rows"][0]["name"] = "tampered"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        store.load()


def test_candidate_join_exact_date_deduplicated_and_preserves_two_scores():
    s = snapshot()
    raw = {"ticker":"S0","data_date":s["source_session"],"status":"READY","close":100,"pivot":102,"score":88}
    report = {"source_session":s["source_session"],"rows":[raw,raw,{**raw,"ticker":"S1","data_date":"2000-01-01"}]}
    result = attach_candidates(s,report)
    stocks = result["rows"][1]["candidates"]
    assert len(stocks) == 1
    assert stocks[0]["score"] == 88
    assert stocks[0]["href"] == "/breakouts/S0"
    assert "candidates" not in s["rows"][1]
    report["source_session"] = "2000-01-01"
    assert not attach_candidates(s,report)["rows"][1]["candidates"]


def evidence(**updates):
    return {"id":"rates","family":"rates","direction":"support","strength":.8,
            "observed_at":"2026-09-04T20:00:00Z","published_at":"2026-09-07T20:15:00Z",
            "first_seen_at":"2026-09-07T20:16:00Z","publish_allowed":True,
            "source":"test","rule_version":"fixture","target_benchmark":"QQQ",**updates}


def test_context_blocks_future_unlicensed_and_stale():
    cutoff = "2026-09-08T13:20:00Z"
    for e in [evidence(first_seen_at="2026-09-09T00:00:00Z"),evidence(publish_allowed=False),evidence(observed_at="2026-01-01T00:00:00Z")]:
        r = evaluate_context([e],cutoff=cutoff)
        assert r["status"] == "insufficient"
        assert not r["accepted"]


def test_context_independent_families_and_price_response_not_action():
    obs = [evidence(),evidence(id="rates2"),evidence(id="usd",family="dollar")]
    result = evaluate_context(obs, cutoff="2026-09-08T13:20:00Z")
    assert result["support"] == 2  # Duplicate family is not a third vote.
    assert result["status"] == "support"
    assert "尚未兑现" in price_response(result, {"history_valid":True,"abs5":-1})


def test_digest_gate_stale_and_payload_budget(tmp_path):
    s = attach_candidates(snapshot()); store = RotationStore(tmp_path); store.publish(s)
    report = load_rotation_report(s["source_session"],store=store)
    ctx = SimpleNamespace(target_session="2026-09-09")
    payload = rotation_payload(report,ctx,PremarketDigestSettings(dashboard_base_url="https://example.com"))
    assert "不含实时盘前" in payload["embeds"][0]["description"]
    assert payload["embeds"][0]["url"].startswith("https://example.com/group-analytics?run=")
    assert payload["allowed_mentions"] == {"parse":[]}
    with pytest.raises(SourceGateError):
        load_rotation_report("2000-01-01",store=store)


def test_service_calendar_holiday_future_and_no_publish_dry_run(tmp_path):
    import exchange_calendars as xcals
    cal = xcals.get_calendar("XNYS")
    dates = cal.sessions_in_range("2025-01-02", "2026-09-08")
    frame = pd.DataFrame({"adj_close":100*np.exp(.0001*np.arange(len(dates))),"volume":10000},index=dates)
    store = RotationStore(tmp_path)
    theme = Theme("etf","测试","technology","QQQ",proxy="ETF")
    result = run_rotation(asof="2026-09-08",store=store,frames={"ETF":frame,"QQQ":frame},themes=[theme],
                          now="2026-09-09T01:00:00Z",dry_run=True)
    assert result["source_session"] == "2026-09-08"
    assert not store.initialized
    from src.group_analytics.rotation.replay import replay_snapshot
    assert replay_snapshot(result)["status"] == "MATCH"
    corrupt = copy.deepcopy(result)
    corrupt["input_panel"]["prices"][-1][0] = 1
    with pytest.raises(ValueError):
        replay_snapshot(corrupt)
    with pytest.raises(ValueError):
        run_rotation(asof="2026-09-09",store=store,now="2026-09-09T01:00:00Z",dry_run=True)
    from src.group_analytics.calendar import CalendarUnavailableError
    with pytest.raises(CalendarUnavailableError):
        run_rotation(asof="2026-09-07",store=store,now="2026-09-09T01:00:00Z",dry_run=True)


def test_new_main_and_legacy_route_and_read_only_api(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from dataclasses import replace
    import src.webapp.group_analytics_routes as routes
    store = RotationStore(tmp_path / "group_analytics" / "rotation")
    s = attach_candidates(snapshot())
    s["input_panel"] = {"private_audit_input": "not-a-public-data-endpoint"}
    run = store.publish(s)
    app = FastAPI(); app.include_router(routes.router)
    with patch.object(routes,"settings",replace(routes.settings,output_root=tmp_path)):
        client=TestClient(app)
        assert "rotation-main" in client.get("/group-analytics").text
        assert "ga-heatmap" in client.get("/group-analytics/daily").text
        response=client.get("/api/group-analytics/rotation").json()
        assert response["run_id"] == run
        assert "input_panel" not in response
        assert "history" not in response["rows"][0]
        detail=client.get("/api/group-analytics/rotation/etf",params={"run":run}).json()
        assert len(detail["theme"]["reference_history"]) == 100
        assert client.get("/api/group-analytics/rotation",params={"run":"../../secret"}).status_code == 422
        assert client.get("/api/group-analytics/rotation/no-such").status_code == 404
        store.failure(s["source_session"], "error")
        assert client.get("/api/group-analytics/rotation").json()["last_attempt"]["status"] == "FAILED"


def test_cli_publish_candidate_degradation_and_audit(tmp_path, capsys):
    import exchange_calendars as xcals
    from scripts.run_group_rotation import main
    from scripts.audit_group_rotation import main as audit
    dates = xcals.get_calendar("XNYS").sessions_in_range("2025-10-01", "2026-09-08")
    frame = pd.DataFrame({"adj_close":100*np.exp(.0001*np.arange(len(dates))),"volume":10000}, index=dates)
    theme = Theme("etf", "测试", "technology", "QQQ", proxy="ETF")
    with patch("src.group_analytics.rotation.service.default_themes",return_value=[theme]), \
         patch("src.group_analytics.rotation.service.load_frames",return_value={"ETF":frame,"QQQ":frame}), \
         patch("scripts.run_group_rotation.run_rotation", side_effect=lambda **kw: run_rotation(now="2026-09-09T01:00:00Z", **kw)), \
         patch("src.premarket_digest.momentum.CompletedSessionMomentumSource.load",side_effect=ValueError("private-path-secret")):
        assert main(["--asof","2026-09-08","--output-root",str(tmp_path),"--dry-run"]) == 0
        assert not RotationStore(tmp_path).initialized
        assert main(["--asof","2026-09-08","--output-root",str(tmp_path)]) == 0
    stored = RotationStore(tmp_path).load()
    assert stored["candidate_linkage"]["status"] == "unavailable"
    assert "private-path-secret" not in str(stored)
    assert audit(["--output-root",str(tmp_path)]) == 0
    assert '"status": "MATCH"' in capsys.readouterr().out


def test_source_cross_events_are_not_state_transitions():
    d, p, v, themes = sample(100)
    frame = metric_frame(themes[0], p, v, strict=False)
    for threshold in (60,75):
        expected = (frame.score > threshold) & (frame.score.shift(1) <= threshold)
        assert frame[f"source_cross_up_{threshold}"].equals(expected)
    assert frame.source_cross_down_45.equals((frame.score<45) & (frame.score.shift(1)>=45))


def test_price_outperformance_during_absolute_decline_is_not_priority():
    d, p, v, themes = sample(100)
    t = np.arange(100)
    p["ETF"] = 100*np.exp(-.001*t)
    p["QQQ"] = 100*np.exp(-.002*t-.00003*t*t)
    row=metric_frame(themes[0],p,v,strict=True).iloc[-1]
    assert row.rs20 > 0 and row.abs20 < 0
    assert row.action not in {"priority","price_watch"}


def test_digest_keeps_configured_role_opt_in():
    from dataclasses import replace
    s=attach_candidates(snapshot()); s["run_id"]="rot_20260101_"+"a"*16
    settings=replace(PremarketDigestSettings(),sector_rotation_role_id="123456789012345678")
    payload=rotation_payload(s,SimpleNamespace(target_session="2026-09-09"),settings)
    assert payload["allowed_mentions"]["roles"] == [settings.sector_rotation_role_id]


def test_rotation_digest_never_falls_back_after_v2_initialized(tmp_path):
    from src.premarket_digest.groups import GroupArtifactDigestSource
    store=RotationStore(tmp_path/"group_analytics"/"rotation")
    store.failure("2026-09-08","not-ready")
    reader=SimpleNamespace(settings=SimpleNamespace(output_root=tmp_path))
    source=GroupArtifactDigestSource(PremarketDigestSettings(),reader=reader)
    with patch.object(source,"_load_level",side_effect=AssertionError("must not silently use old daily rank")):
        with pytest.raises(SourceGateError):
            source.load("2026-09-08")
