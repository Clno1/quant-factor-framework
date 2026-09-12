from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import hashlib
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from src.group_analytics.rotation import CACHE_PRICE_BASIS, LEGACY_SCHEMA_VERSION, SCHEMA_VERSION, AMOUNT_AUDIT_STATUS
from src.group_analytics.rotation.context import evaluate_context, price_response
from src.group_analytics.rotation.engine import (
    amount_direction_label, analyze, assign_priority, basket_index, classify_axes, metric_frame, add_states,
)
from src.group_analytics.rotation.service import load_frames, run_rotation
from src.group_analytics.rotation.store import RotationStore, encoded
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
    frame = metric_frame(themes[0], p, v, strict=True)
    first = frame.iloc[60]
    second = frame.iloc[61]
    assert first.history_valid and second.history_valid
    assert first.strength_confirmed == "unavailable"
    assert first.strength_axis == "leading"
    assert first.priority != "wait"
    assert second.strength_confirmed == "leading"
    repeated = frame.copy(); add_states(repeated)
    assert frame.strength_confirmed.tolist() == repeated.strength_confirmed.tolist()
    broken = frame.copy(); broken.iloc[-2, broken.columns.get_loc("history_valid")] = False
    add_states(broken)
    assert broken.iloc[-1].strength_confirmed == "unavailable"
    assert broken.iloc[-1].strength_candidate in {"leading", "flat", "lagging"}


def test_constant_relative_trend_is_steady_leading_not_wait():
    n = 80
    dates = pd.bdate_range("2026-01-05", periods=n)
    c = np.log(1.02) / 20
    t = np.arange(n)
    qqq = 100 * np.exp(0.0004 * t)
    etf = qqq * np.exp(c * t)
    prices = pd.DataFrame({"QQQ": qqq, "ETF": etf}, index=dates)
    volume = pd.DataFrame(1000.0, index=dates, columns=prices.columns)
    theme = Theme("etf", "ETF测试", "technology", "QQQ", proxy="ETF")
    frame = metric_frame(theme, prices, volume, strict=True)
    latest = frame.iloc[-1]
    assert latest.strength_axis == "leading"
    assert latest.speed_axis == "steady"
    assert latest.priority == "focus"
    assert latest.action == "focus"
    assert "wait" not in set(frame.loc[frame.history_valid, "action"])
    assert abs(latest.acceleration_log) < 1e-9


def test_state_exact_acceleration_boundary_is_stable():
    n = 80
    dates = pd.bdate_range("2026-01-05", periods=n)
    c = np.log(1.02) / 20
    t = np.arange(n)
    qqq = 100 * np.exp(0.0004 * t)
    etf = qqq * np.exp(c * t)
    prices = pd.DataFrame({"QQQ": qqq, "ETF": etf}, index=dates)
    volume = pd.DataFrame(1000.0, index=dates, columns=prices.columns)
    theme = Theme("etf", "ETF测试", "technology", "QQQ", proxy="ETF")
    frame = metric_frame(theme, prices, volume, strict=True)
    valid = frame.iloc[60:]
    assert (valid.speed_axis == "steady").all()
    assert (valid.strength_axis == "leading").all()


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
    assert "固定快照" in payload["embeds"][0]["description"]
    assert report["kind"] == "rotation_v3"
    assert payload["embeds"][0]["url"].startswith("https://example.com/group-analytics?run=")
    assert any(field["name"] == "页面入口" and "https://example.com/group-analytics" in field["value"]
               and "?run=" not in field["value"] for field in payload["embeds"][0]["fields"])
    assert payload["allowed_mentions"] == {"parse":[]}
    with pytest.raises(SourceGateError):
        load_rotation_report("2000-01-01",store=store)


def test_discord_stale_holdings_does_not_say_unlinked():
    s = snapshot()
    s["run_id"] = "rot_20260515_aaaaaaaaaaaaaaaa"
    s["rows"][0]["production"]["action"] = "focus"
    s["rows"][0]["holdings_breadth"] = {
        "breadth_kind": "unavailable",
        "status": "HOLDINGS_OBSERVATION_STALE",
        "breadth_equal_weight_pct": None,
    }
    payload = rotation_payload(
        s, SimpleNamespace(target_session="2026-09-09"),
        PremarketDigestSettings(dashboard_base_url="https://example.com"),
    )
    text = payload["embeds"][0]["fields"][0]["value"]
    assert "过期未用" in text
    assert "真实广度未接入" not in text


def test_v3_digest_kind_does_not_fall_back_to_daily_group_embed():
    from src.premarket_digest.render import build_sector_rotation_payload
    s = snapshot()
    s["kind"] = "rotation_v3"
    s["run_id"] = "rot_20260515_aaaaaaaaaaaaaaaa"
    payload = build_sector_rotation_payload(
        s, SimpleNamespace(target_session="2026-09-09"),
        PremarketDigestSettings(dashboard_base_url="https://example.com"),
    )
    assert "不含实时盘前" in payload["embeds"][0]["description"]
    assert "今日分类涨跌" not in str(payload)


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
        assert "rotation-asof" in client.get("/group-analytics").text
        assert "固定历史快照" in client.get("/group-analytics").text
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
    assert row.action == "defensive"
    assert row.priority == "defensive"
    assert row.action not in {"priority", "price_watch", "focus"}


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


