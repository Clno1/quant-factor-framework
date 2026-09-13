from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
import hashlib

import numpy as np
import pandas as pd
import pytest

from src.group_analytics.artifacts import normalize_json_value
from src.group_analytics.rotation import LEGACY_SCHEMA_VERSION, SCHEMA_VERSION
from src.group_analytics.rotation.engine import analyze, metric_frame
from src.group_analytics.rotation.holdings import attach_holdings_breadth, normalize_observation
from src.group_analytics.rotation.replay import replay_snapshot
from src.group_analytics.rotation.service import ROTATION_PARAMETERS, run_rotation
from src.group_analytics.rotation.store import RotationStore, encoded
from src.group_analytics.rotation.themes import Theme
from src.group_analytics.rotation.timeline import (
    SPIKE_LOG_SHARE,
    TIMELINE_VERSION,
    attach_rotation_timeline,
    classify_persistence,
    format_rotation_timeline_text,
    refresh_timeline_breadth,
)
from src.premarket_digest.rotation import rotation_payload
from src.premarket_digest.settings import PremarketDigestSettings


def _volume(prices):
    return pd.DataFrame(1000.0, index=prices.index, columns=prices.columns)


def _themes_two_tech_one_sector():
    return (
        Theme("aaa", "强势科技", "technology", "QQQ", proxy="AAA"),
        Theme("bbb", "弱势科技", "technology", "QQQ", proxy="BBB"),
        Theme("sector_energy", "能源", "sectors", "SPY", proxy="XLE"),
    )


def test_parameters_document_timeline_version():
    assert ROTATION_PARAMETERS["rotation_timeline_version"] == TIMELINE_VERSION
    assert SPIKE_LOG_SHARE == 0.5


def test_same_cohort_ranks_and_mixed_cohorts_are_not_co_ranked():
    n = 100
    dates = pd.bdate_range("2026-01-05", periods=n)
    t = np.arange(n)
    qqq = 100 * np.exp(0.0004 * t)
    spy = 100 * np.exp(0.0003 * t)
    aaa = qqq * np.exp((np.log(1.04) / 20) * t)
    bbb = qqq * np.exp((np.log(1.01) / 20) * t)
    xle = spy * np.exp((np.log(1.20) / 20) * t)
    prices = pd.DataFrame({"QQQ": qqq, "SPY": spy, "AAA": aaa, "BBB": bbb, "XLE": xle}, index=dates)
    rows = analyze(prices, _volume(prices), dates, _themes_two_tech_one_sector())
    by_id = {row["id"]: row for row in rows}
    tech_a, tech_b, energy = by_id["aaa"], by_id["bbb"], by_id["sector_energy"]
    assert tech_a["rotation_timeline"]["rank_rs20"] == 1
    assert tech_b["rotation_timeline"]["rank_rs20"] == 2
    assert tech_a["rotation_timeline"]["rank_rs20_n"] == 2
    assert energy["rotation_timeline"]["rank_rs20"] == 1
    assert energy["rotation_timeline"]["rank_rs20_n"] == 1
    assert energy["production"]["rs20"] > tech_a["production"]["rs20"]
    assert tech_a["rotation_timeline"]["benchmark"] == "QQQ"
    assert energy["rotation_timeline"]["benchmark"] == "SPY"
    assert "rank_rs20" not in tech_a["production"]


