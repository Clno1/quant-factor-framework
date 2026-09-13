from __future__ import annotations

import copy
from pathlib import Path
import hashlib

import numpy as np
import pandas as pd
import pytest

from src.group_analytics.artifacts import normalize_json_value
from src.group_analytics.rotation import LEGACY_SCHEMA_VERSION, SCHEMA_VERSION
from src.group_analytics.rotation.engine import analyze, classify_axes
from src.group_analytics.rotation.replay import replay_snapshot
from src.group_analytics.rotation.service import ROTATION_PARAMETERS, run_rotation
from src.group_analytics.rotation.store import RotationStore, encoded
from src.group_analytics.rotation.themes import Theme
from src.group_analytics.rotation.trail import TRAIL_BARS, TRAIL_VERSION, attach_rotation_trail


def _volume(prices):
    return pd.DataFrame(1000.0, index=prices.index, columns=prices.columns)


def test_parameters_document_trail_version():
    assert ROTATION_PARAMETERS["rotation_trail_version"] == TRAIL_VERSION
    assert ROTATION_PARAMETERS["trail_bars"] == TRAIL_BARS == 50


def test_trail_matches_history_axes_and_stays_off_production():
    n = 100
    dates = pd.bdate_range("2026-01-05", periods=n)
    t = np.arange(n)
    qqq = 100 * np.exp(0.0004 * t)
    prices = pd.DataFrame({
        "QQQ": qqq,
        "AAA": qqq * np.exp((np.log(1.04) / 20) * t),
        "BBB": qqq * np.exp((np.log(1.01) / 20) * t),
    }, index=dates)
    themes = (
        Theme("aaa", "强势科技", "technology", "QQQ", proxy="AAA"),
        Theme("bbb", "弱势科技", "technology", "QQQ", proxy="BBB"),
    )
    rows = analyze(prices, _volume(prices), dates, themes)
    production = normalize_json_value(copy.deepcopy(rows[0]["production"]))
    attach_rotation_trail(rows)
    assert normalize_json_value(rows[0]["production"]) == production
    trail = rows[0]["rotation_trail"]
    assert trail["version"] == TRAIL_VERSION
    assert trail["window_bars"] == 50
    assert "非RRG" in trail["note"]
    assert len(trail["points"]) == 50
    history = rows[0]["history"][-50:]
    for point, bar in zip(trail["points"], history):
        assert point["date"] == bar["date"]
        if bar.get("history_valid") and np.isfinite(bar.get("strength_log")):
            assert point["strength_log"] == pytest.approx(bar["strength_log"])
            assert point["acceleration_log"] == pytest.approx(bar["acceleration_log"])
        else:
            assert point["strength_log"] is None
    x, y, _, _ = classify_axes(rows[0]["production"]["rs5"], rows[0]["production"]["rs20"])
    assert trail["current"]["strength_log"] == pytest.approx(x)
    assert trail["current"]["acceleration_log"] == pytest.approx(y)
    assert "rotation_trail" not in rows[0]["production"]
    assert rows[0]["rotation_trail"]["current"]["strength_log"] != rows[1]["rotation_trail"]["current"]["strength_log"]


def test_legacy_analyze_has_no_trail():
    n = 80
    dates = pd.bdate_range("2026-01-05", periods=n)
    t = np.arange(n)
    qqq = 100 * np.exp(0.0004 * t)
    prices = pd.DataFrame({"QQQ": qqq, "ETF": qqq * np.exp(0.001 * t)}, index=dates)
    theme = Theme("etf", "ETF测试", "technology", "QQQ", proxy="ETF")
    rows = analyze(prices, _volume(prices), dates, (theme,), schema_version=LEGACY_SCHEMA_VERSION)
    assert "rotation_trail" not in rows[0]


def test_trail_replay_still_matches():
    n = 80
    dates = pd.bdate_range("2026-01-05", periods=n)
    t = np.arange(n)
    qqq = 100 * np.exp(0.0004 * t)
    prices = pd.DataFrame({"QQQ": qqq, "ETF": qqq * np.exp((np.log(1.02) / 20) * t)}, index=dates)
    volume = _volume(prices)
    theme = Theme("etf", "ETF测试", "technology", "QQQ", proxy="ETF")
    rows = analyze(prices, volume, dates, (theme,))
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
    assert "rotation_trail" in snap["rows"][0]
    assert "strength_log" in snap["rows"][0]["production"]


def test_run_rotation_records_trail_and_replays(tmp_path):
    import exchange_calendars as xcals
    cal = xcals.get_calendar("XNYS")
    dates = cal.sessions_in_range("2025-01-02", "2026-09-08")
    close = 100 * np.exp(0.0001 * np.arange(len(dates)))
    frame = pd.DataFrame({"adj_close": close, "close": close, "volume": 10000}, index=dates)
    theme = Theme("etf", "测试", "technology", "QQQ", proxy="ETF")
    result = run_rotation(
        asof="2026-09-08", store=RotationStore(tmp_path),
        frames={"ETF": frame, "QQQ": frame}, themes=[theme],
        now="2026-09-09T01:00:00Z", dry_run=True,
    )
    trail = result["rows"][0]["rotation_trail"]
    assert trail["version"] == TRAIL_VERSION
    assert trail["window_bars"] == 50
    assert result["parameters"]["rotation_trail_version"] == TRAIL_VERSION
    assert "rank_rs20" not in result["rows"][0]["production"]
    assert replay_snapshot(result)["status"] == "MATCH"
    assert any("非RRG" in note for note in result["notes"])


def test_validation_module_does_not_import_trail():
    text = Path("src/group_analytics/rotation/validation.py").read_text(encoding="utf-8")
    assert "rotation_trail" not in text
    assert "attach_rotation_trail" not in text