def _price_cli_patches(tmp_path):
    import exchange_calendars as xcals
    dates = xcals.get_calendar("XNYS").sessions_in_range("2025-10-01", "2026-09-08")
    frame = pd.DataFrame({"adj_close": 100 * np.exp(.0001 * np.arange(len(dates))), "volume": 10000}, index=dates)
    theme = Theme("etf", "测试", "technology", "QQQ", proxy="ETF")
    return [
        patch("src.group_analytics.rotation.service.default_themes", return_value=[theme]),
        patch("src.group_analytics.rotation.service.load_frames", return_value={"ETF": frame, "QQQ": frame}),
        patch("scripts.run_group_rotation.run_rotation",
              side_effect=lambda **kw: run_rotation(now="2026-09-09T01:00:00Z", **kw)),
    ], tmp_path


def test_price_stage_never_loads_momentum(tmp_path):
    from scripts.run_group_rotation import main
    patches, root = _price_cli_patches(tmp_path)
    with patches[0], patches[1], patches[2], \
         patch("scripts.run_group_rotation.load_momentum_report") as load_report:
        assert main(["--stage", "price", "--asof", "2026-09-08", "--output-root", str(root)]) == 0
    load_report.assert_not_called()
    stored = RotationStore(root).load()
    assert stored["candidate_linkage"]["status"] == "unavailable"
    assert stored["candidate_linkage"]["reason"] == "个股关联待后续阶段"


def test_price_stage_duplicate_fingerprint_is_noop(tmp_path, capsys):
    from scripts.run_group_rotation import main
    patches, root = _price_cli_patches(tmp_path)
    nows = iter(["2026-09-09T01:00:00Z", "2026-09-09T03:00:00Z"])
    with patches[0], patches[1], \
         patch("scripts.run_group_rotation.run_rotation",
               side_effect=lambda **kw: run_rotation(now=next(nows), **kw)), \
         patch("scripts.run_group_rotation.load_momentum_report") as load_report:
        assert main(["--stage", "price", "--asof", "2026-09-08", "--output-root", str(root)]) == 0
        first = json.loads(capsys.readouterr().out)
        assert first["status"] == "SUCCESS"
        assert main(["--stage", "price", "--asof", "2026-09-08", "--output-root", str(root)]) == 0
        second = json.loads(capsys.readouterr().out)
    load_report.assert_not_called()
    assert second["status"] == "NOOP"
    assert second["run_id"] == first["run_id"]
    assert len(list((root / "runs").glob("*.json"))) == 1


def test_price_stage_new_holdings_fingerprint_republishes(tmp_path, capsys):
    from scripts.run_group_rotation import main
    from src.group_analytics.rotation.holdings import normalize_observation, save_observation
    patches, root = _price_cli_patches(tmp_path)
    holdings = tmp_path / "holdings"
    nows = iter(["2026-09-09T01:00:00Z", "2026-09-09T03:00:00Z"])
    argv = ["--stage", "price", "--asof", "2026-09-08", "--output-root", str(root),
            "--holdings-root", str(holdings)]
    with patches[0], patches[1], \
         patch("scripts.run_group_rotation.run_rotation",
               side_effect=lambda **kw: run_rotation(now=next(nows), **kw)):
        assert main(argv) == 0
        first = json.loads(capsys.readouterr().out)
        data = [{"symbol": "ETF", "asset": "QQQ", "isin": "USQQQ", "weightPercentage": 100,
                 "updatedAt": "2026-09-08 17:00:00"}]
        save_observation(holdings, normalize_observation(data, "ETF", "2026-09-08T18:00:00Z"))
        assert main(argv) == 0
        second = json.loads(capsys.readouterr().out)
    assert first["status"] == "SUCCESS"
    assert second["status"] == "SUCCESS"
    assert second["run_id"] != first["run_id"]
    assert len(list((root / "runs").glob("*.json"))) == 2


def test_linkage_unavailable_does_not_overwrite_or_mark_failure(tmp_path, capsys):
    from scripts.run_group_rotation import main
    patches, root = _price_cli_patches(tmp_path)
    with patches[0], patches[1], patches[2], \
         patch("scripts.run_group_rotation.load_momentum_report", side_effect=ValueError("private-path-secret")):
        assert main(["--stage", "price", "--asof", "2026-09-08", "--output-root", str(root)]) == 0
        capsys.readouterr()
        first = RotationStore(root).load()["run_id"]
        assert main(["--stage", "linkage", "--asof", "2026-09-08", "--output-root", str(root)]) == 0
        payload = json.loads(capsys.readouterr().out)
    store = RotationStore(root)
    assert payload["status"] == "LINKAGE_UNAVAILABLE"
    assert store.load()["run_id"] == first
    assert store.last_attempt()["status"] == "SUCCESS"
    assert "private-path-secret" not in str(store.load())