def test_rank_change20_uses_same_cohort_window():
    n = 100
    dates = pd.bdate_range("2026-01-05", periods=n)
    t = np.arange(n)
    qqq = 100 * np.exp(0.0004 * t)
    q_a = np.exp(0.001 * t)
    q_b = np.empty(n)
    q_b[:n - 20] = np.exp(0.003 * np.arange(n - 20))
    q_b[n - 20:] = q_b[n - 21]
    spy = 100 * np.exp(0.0003 * t)
    prices = pd.DataFrame({
        "QQQ": qqq, "SPY": spy, "AAA": qqq * q_a, "BBB": qqq * q_b, "XLE": spy,
    }, index=dates)
    rows = analyze(prices, _volume(prices), dates, _themes_two_tech_one_sector())
    by_id = {row["id"]: row for row in rows}
    assert by_id["aaa"]["rotation_timeline"]["rank_rs20"] == 1
    assert by_id["bbb"]["rotation_timeline"]["rank_rs20"] == 2
    assert by_id["aaa"]["rotation_timeline"]["rank_rs20_ago20"] == 2
    assert by_id["bbb"]["rotation_timeline"]["rank_rs20_ago20"] == 1
    assert by_id["aaa"]["rotation_timeline"]["rank_change20"] == 1
    assert by_id["bbb"]["rotation_timeline"]["rank_change20"] == -1
    path = {point["offset"]: point["rank"] for point in by_id["aaa"]["rotation_timeline"]["rank_path20"]}
    assert path[0] == 1
    assert path[20] == 2


def _ratio_theme(q, name="etf"):
    n = len(q)
    dates = pd.bdate_range("2026-01-05", periods=n)
    qqq = 100 * np.exp(0.0004 * np.arange(n))
    prices = pd.DataFrame({"QQQ": qqq, "ETF": qqq * q}, index=dates)
    theme = Theme(name, "测试", "technology", "QQQ", proxy="ETF")
    return analyze(prices, _volume(prices), dates, (theme,))[0]


def test_event_spike_vs_repair_vs_gradual_vs_fading():
    n = 100
    t = np.arange(n)
    gradual_q = np.exp((np.log(1.04) / 20) * t)
    gradual = _ratio_theme(gradual_q)
    assert gradual["rotation_timeline"]["persistence"] == "gradual"
    assert gradual["rotation_timeline"]["log_share_5_of_20"] == pytest.approx(0.25, abs=0.02)

    spike_q = np.ones(n)
    spike_q[:n - 5] = np.exp(0.00005 * np.arange(n - 5))
    spike_q[n - 5:] = spike_q[n - 6] * np.exp(np.log(1.08) * (np.arange(5) + 1) / 5)
    spike = _ratio_theme(spike_q)
    assert spike["production"]["rs20"] > 0
    assert spike["rotation_timeline"]["log_share_5_of_20"] >= SPIKE_LOG_SHARE
    assert spike["rotation_timeline"]["persistence"] == "event_spike"

    repair_q = np.empty(n)
    repair_q[:n - 5] = np.exp(-0.002 * np.arange(n - 5))
    repair_q[n - 5:] = repair_q[n - 6] * np.exp(0.001 * (np.arange(5) + 1))
    repair = _ratio_theme(repair_q)
    assert repair["production"]["rs20"] < 0
    assert repair["production"]["rs5"] > 0
    assert repair["rotation_timeline"]["persistence"] == "repair"

    fade_q = np.empty(n)
    fade_q[:n - 5] = np.exp(0.002 * np.arange(n - 5))
    fade_q[n - 5:] = fade_q[n - 6] * np.exp(-0.001 * (np.arange(5) + 1))
    fade = _ratio_theme(fade_q)
    assert fade["production"]["rs20"] > 0
    assert fade["production"]["rs5"] < 0
    assert fade["rotation_timeline"]["persistence"] == "fading"

    lag_q = np.exp(-0.002 * t)
    lag = _ratio_theme(lag_q)
    assert lag["rotation_timeline"]["persistence"] == "lagging"


def test_classify_persistence_table():
    assert classify_persistence(False, 1, 2, 0.2) == "unavailable"
    assert classify_persistence(True, None, None, None) == "unavailable"
    assert classify_persistence(True, 1, 4, 0.2) == "gradual"
    assert classify_persistence(True, 4, 4, 0.9) == "event_spike"
    assert classify_persistence(True, -1, 4, -0.2) == "fading"
    assert classify_persistence(True, 1, -4, -0.2) == "repair"
    assert classify_persistence(True, -1, -4, 0.2) == "lagging"


