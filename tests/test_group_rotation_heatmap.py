from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.data.security_master import CLASSIFICATION_POLICY
from src.group_analytics.rotation.heatmap import (
    HEATMAP_NOTE,
    HEATMAP_VERSION,
    build_coverage_heatmap,
    load_coverage_heatmap,
    unavailable,
)
from src.group_analytics.rotation.engine import analyze
from src.group_analytics.rotation.replay import replay_snapshot
from src.group_analytics.rotation.themes import Theme
from src.group_analytics.artifacts import normalize_json_value
import hashlib
from src.group_analytics.rotation import SCHEMA_VERSION
from src.group_analytics.rotation.store import encoded


def _members():
    return pd.DataFrame({
        "ticker": ["AAA", "BBB", "CCC", "QQQ"],
        "name": ["Alpha", "Beta", "NoCap", "QQQ"],
        "sector": ["Technology", "Technology", "Energy", "Benchmark"],
        "sub_industry": ["Semis", "Software", "Oil", "ETF"],
        "market_cap": [80.0, 20.0, np.nan, 1.0],
        "is_current_member": [True, True, True, True],
        "coverage_role": ["EQUITY", "EQUITY", "EQUITY", "BENCHMARK_ONLY"],
    })


def _prices(dates):
    rows = []
    for i, session in enumerate(dates):
        t = i
        rows.extend([
            {"date": session, "ticker": "AAA", "adj_close": 100 * (1.02 ** t)},
            {"date": session, "ticker": "BBB", "adj_close": 100 * (0.99 ** t)},
            {"date": session, "ticker": "CCC", "adj_close": 50.0},
            {"date": session, "ticker": "QQQ", "adj_close": 400.0},
        ])
    return pd.DataFrame(rows)


def test_cap_weighted_tree_reconciles_and_stays_off_production():
    dates = pd.bdate_range("2026-01-05", periods=25)
    payload = build_coverage_heatmap(_members(), _prices(dates), source_session=dates[-1])
    assert payload["status"] == "available"
    assert payload["version"] == HEATMAP_VERSION
    assert payload["classification_policy"] == CLASSIFICATION_POLICY
    assert payload["pit_safe_for_history"] is False
    assert HEATMAP_NOTE in payload["notes"]
    counts = payload["counts"]
    assert counts["coverage_current"] == 4
    assert counts["benchmark_only"] == 1
    assert counts["eligible"] == 3
    assert counts["eligible"] + counts["benchmark_only"] == counts["coverage_current"]
    for window, row in counts["windows"].items():
        assert row["in_tree"] + row["no_price"] + row["no_market_cap"] == counts["eligible"]
        assert row["no_market_cap"] == 1
        assert row["in_tree"] == 2
    tech = next(sector for sector in payload["sectors"] if sector["name"] == "Technology")
    expected_1d = (80 * (1.02 - 1) + 20 * (0.99 - 1)) / 100
    assert tech["ret1"] == pytest.approx(expected_1d)
    assert tech["n1"] == 2
    semis = next(item for item in tech["industries"] if item["name"] == "Semis")
    assert semis["stocks"][0]["ticker"] == "AAA"
    assert semis["stocks"][0]["href"] == "/breakouts/AAA"
    assert semis["stocks"][0]["ret1"] == pytest.approx(0.02)
    assert all(sector["name"] != "Benchmark" for sector in payload["sectors"])
    assert "production" not in payload


def test_unknown_classification_stays_in_tree():
    members = pd.DataFrame({
        "ticker": ["ZZZ"],
        "sector": [None],
        "sub_industry": [None],
        "market_cap": [10.0],
        "is_current_member": [True],
    })
    dates = pd.bdate_range("2026-06-01", periods=5)
    prices = pd.DataFrame({
        "date": list(dates) + list(dates),
        "ticker": ["ZZZ"] * 5 + ["AAA"] * 5,
        "adj_close": [10, 10, 10, 10, 11] + [1, 1, 1, 1, 1],
    })
    payload = build_coverage_heatmap(members, prices, source_session=dates[-1])
    assert payload["sectors"][0]["name"] == "UNKNOWN"
    assert payload["sectors"][0]["industries"][0]["name"] == "UNKNOWN"


def test_historical_and_session_mismatch_fail_closed():
    forbidden = load_coverage_heatmap(historical=True, source_session="2026-09-08")
    assert forbidden["status"] == "unavailable"
    assert forbidden["reason"] == "HISTORICAL_VIEW_FORBIDDEN"
    mismatch = load_coverage_heatmap(source_session="2000-01-01", now=pd.Timestamp("2026-09-09", tz="UTC"))
    assert mismatch["reason"] == "SESSION_MISMATCH"
    assert mismatch["pit_safe_for_history"] is False


def test_missing_catalog_is_unavailable():
    class Missing:
        def require_latest(self, *args, **kwargs):
            from src.data.foundation import NoPublishedDataError
            raise NoPublishedDataError("none")

    payload = load_coverage_heatmap(
        source_session="2026-09-08",
        now=pd.Timestamp("2026-09-09T01:00:00Z"),
        reader=Missing(),
    )
    assert payload["reason"] == "COVERAGE_NOT_PUBLISHED"


def test_engine_and_replay_do_not_gain_heatmap():
    dates = pd.bdate_range("2026-01-05", periods=80)
    t = np.arange(len(dates))
    qqq = 100 * np.exp(0.0004 * t)
    prices = pd.DataFrame({"QQQ": qqq, "ETF": qqq * np.exp(0.001 * t)}, index=dates)
    volume = pd.DataFrame(1000.0, index=prices.index, columns=prices.columns)
    theme = Theme("etf", "ETF测试", "technology", "QQQ", proxy="ETF")
    rows = analyze(prices, volume, dates, (theme,))
    assert "coverage_heatmap" not in rows[0]
    assert "heatmap" not in rows[0]["production"]
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
    engine = Path("src/group_analytics/rotation/engine.py").read_text(encoding="utf-8")
    service = Path("src/group_analytics/rotation/service.py").read_text(encoding="utf-8")
    validation = Path("src/group_analytics/rotation/validation.py").read_text(encoding="utf-8")
    assert "heatmap" not in engine
    assert "heatmap" not in service
    assert "heatmap" not in validation


def test_unavailable_helper_keeps_policy_labels():
    payload = unavailable("PRICE_GAP", source_session="2026-09-08")
    assert payload["classification_policy"] == CLASSIFICATION_POLICY
    assert payload["sectors"] == []
    assert payload["status"] == "unavailable"