def test_linkage_success_advances_latest_and_keeps_old_run(tmp_path):
    from scripts.run_group_rotation import main
    patches, root = _price_cli_patches(tmp_path)
    report = {"source_session": "2026-09-08", "input_fingerprint": "fp-1", "universe": "SP500", "rows": []}
    with patches[0], patches[1], patches[2], \
         patch("scripts.run_group_rotation.load_momentum_report", return_value=report):
        assert main(["--stage", "price", "--asof", "2026-09-08", "--output-root", str(root)]) == 0
        old = RotationStore(root).load()
        assert main(["--stage", "linkage", "--asof", "2026-09-08", "--output-root", str(root)]) == 0
        assert main(["--stage", "linkage", "--asof", "2026-09-08", "--output-root", str(root)]) == 0
    store = RotationStore(root)
    latest = store.load()
    assert latest["run_id"] != old["run_id"]
    assert latest["candidate_linkage"]["status"] == "available"
    assert latest["generated_at"] == old["generated_at"]
    replayed = store.load(old["run_id"])
    assert replayed["candidate_linkage"]["status"] == "unavailable"
    assert len(list((root / "runs").glob("*.json"))) == 2
    from src.group_analytics.rotation.replay import replay_snapshot
    assert "NO_QUALIFIED_CANDIDATE" in latest["rows"][0]["evidence_gaps"]
    assert "NO_QUALIFIED_CANDIDATE" not in latest["rows"][0]["production"]["evidence_gaps"]
    assert replay_snapshot(latest)["status"] == "MATCH"


def test_linkage_rejects_refresh_and_without_candidates(tmp_path):
    from scripts.run_group_rotation import main
    assert main(["--stage", "linkage", "--refresh", "--asof", "2026-09-08", "--output-root", str(tmp_path)]) == 2
    assert main(["--stage", "linkage", "--without-candidates", "--asof", "2026-09-08",
                 "--output-root", str(tmp_path)]) == 2
    assert not RotationStore(tmp_path).initialized


def test_cli_failure_records_iso_session_not_latest(tmp_path):
    from scripts.run_group_rotation import main
    with patch("src.group_analytics.settings.load_group_analytics_settings",
               return_value=SimpleNamespace(enabled=False)):
        assert main(["--asof", "latest", "--output-root", str(tmp_path)]) == 1
    attempt = RotationStore(tmp_path).last_attempt()
    assert attempt["status"] == "FAILED"
    assert attempt["source_session"] != "latest"
    assert len(str(attempt["source_session"])) == 10


def test_rotation_page_freshness_contract():
    html = Path("src/webapp/templates/group_rotation.html").read_text(encoding="utf-8")
    js = Path("src/webapp/static/js/group_rotation.js").read_text(encoding="utf-8")
    assert "rotation-asof" in html and "rotation-generated" in html and "rotation-next" in html
    assert "固定历史快照" in html
    assert ">强弱<" in html and ">速度<" in html and ">成交活跃<" in html and ">风险<" in html
    assert ">净申赎<" in html
    assert "仅部分持仓观察" in html
    assert "生产分数不在主表" in html
    assert "schema_legacy" in js
    assert "strength_label" in js
    assert "holdings_breadth" in js
    assert "net_creation" in js
    assert "visibilitychange" in js
    assert "5 * 60 * 1000" in js
    assert "America/New_York" in js
    assert 'overlayStatus === "HOLDINGS_OBSERVATION_STALE"' in js
    assert 'overlayStatus === "HOLDINGS_MEASUREMENT_FAILED"' in js
    assert "latest.freshness" in js
    assert "latest.last_attempt" in js
    assert "已测基金权重" in js
    assert "PARTIAL_HOLDINGS_COVERAGE" in js
    assert "仅部分持仓观察" in js


def test_group_rotation_price_adapter_reads_store(tmp_path):
    from datetime import datetime, timezone
    from src.operations.adapters.research import collect_research_evidence
    from src.operations.models import JobDefinition, JobStatus
    store = RotationStore(tmp_path / "group_analytics" / "rotation")
    published = attach_candidates(snapshot())
    published["source_session"] = "2026-09-08"
    store.publish(published)
    job = JobDefinition(
        job_id="group_rotation_price", display_name="板块轮动价格层", category="RESEARCH",
        run_type="SCHEDULED_BATCH", adapter="group_rotation", order=35, enabled_expected=True,
        schedule={"timezone": "America/New_York", "time": "17:30", "deadline_minutes": 75,
                  "target_policy": "latest_publishable_xnys"},
    )
    now = datetime(2026, 9, 8, 22, 0, tzinfo=timezone.utc)
    with patch("src.operations.adapters.research._rotation_store", return_value=store), \
         patch("src.operations.adapters.research.expected_target_session", return_value="2026-09-08"):
        result = collect_research_evidence([job], now=now, observed_at=now.isoformat())
    assert result.snapshots[0].status == JobStatus.SUCCESS
    assert result.snapshots[0].job_id == "group_rotation_price"


