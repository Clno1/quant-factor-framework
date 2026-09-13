from __future__ import annotations

from pathlib import Path
import hashlib

import numpy as np
import pandas as pd
import pytest

from src.group_analytics.artifacts import normalize_json_value
from src.group_analytics.rotation import SCHEMA_VERSION
from src.group_analytics.rotation.cross_asset import (
    CROSS_ASSET_NOTE,
    CROSS_ASSET_VERSION,
    PROXIES,
    build_cross_asset,
    cross_asset_symbols,
    unavailable_cross_asset,
)
from src.group_analytics.rotation.engine import _return, analyze
from src.group_analytics.rotation.replay import replay_snapshot
from src.group_analytics.rotation.service import ROTATION_PARAMETERS, run_rotation
from src.group_analytics.rotation.store import RotationStore, encoded
from src.group_analytics.rotation.themes import Theme, default_themes, required_symbols


def _volume(prices):
    return pd.DataFrame(1000.0, index=prices.index, columns=prices.columns)


def _proxy_frame(dates, daily):
    close = 100 * np.exp(np.log(1 + daily) * np.arange(len(dates)))
    return pd.DataFrame({"adj_close": close, "close": close, "volume": 10000}, index=dates)


def test_parameters_document_cross_asset_version():
    assert ROTATION_PARAMETERS["cross_asset_version"] == CROSS_ASSET_VERSION == "etf-proxy-bar-v1"
    assert len(cross_asset_symbols()) == 6
    assert cross_asset_symbols() == ("TLT", "IEF", "UUP", "GLD", "USO", "DBC")
    assert "不是美元指数" in CROSS_ASSET_NOTE
    assert "现货" in CROSS_ASSET_NOTE
    assert "ETF代理" in CROSS_ASSET_NOTE


def test_proxies_are_not_theme_symbols():
    themes = default_themes()
    theme_symbols = set(required_symbols(themes))
    for symbol in cross_asset_symbols():
        assert symbol not in theme_symbols
    assert all("ETF 代理" in item["name"] for item in PROXIES)
    assert not any("指数" in item["name"] or "现货" in item["name"] for item in PROXIES)
    assert not any("指数" in item["observes"] or "现货" in item["observes"] for item in PROXIES)


def test_abs_windows_match_production_formula_and_do_not_bridge_gaps():
    dates = pd.bdate_range("2026-01-05", periods=80)
    tlt = 100 * np.exp((np.log(1.01) / 5) * np.arange(len(dates)))
    ief = 100 * np.ones(len(dates))
    prices = pd.DataFrame({
        "TLT": tlt,
        "IEF": ief,
        "UUP": tlt * 0.5,
        "GLD": tlt * 1.2,
        "USO": tlt * 0.8,
        "DBC": tlt * 0.9,
    }, index=dates)
    payload = build_cross_asset(prices, dates)
    assert payload["status"] == "available"
    assert payload["version"] == CROSS_ASSET_VERSION
    assert payload["available_count"] == 6
    tlt_item = next(item for item in payload["items"] if item["symbol"] == "TLT")
    expected5 = _return(pd.Series(tlt, index=dates).where(lambda s: s > 0), 5, True).iloc[-1]
    expected20 = _return(pd.Series(tlt, index=dates).where(lambda s: s > 0), 20, True).iloc[-1]
    expected60 = _return(pd.Series(tlt, index=dates).where(lambda s: s > 0), 60, True).iloc[-1]
    assert tlt_item["abs5"] == pytest.approx(expected5)
    assert tlt_item["abs20"] == pytest.approx(expected20)
    assert tlt_item["abs60"] == pytest.approx(expected60)
    assert tlt_item["abs5"] == pytest.approx((tlt[-1] / tlt[-6] - 1) * 100)
    gapped = prices.copy()
    gapped.loc[dates[-20], "TLT"] = np.nan
    gapped_item = next(item for item in build_cross_asset(gapped, dates)["items"] if item["symbol"] == "TLT")
    assert gapped_item["abs5"] == pytest.approx(expected5)
    assert gapped_item["abs60"] is None
    assert gapped_item["status"] == "available"


def test_missing_and_non_positive_bars_are_unavailable():
    dates = pd.bdate_range("2026-01-05", periods=80)
    prices = pd.DataFrame({"TLT": np.linspace(100, 110, len(dates))}, index=dates)
    payload = build_cross_asset(prices, dates)
    assert payload["available_count"] == 1
    missing = next(item for item in payload["items"] if item["symbol"] == "IEF")
    assert missing["status"] == "unavailable"
    assert missing["abs5"] is missing["abs20"] is missing["abs60"] is None
    zeroed = prices.copy()
    zeroed["TLT"] = 0.0
    assert build_cross_asset(zeroed, dates)["available_count"] == 0
    empty = unavailable_cross_asset()
    assert empty["status"] == "unavailable"
    assert len(empty["items"]) == 6
    assert all(item["status"] == "unavailable" for item in empty["items"])


def test_extra_proxy_columns_do_not_change_theme_production():
    dates = pd.bdate_range("2026-01-05", periods=80)
    t = np.arange(len(dates))
    qqq = 100 * np.exp(0.0004 * t)
    etf = qqq * np.exp((np.log(1.02) / 20) * t)
    tlt = 80 * np.exp(-0.0002 * t)
    base = pd.DataFrame({"QQQ": qqq, "ETF": etf}, index=dates)
    extra = base.copy()
    extra["TLT"] = tlt
    theme = Theme("etf", "ETF测试", "technology", "QQQ", proxy="ETF")
    volume_base = _volume(base)
    volume_extra = _volume(extra)
    left = analyze(base, volume_base, dates, (theme,))
    right = analyze(extra, volume_extra, dates, (theme,))
    assert normalize_json_value(left[0]["production"]) == normalize_json_value(right[0]["production"])
    assert "cross_asset" not in left[0]
    assert "abs5" in left[0]["production"]
    assert left[0]["id"] == "etf"