def test_timeline_is_outside_production_so_replay_still_matches():
    n = 80
    dates = pd.bdate_range("2026-01-05", periods=n)
    t = np.arange(n)
    qqq = 100 * np.exp(0.0004 * t)
    etf = qqq * np.exp((np.log(1.02) / 20) * t)
    prices = pd.DataFrame({"QQQ": qqq, "ETF": etf}, index=dates)
    volume = _volume(prices)
    theme = Theme("etf", "ETF测试", "technology", "QQQ", proxy="ETF")
    rows = analyze(prices, volume, dates, (theme,))
    production = normalize_json_value(copy.deepcopy(rows[0]["production"]))
    attach_rotation_timeline(rows)
    assert normalize_json_value(rows[0]["production"]) == production
    assert rows[0]["production"]["priority"] == metric_frame(theme, prices, volume, strict=True).iloc[-1]["priority"]
    panel = normalize_json_value({
        "sessions": [str(item.date()) for item in dates],
        "price_columns": list(prices.columns), "volume_columns": list(volume.columns),
        "prices": prices.to_numpy().tolist(), "volumes": volume.to_numpy().tolist(),
    })
    snap = normalize_json_value({
        "schema_version": SCHEMA_VERSION, "source_session": str(dates[-1].date()),
        "amount_verified": False, "rows": rows, "input_panel": panel,
        "input_fingerprint": hashlib.sha256(encoded(panel)).hexdigest(),
    })
    assert replay_snapshot(snap)["status"] == "MATCH"
    assert "rank_rs20" not in snap["rows"][0]["production"]
    assert snap["rows"][0]["rotation_timeline"]["rank_rs20"] == 1


def test_legacy_analyze_has_no_timeline():
    n = 80
    dates = pd.bdate_range("2026-01-05", periods=n)
    t = np.arange(n)
    qqq = 100 * np.exp(0.0004 * t)
    prices = pd.DataFrame({"QQQ": qqq, "ETF": qqq * np.exp(0.001 * t)}, index=dates)
    theme = Theme("etf", "ETF测试", "technology", "QQQ", proxy="ETF")
    rows = analyze(prices, _volume(prices), dates, (theme,), schema_version=LEGACY_SCHEMA_VERSION)
    assert "rotation_timeline" not in rows[0]


def test_etf_overlay_does_not_invent_historical_breadth():
    n = 80
    dates = pd.bdate_range("2026-01-05", periods=n)
    t = np.arange(n)
    curve = 100 * np.exp(0.001 * t)
    prices = pd.DataFrame({"QQQ": 100.0, "ETF": curve, **{f"S{i}": curve for i in range(5)}}, index=dates)
    themes = (
        Theme("etf", "ETF测试", "technology", "QQQ", proxy="ETF"),
        Theme("basket", "篮子测试", "technology", "QQQ", members=tuple(f"S{i}" for i in range(5))),
    )
    rows = analyze(prices, _volume(prices), dates, themes)
    etf, basket = rows[0], rows[1]
    assert etf["rotation_timeline"]["breadth_source"] == "unavailable"
    assert etf["rotation_timeline"]["breadth_ago20"] is None
    assert basket["rotation_timeline"]["breadth_source"] == "production_history"
    assert basket["rotation_timeline"]["breadth_now"] == pytest.approx(100)
    assert basket["rotation_timeline"]["breadth_ago20"] == pytest.approx(100)
    data = [{"symbol": "ETF", "asset": f"S{i}", "isin": f"US{i}", "weightPercentage": 20,
             "updatedAt": "2026-04-20 00:00:00"} for i in range(5)]
    observation = normalize_observation(data, "ETF", "2026-04-20T00:00:00Z")
    frames = {f"S{i}": pd.DataFrame({"adj_close": prices[f"S{i}"]}, index=dates) for i in range(5)}
    before_priority = etf["production"]["priority"]
    attach_holdings_breadth(rows, {"ETF": observation}, frames, dates, now="2026-04-24T00:00:00Z")
    refresh_timeline_breadth(rows)
    assert rows[0]["production"]["priority"] == before_priority
    assert pd.isna(rows[0]["production"]["breadth"])
    assert rows[0]["rotation_timeline"]["breadth_source"] == "etf_holdings_current_only"
    assert rows[0]["rotation_timeline"]["breadth_now"] == pytest.approx(100)
    assert rows[0]["rotation_timeline"]["breadth_ago20"] is None
    assert rows[0]["rotation_timeline"]["breadth_change20"] is None
    refresh_timeline_breadth(rows)
    assert rows[0]["rotation_timeline"]["notes"].count("持仓观测无历史时点，不能比较20日前广度") == 1