def _write_run(store, snapshot_payload):
    from src.group_analytics.artifacts import normalize_json_value
    snapshot_payload = normalize_json_value(snapshot_payload)
    snapshot_payload.pop("run_id", None)
    snapshot_payload.pop("schema_legacy", None)
    data = encoded(snapshot_payload)
    digest = hashlib.sha256(data).hexdigest()
    run_id = "rot_" + snapshot_payload["source_session"].replace("-", "") + "_" + digest[:16]
    target = store.root / "runs" / (run_id + ".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"sha256": digest, "snapshot": snapshot_payload}, ensure_ascii=False))
    pointer = {"run_id": run_id, "sha256": digest, "source_session": snapshot_payload["source_session"]}
    (store.root / "latest.json").write_text(json.dumps(pointer))
    return run_id


def legacy_snapshot():
    from src.group_analytics.artifacts import normalize_json_value
    d, p, v, themes = sample()
    return normalize_json_value({
        "schema_version": LEGACY_SCHEMA_VERSION, "source_session": str(d[-1].date()),
        "generated_at": "2026-09-09T00:00:00+00:00", "session_status": "FINAL",
        "valid_theme_count": 2, "total_theme_count": 2, "notes": [], "amount_verified": False,
        "context": evaluate_context([], cutoff="2026-09-09T00:00:00+00:00"),
        "rows": analyze(p, v, d, themes, schema_version=LEGACY_SCHEMA_VERSION),
        "input_panel": {"sessions": [str(x.date()) for x in d],
                        "price_columns": list(p.columns), "volume_columns": list(v.columns),
                        "prices": p.to_numpy().tolist(), "volumes": v.to_numpy().tolist()},
    })


def test_priority_completeness_covers_every_axis_combination():
    seen = set()
    for strength in ("leading", "flat", "lagging"):
        for speed in ("accelerating", "steady", "decelerating"):
            for abs20 in (1.0, 0.0, -1.0):
                for above in (True, False):
                    for abs5 in (1.0, 0.0, -1.0):
                        index, ma20 = (2.0, 1.0) if above else (1.0, 2.0)
                        got = assign_priority(True, strength, speed, abs20, index, ma20, abs5)
                        assert got in {"focus", "defensive", "recover", "weak", "neutral"}
                        if strength == "leading" and abs20 > 0 and above:
                            assert got == "focus"
                        elif strength == "leading":
                            assert got == "defensive"
                            assert got != "focus"
                        elif strength == "lagging" and speed == "accelerating" and abs5 > 0:
                            assert got == "recover"
                        elif strength == "lagging":
                            assert got == "weak"
                        else:
                            assert got == "neutral"
                        seen.add(got)
    assert seen == {"focus", "defensive", "recover", "weak", "neutral"}
    assert assign_priority(False, "leading", "accelerating", 1, 2, 1, 1) == "unavailable"


def test_one_day_noise_does_not_change_confirmed_strength_or_priority():
    n = 90
    dates = pd.bdate_range("2026-01-05", periods=n)
    c = np.log(1.10) / 20
    t = np.arange(n)
    qqq = 100 * np.exp(0.01 * t)
    etf = qqq * np.exp(c * t)
    prices = pd.DataFrame({"QQQ": qqq, "ETF": etf}, index=dates)
    volume = pd.DataFrame(1000.0, index=dates, columns=prices.columns)
    theme = Theme("etf", "ETF测试", "technology", "QQQ", proxy="ETF")
    clean = metric_frame(theme, prices, volume, strict=True)
    assert clean.iloc[-1].strength_confirmed == "leading"
    assert clean.iloc[-1].priority == "focus"
    noisy = prices.copy()
    noisy.loc[dates[-1], "ETF"] = noisy.ETF.iloc[-2] * 0.90
    changed = metric_frame(theme, noisy, volume, strict=True)
    assert changed.iloc[-1].strength_axis == "lagging"
    assert changed.iloc[-1].strength_confirmed == "leading"
    assert changed.iloc[-1].priority == "focus"
    assert changed.iloc[-1].strength_confirmation_count == 1
    assert changed.iloc[-1].action != "wait"


def test_speed_axis_is_not_two_bar_confirmed():
    n = 90
    dates = pd.bdate_range("2026-01-05", periods=n)
    c = np.log(1.02) / 20
    t = np.arange(n)
    qqq = 100 * np.exp(0.0004 * t)
    etf = qqq * np.exp(c * t)
    prices = pd.DataFrame({"QQQ": qqq, "ETF": etf}, index=dates)
    volume = pd.DataFrame(1000.0, index=dates, columns=prices.columns)
    theme = Theme("etf", "ETF测试", "technology", "QQQ", proxy="ETF")
    prices.loc[dates[-1], "ETF"] = prices.ETF.iloc[-1] * 1.04
    frame = metric_frame(theme, prices, volume, strict=True)
    assert frame.iloc[-2].speed_axis == "steady"
    assert frame.iloc[-1].speed_axis == "accelerating"
    assert frame.iloc[-1].strength_confirmed == "leading"
    assert frame.iloc[-2].strength_confirmed == "leading"


def test_extended_theme_can_still_be_focus():
    n = 100
    dates = pd.bdate_range("2026-01-05", periods=n)
    t = np.arange(n)
    qqq = np.full(n, 100.0)
    etf = 100 * np.exp(0.004 * t)
    prices = pd.DataFrame({"QQQ": qqq, "ETF": etf}, index=dates)
    volume = pd.DataFrame(1000.0, index=dates, columns=prices.columns)
    theme = Theme("etf", "ETF测试", "technology", "QQQ", proxy="ETF")
    row = metric_frame(theme, prices, volume, strict=True).iloc[-1]
    assert row.extension
    assert "EXTENDED" in list(row.risk_flags)
    assert row.priority == "focus"
    assert row.action == "focus"


def test_unlinked_etf_gap_does_not_change_priority():
    d, p, v, themes = sample(80)
    rows = analyze(p, v, d, (themes[0],))
    latest = rows[0]["production"]
    assert "ETF_HOLDINGS_NOT_LINKED" in latest["evidence_gaps"]
    assert latest["priority"] != "unavailable" or not latest["history_valid"]
    assert latest["action"] not in {"wait", "price_watch"}


def test_small_basket_gap_is_independent():
    d, p, v, _ = sample(80)
    theme = Theme("tiny", "小篮子", "technology", "QQQ", members=("S0", "S1", "S2"))
    rows = analyze(p, v, d, (theme,))
    assert "SMALL_BASKET" in rows[0]["evidence_gaps"]
    assert rows[0]["production"]["action"] != "wait"


def test_relative_underperformance_label_is_not_decline():
    assert amount_direction_label(1.0, 1.0, 1.4) == "放量相对走强"
    assert amount_direction_label(-1.0, 1.0, 1.4) == "放量相对走弱"
    assert "下跌" not in amount_direction_label(-1.0, 1.0, 1.1)
    assert amount_direction_label(-1.0, -0.5, 0.8) == "缩量相对走弱（绝对下跌）"
    x, y, strength, speed = classify_axes(0.4963, 2.0)
    assert strength == "leading"
    assert speed == "steady"


def test_split_adjusted_close_times_volume_stays_continuous():
    dates = pd.bdate_range("2026-01-05", periods=40)
    close = pd.Series(np.full(40, 100.0), index=dates)
    volume = pd.Series(np.full(40, 1000.0), index=dates)
    close.iloc[-1] = 50.0
    volume.iloc[-1] = 2000.0
    prices = pd.DataFrame({"ETF": close, "QQQ": close}, index=dates)
    volumes = pd.DataFrame({"ETF": volume, "QQQ": volume}, index=dates)
    theme = Theme("etf", "ETF测试", "technology", "QQQ", proxy="ETF")
    row = metric_frame(theme, prices, volumes, strict=True, amount_verified=True,
                       execution_close=prices).iloc[-1]
    assert row.amount_proxy == pytest.approx(100000)
    assert row.amount_ratio == pytest.approx(1.0)


def test_amount_uses_execution_close_not_dividend_adjusted_price():
    dates = pd.bdate_range("2026-01-05", periods=30)
    close = pd.Series(np.full(30, 100.0), index=dates)
    adj = close.copy()
    adj.iloc[-1] = 90.0
    volume = pd.Series(np.full(30, 1000.0), index=dates)
    prices = pd.DataFrame({"ETF": adj, "QQQ": adj}, index=dates)
    execution = pd.DataFrame({"ETF": close, "QQQ": close}, index=dates)
    volumes = pd.DataFrame({"ETF": volume, "QQQ": volume}, index=dates)
    theme = Theme("etf", "ETF测试", "technology", "QQQ", proxy="ETF")
    wrong = metric_frame(theme, prices, volumes, strict=True, amount_verified=True).iloc[-1]
    right = metric_frame(theme, prices, volumes, strict=True, amount_verified=True,
                         execution_close=execution).iloc[-1]
    assert wrong.amount_proxy == pytest.approx(90000)
    assert right.amount_proxy == pytest.approx(100000)


def test_wrong_cache_price_basis_is_treated_as_missing(tmp_path):
    dates = pd.bdate_range("2026-01-05", periods=5)
    frame = pd.DataFrame({"close": 100.0, "adj_close": 100.0, "volume": 1000.0}, index=dates)
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    frame.to_parquet(canonical / "QQQ.parquet")
    assert load_frames(["QQQ"], "2026-01-05", "2026-01-09", cache_root=tmp_path) == {}
    (canonical / "QQQ.basis.json").write_text(json.dumps({"price_basis": "dividend_adjusted_legacy"}))
    assert load_frames(["QQQ"], "2026-01-05", "2026-01-09", cache_root=tmp_path) == {}
    (canonical / "QQQ.basis.json").write_text(json.dumps({"price_basis": CACHE_PRICE_BASIS}))
    loaded = load_frames(["QQQ"], "2026-01-05", "2026-01-09", cache_root=tmp_path)
    assert "QQQ" in loaded


def test_canonical_refresh_writes_basis_sidecar(tmp_path):
    dates = pd.bdate_range("2026-01-05", periods=3)

    def fake_fetch(symbol, start, end):
        return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                             "adj_close": 1.0, "volume": 1.0}, index=dates)

    loaded = load_frames(["QQQ"], "2026-01-05", "2026-01-07", refresh=True,
                         cache_root=tmp_path, fetcher=fake_fetch)
    assert "QQQ" in loaded
    meta = json.loads((tmp_path / "canonical" / "QQQ.basis.json").read_text())
    assert meta["price_basis"] == CACHE_PRICE_BASIS
    shared = tmp_path / "raw_ohlcv"
    shared.mkdir()
    pd.DataFrame({"close": [9.0]}).to_parquet(shared / "SPY.parquet")
    assert load_frames(["SPY"], "2026-01-05", "2026-01-07", cache_root=tmp_path) == {}