def test_engine_and_validation_do_not_import_cross_asset():
    engine = Path("src/group_analytics/rotation/engine.py").read_text(encoding="utf-8")
    validation = Path("src/group_analytics/rotation/validation.py").read_text(encoding="utf-8")
    module = Path("src/group_analytics/rotation/cross_asset.py").read_text(encoding="utf-8")
    assert "cross_asset" not in engine
    assert "build_cross_asset" not in engine
    assert "cross_asset" not in validation
    assert "assign_priority(" not in module
    assert "不是美元指数" in module
    assert "黄金现货" not in module


def test_run_rotation_records_proxies_and_replays_without_them(tmp_path):
    import exchange_calendars as xcals
    cal = xcals.get_calendar("XNYS")
    dates = cal.sessions_in_range("2025-01-02", "2026-09-08")
    theme = Theme("etf", "测试", "technology", "QQQ", proxy="ETF")
    etf = _proxy_frame(dates, 0.0002)
    qqq = _proxy_frame(dates, 0.0001)
    tlt = _proxy_frame(dates, 0.001)
    ief = _proxy_frame(dates, -0.0003)
    uup = _proxy_frame(dates, 0.0004)
    gld = _proxy_frame(dates, 0.0005)
    uso = _proxy_frame(dates, -0.0006)
    dbc = _proxy_frame(dates, 0.0007)
    with_proxies = run_rotation(
        asof="2026-09-08", store=RotationStore(tmp_path / "with"),
        frames={"ETF": etf, "QQQ": qqq, "TLT": tlt, "IEF": ief, "UUP": uup, "GLD": gld, "USO": uso, "DBC": dbc},
        themes=[theme], now="2026-09-09T01:00:00Z", dry_run=True,
    )
    bar = with_proxies["cross_asset"]
    assert bar["version"] == CROSS_ASSET_VERSION
    assert bar["available_count"] == 6
    assert with_proxies["parameters"]["cross_asset_version"] == CROSS_ASSET_VERSION
    tlt_item = next(item for item in bar["items"] if item["symbol"] == "TLT")
    expected = _return(tlt["adj_close"].where(lambda s: s > 0), 5, True).iloc[-1]
    assert tlt_item["abs5"] == pytest.approx(expected)
    assert "rank_rs20" not in with_proxies["rows"][0]["production"]
    assert "cross_asset" not in with_proxies["rows"][0]["production"]
    assert replay_snapshot(with_proxies)["status"] == "MATCH"
    assert any("ETF代理" in note for note in with_proxies["notes"])
    assert set(with_proxies["input_panel"]["price_columns"]) >= {"ETF", "QQQ", "TLT"}

    without = run_rotation(
        asof="2026-09-08", store=RotationStore(tmp_path / "without"),
        frames={"ETF": etf, "QQQ": qqq}, themes=[theme],
        now="2026-09-09T01:00:00Z", dry_run=True,
    )
    missing = without["cross_asset"]
    assert missing["available_count"] == 0
    assert all(item["status"] == "unavailable" for item in missing["items"])
    assert replay_snapshot(without)["status"] == "MATCH"
    assert without["rows"][0]["production"]["rs20"] == with_proxies["rows"][0]["production"]["rs20"]
    assert "TLT" not in without["input_panel"]["price_columns"]


def test_cross_asset_replay_panel_with_extra_columns_still_matches():
    dates = pd.bdate_range("2026-01-05", periods=80)
    t = np.arange(len(dates))
    qqq = 100 * np.exp(0.0004 * t)
    prices = pd.DataFrame({
        "QQQ": qqq,
        "ETF": qqq * np.exp((np.log(1.02) / 20) * t),
        "TLT": 90 * np.exp(-0.0001 * t),
    }, index=dates)
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
        "cross_asset": build_cross_asset(prices, dates),
    })
    assert replay_snapshot(snap)["status"] == "MATCH"
    assert snap["cross_asset"]["items"][0]["symbol"] == "TLT"


def test_summary_api_keeps_cross_asset_on_historical_run(tmp_path):
    from dataclasses import replace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from unittest.mock import patch

    import exchange_calendars as xcals
    import src.webapp.group_analytics_routes as routes

    cal = xcals.get_calendar("XNYS")
    dates = cal.sessions_in_range("2025-01-02", "2026-09-08")
    theme = Theme("etf", "测试", "technology", "QQQ", proxy="ETF")
    frames = {
        "ETF": _proxy_frame(dates, 0.0002),
        "QQQ": _proxy_frame(dates, 0.0001),
        "TLT": _proxy_frame(dates, 0.001),
    }
    store = RotationStore(tmp_path / "group_analytics" / "rotation")
    result = run_rotation(
        asof="2026-09-08", store=store, frames=frames, themes=[theme],
        now="2026-09-09T01:00:00Z",
    )
    app = FastAPI()
    app.include_router(routes.router)
    with patch.object(routes, "settings", replace(routes.settings, output_root=tmp_path)):
        client = TestClient(app)
        payload = client.get("/api/group-analytics/rotation").json()
        historical = client.get("/api/group-analytics/rotation", params={"run": result["run_id"]}).json()
        heat = client.get("/api/group-analytics/rotation/heatmap", params={"run": result["run_id"]}).json()
    assert payload["cross_asset"]["available_count"] == 1
    assert payload["cross_asset"]["items"][0]["name"] == "长债 ETF 代理"
    assert historical["cross_asset"]["available_count"] == 1
    assert "input_panel" not in payload
    assert heat["reason"] == "HISTORICAL_VIEW_FORBIDDEN"