def test_run_rotation_refreshes_etf_timeline_breadth_and_replays(tmp_path):
    import exchange_calendars as xcals
    from src.group_analytics.rotation.holdings import save_observation
    cal = xcals.get_calendar("XNYS")
    dates = cal.sessions_in_range("2025-01-02", "2026-09-08")
    close = 100 * np.exp(0.0001 * np.arange(len(dates)))
    frame = pd.DataFrame({"adj_close": close, "close": close, "volume": 10000}, index=dates)
    members = {f"S{i}": frame.copy() for i in range(5)}
    data = [{"symbol": "ETF", "asset": f"S{i}", "isin": f"US{i}", "weightPercentage": 20,
             "updatedAt": "2026-09-01 00:00:00"} for i in range(5)]
    save_observation(tmp_path / "holdings", normalize_observation(data, "ETF", "2026-09-01T00:00:00Z"))
    theme = Theme("etf", "测试", "technology", "QQQ", proxy="ETF")
    result = run_rotation(
        asof="2026-09-08", store=RotationStore(tmp_path / "out"),
        frames={"ETF": frame, "QQQ": frame, **members}, themes=[theme],
        now="2026-09-09T01:00:00Z", dry_run=True, holdings_root=tmp_path / "holdings",
        cache_root=tmp_path / "cache",
    )
    timeline = result["rows"][0]["rotation_timeline"]
    assert timeline["version"] == TIMELINE_VERSION
    assert timeline["breadth_source"] == "etf_holdings_current_only"
    assert timeline["breadth_ago20"] is None
    assert "rank_rs20" not in result["rows"][0]["production"]
    assert replay_snapshot(result)["status"] == "MATCH"
    assert any("解释字段" in note for note in result["notes"])


def test_format_and_discord_line():
    assert format_rotation_timeline_text(None) is None
    assert format_rotation_timeline_text({"persistence": "unavailable"}) is None
    text = format_rotation_timeline_text({
        "rank_rs20": 3, "rank_rs20_n": 11, "rank_rs20_ago20": 8,
        "persistence": "gradual", "persistence_label": "逐步跑赢",
    })
    assert text == "同组第3/11 · 20日前第8 · 逐步跑赢"
    n = 80
    dates = pd.bdate_range("2026-01-05", periods=n)
    t = np.arange(n)
    qqq = 100 * np.exp(0.0004 * t)
    prices = pd.DataFrame({"QQQ": qqq, "ETF": qqq * np.exp((np.log(1.04) / 20) * t)}, index=dates)
    theme = Theme("etf", "ETF测试", "technology", "QQQ", proxy="ETF")
    rows = analyze(prices, _volume(prices), dates, (theme,))
    rows[0]["production"]["action"] = "focus"
    payload = rotation_payload(
        {"schema_version": SCHEMA_VERSION, "source_session": str(dates[-1].date()),
         "generated_at": "2026-09-09T00:00:00+00:00", "run_id": "rot_20260515_aaaaaaaaaaaaaaaa",
         "valid_theme_count": 1, "total_theme_count": 1, "rows": rows,
         "context": {"label": "背景不足"}, "candidate_linkage": {"status": "unavailable"}},
        SimpleNamespace(target_session="2026-09-09"),
        PremarketDigestSettings(dashboard_base_url="https://example.com"),
    )
    body = payload["embeds"][0]["fields"][0]["value"]
    assert "同组第1/1" in body
    assert "逐步跑赢" in body


def test_validation_module_does_not_import_timeline():
    text = Path("src/group_analytics/rotation/validation.py").read_text(encoding="utf-8")
    assert "rotation_timeline" not in text
    assert "attach_rotation_timeline" not in text