def test_holdings_cache_leaf_does_not_share_theme_canonical(tmp_path):
    from src.group_analytics.rotation.service import HOLDINGS_CACHE_LEAF

    dates = pd.bdate_range("2026-01-05", periods=3)

    def fake_fetch(symbol, start, end):
        return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                             "adj_close": 1.0, "volume": 1.0}, index=dates)

    loaded = load_frames(["NVDA"], "2026-01-05", "2026-01-07", refresh=True,
                         cache_root=tmp_path, cache_leaf=HOLDINGS_CACHE_LEAF, fetcher=fake_fetch)
    assert "NVDA" in loaded
    assert (tmp_path / "holdings_canonical" / "NVDA.parquet").exists()
    assert not (tmp_path / "canonical" / "NVDA.parquet").exists()


def test_legacy_snapshot_load_sets_schema_legacy(tmp_path):
    store = RotationStore(tmp_path)
    run = _write_run(store, legacy_snapshot())
    loaded = store.load()
    assert loaded["run_id"] == run
    assert loaded["schema_legacy"] is True
    assert loaded["schema_version"] == LEGACY_SCHEMA_VERSION


def test_v3_publish_advances_pointer_over_v2_latest(tmp_path):
    store = RotationStore(tmp_path)
    old = _write_run(store, legacy_snapshot())
    assert store.load()["run_id"] == old
    fresh = attach_candidates(snapshot())
    fresh["source_session"] = store.load()["source_session"]
    new = store.publish(fresh)
    latest = store.load()
    assert new != old
    assert latest["run_id"] == new
    assert latest["schema_version"] == SCHEMA_VERSION
    assert latest["schema_legacy"] is False
    assert store.load(old)["schema_legacy"] is True


def test_replay_v2_uses_v2_rules_not_v3():
    from src.group_analytics.rotation.replay import replay_snapshot
    from src.group_analytics.artifacts import normalize_json_value
    d, p, v, themes = sample(80)
    rows = analyze(p, v, d, themes, schema_version=LEGACY_SCHEMA_VERSION)
    panel = normalize_json_value({
        "sessions": [str(x.date()) for x in d],
        "price_columns": list(p.columns), "volume_columns": list(v.columns),
        "prices": p.to_numpy().tolist(), "volumes": v.to_numpy().tolist(),
    })
    snap = normalize_json_value({
        "schema_version": LEGACY_SCHEMA_VERSION, "source_session": str(d[-1].date()),
        "amount_verified": False, "rows": rows, "input_panel": panel,
        "input_fingerprint": hashlib.sha256(encoded(panel)).hexdigest(),
    })
    result = replay_snapshot(snap)
    assert result["status"] == "MATCH", result["differences"][:5]
    assert result["schema_version"] == LEGACY_SCHEMA_VERSION
    v3 = analyze(p, v, d, themes, schema_version=SCHEMA_VERSION)
    assert "strength_axis" not in rows[0]["production"]
    assert "strength_axis" in v3[0]["production"]


def test_service_records_amount_basis_and_replays(tmp_path):
    import exchange_calendars as xcals
    from src.group_analytics.rotation.replay import replay_snapshot
    cal = xcals.get_calendar("XNYS")
    dates = cal.sessions_in_range("2025-01-02", "2026-09-08")
    frame = pd.DataFrame({
        "adj_close": 100 * np.exp(.0001 * np.arange(len(dates))),
        "close": 100 * np.exp(.0001 * np.arange(len(dates))),
        "volume": 10000,
    }, index=dates)
    theme = Theme("etf", "测试", "technology", "QQQ", proxy="ETF")
    result = run_rotation(asof="2026-09-08", store=RotationStore(tmp_path),
                          frames={"ETF": frame, "QQQ": frame}, themes=[theme],
                          now="2026-09-09T01:00:00Z", dry_run=True)
    assert result["schema_version"] == SCHEMA_VERSION
    assert result["amount_basis"] == "split_adjusted_close_x_volume"
    assert result["price_basis"] == CACHE_PRICE_BASIS
    assert result["amount_verified"] is True
    assert result["amount_audit_status"] == AMOUNT_AUDIT_STATUS
    assert "execution_close" in result["input_panel"]
    assert "priority_breadth_pct" not in result["parameters"]
    assert result["parameters"]["price_state_version"] == "dual-axis-v3"
    assert replay_snapshot(result)["status"] == "MATCH"


def test_missing_close_does_not_fall_back_to_adj_for_amount(tmp_path):
    import exchange_calendars as xcals
    cal = xcals.get_calendar("XNYS")
    dates = cal.sessions_in_range("2025-01-02", "2026-09-08")
    frame = pd.DataFrame({
        "adj_close": 100 * np.exp(.0001 * np.arange(len(dates))),
        "volume": 10000,
    }, index=dates)
    theme = Theme("etf", "测试", "technology", "QQQ", proxy="ETF")
    result = run_rotation(
        asof="2026-09-08", store=RotationStore(tmp_path),
        frames={"ETF": frame, "QQQ": frame}, themes=[theme],
        now="2026-09-09T01:00:00Z", dry_run=True,
    )
    amount = result["rows"][0]["production"]["amount_proxy"]
    assert amount is None or (isinstance(amount, float) and np.isnan(amount))


def test_amount_verified_flip_republishes(tmp_path, capsys):
    from scripts.run_group_rotation import main
    patches, root = _price_cli_patches(tmp_path)
    nows = iter(["2026-09-09T01:00:00Z", "2026-09-09T03:00:00Z"])
    with patches[0], patches[1], \
         patch("scripts.run_group_rotation.run_rotation",
               side_effect=lambda **kw: run_rotation(now=next(nows), **kw)):
        assert main(["--stage", "price", "--asof", "2026-09-08", "--output-root", str(root)]) == 0
        first = json.loads(capsys.readouterr().out)
        assert main(["--stage", "price", "--asof", "2026-09-08", "--output-root", str(root),
                     "--no-amount-verified"]) == 0
        second = json.loads(capsys.readouterr().out)
    assert first["status"] == "SUCCESS"
    assert second["status"] == "SUCCESS"
    assert second["run_id"] != first["run_id"]


def test_member_price_change_republishes(tmp_path, capsys):
    from scripts.run_group_rotation import main
    from src.group_analytics.rotation.holdings import normalize_observation, save_observation
    import exchange_calendars as xcals
    dates = xcals.get_calendar("XNYS").sessions_in_range("2025-10-01", "2026-09-08")
    close = 100 * np.exp(.0001 * np.arange(len(dates)))
    frame = pd.DataFrame({"adj_close": close, "close": close, "volume": 10000}, index=dates)
    members = {f"S{i}": frame.copy() for i in range(5)}
    theme = Theme("etf", "测试", "technology", "QQQ", proxy="ETF")
    holdings = tmp_path / "holdings"
    data = [{"symbol": "ETF", "asset": f"S{i}", "isin": f"US{i}", "weightPercentage": 20,
             "updatedAt": "2026-09-08 17:00:00"} for i in range(5)]
    save_observation(holdings, normalize_observation(data, "ETF", "2026-09-08T18:00:00Z"))
    frames = {"ETF": frame, "QQQ": frame, **members}
    nows = iter(["2026-09-09T01:00:00Z", "2026-09-09T03:00:00Z"])
    argv = ["--stage", "price", "--asof", "2026-09-08", "--output-root", str(tmp_path / "out"),
            "--holdings-root", str(holdings)]
    with patch("src.group_analytics.rotation.service.default_themes", return_value=[theme]), \
         patch("src.group_analytics.rotation.service.load_frames", return_value=frames), \
         patch("scripts.run_group_rotation.run_rotation",
               side_effect=lambda **kw: run_rotation(now=next(nows), **kw)):
        assert main(argv) == 0
        first = json.loads(capsys.readouterr().out)
        members["S0"].loc[dates[-1], "adj_close"] = float(members["S0"].iloc[-1]["adj_close"]) * 0.5
        members["S0"].loc[dates[-1], "close"] = float(members["S0"].iloc[-1]["close"]) * 0.5
        assert main(argv) == 0
        second = json.loads(capsys.readouterr().out)
    assert first["status"] == "SUCCESS"
    assert second["status"] == "SUCCESS"
    assert second["run_id"] != first["run_id"]


def test_price_refresh_writes_member_cache_into_holdings_leaf(tmp_path):
    from src.group_analytics.rotation.service import HOLDINGS_CACHE_LEAF, MEMBER_LOOKBACK_CALENDAR_DAYS
    dates = pd.bdate_range("2026-01-05", periods=25)
    stale = dates[:-1]
    root = tmp_path / "holdings_canonical"
    root.mkdir()
    stale_frame = pd.DataFrame({"close": 1.0, "adj_close": 1.0, "volume": 1.0}, index=stale)
    stale_frame.to_parquet(root / "NVDA.parquet")
    (root / "NVDA.basis.json").write_text(json.dumps({"price_basis": CACHE_PRICE_BASIS}))
    loaded = load_frames(["NVDA"], dates[0].date().isoformat(), dates[-1].date().isoformat(),
                         cache_root=tmp_path, cache_leaf=HOLDINGS_CACHE_LEAF)
    assert dates[-1] not in loaded["NVDA"].index

    def fake_fetch(symbol, start, end):
        return pd.DataFrame({"close": 2.0, "adj_close": 2.0, "volume": 1.0}, index=dates)

    refreshed = load_frames(
        ["NVDA"], dates[0].date().isoformat(), dates[-1].date().isoformat(),
        refresh=True, cache_root=tmp_path, cache_leaf=HOLDINGS_CACHE_LEAF, fetcher=fake_fetch,
    )
    assert dates[-1] in refreshed["NVDA"].index
    assert not (tmp_path / "canonical" / "NVDA.parquet").exists()
    assert MEMBER_LOOKBACK_CALENDAR_DAYS >= 80


def test_linkage_aborts_if_price_parent_moved(tmp_path, capsys):
    from scripts.run_group_rotation import RotationStageError, main, run_linkage_stage
    patches, root = _price_cli_patches(tmp_path)
    nows = iter(["2026-09-09T01:00:00Z", "2026-09-09T03:00:00Z"])
    report = {"source_session": "2026-09-08", "input_fingerprint": "fp-1", "universe": "SP500", "rows": []}
    with patches[0], patches[1], \
         patch("scripts.run_group_rotation.run_rotation",
               side_effect=lambda **kw: run_rotation(now=next(nows), **kw)), \
         patch("scripts.run_group_rotation.load_momentum_report", return_value=report):
        assert main(["--stage", "price", "--asof", "2026-09-08", "--output-root", str(root)]) == 0
        capsys.readouterr()
        old = RotationStore(root).load()
        assert main(["--stage", "price", "--asof", "2026-09-08", "--output-root", str(root),
                     "--no-amount-verified"]) == 0
        newest = RotationStore(root).load()
        with pytest.raises(RotationStageError) as exc:
            run_linkage_stage(asof="2026-09-08", store=RotationStore(root),
                              dry_run=False, snapshot=old)
    assert exc.value.code == "ROTATION_PARENT_MOVED"
    assert RotationStore(root).load()["run_id"] == newest["run_id"]


def test_linkage_ops_adapter_requires_available_candidates(tmp_path):
    from datetime import datetime, timezone
    from src.operations.adapters.research import collect_research_evidence
    from src.operations.models import JobDefinition, JobStatus
    store = RotationStore(tmp_path / "group_analytics" / "rotation")
    pending = attach_candidates(snapshot())
    pending["source_session"] = "2026-09-08"
    store.publish(pending)
    job = JobDefinition(
        job_id="group_analytics", display_name="板块轮动个股关联", category="RESEARCH",
        run_type="SCHEDULED_BATCH", adapter="group_rotation", order=40, enabled_expected=True,
        schedule={"timezone": "Asia/Singapore", "time": "13:15", "deadline_minutes": 75,
                  "target_policy": "latest_publishable_xnys"},
    )
    now = datetime(2026, 9, 8, 22, 0, tzinfo=timezone.utc)
    with patch("src.operations.adapters.research._rotation_store", return_value=store), \
         patch("src.operations.adapters.research.expected_target_session", return_value="2026-09-08"):
        result = collect_research_evidence([job], now=now, observed_at=now.isoformat())
    assert result.snapshots[0].status != JobStatus.SUCCESS
    linked = attach_candidates(pending, {"source_session": "2026-09-08", "input_fingerprint": "fp",
                                         "universe": "SP500", "rows": []})
    store.publish(linked)
    with patch("src.operations.adapters.research._rotation_store", return_value=store), \
         patch("src.operations.adapters.research.expected_target_session", return_value="2026-09-08"):
        result = collect_research_evidence([job], now=now, observed_at=now.isoformat())
    assert result.snapshots[0].status == JobStatus.SUCCESS
    assert result.snapshots[0].stage == "主题个股关联"