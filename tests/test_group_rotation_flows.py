import pandas as pd
import pytest

from src.group_analytics.rotation.engine import analyze
from src.group_analytics.rotation.flows import (
    attach_net_creation,
    compute_net_creation,
    flow_label,
)
from src.group_analytics.rotation.themes import Theme


def test_basket_net_creation_is_not_applicable():
    dates = pd.bdate_range("2026-01-05", periods=80)
    prices = pd.DataFrame({
        "QQQ": 100.0,
        "S0": 100.0, "S1": 101.0, "S2": 102.0, "S3": 103.0, "S4": 104.0,
    }, index=dates)
    volume = pd.DataFrame(1000.0, index=dates, columns=prices.columns)
    theme = Theme("basket", "篮子测试", "technology", "QQQ", members=tuple(f"S{i}" for i in range(5)))
    rows = analyze(prices, volume, dates, (theme,))
    attach_net_creation(rows)
    payload = rows[0]["net_creation"]
    assert payload["status"] == "not_applicable"
    assert payload["daily"] is None
    assert payload["amount_proxy"] is None
    assert payload["label"] is None


def test_etf_without_series_is_unavailable_and_does_not_copy_amount():
    dates = pd.bdate_range("2026-01-05", periods=80)
    curve = 100 * pd.Series([1.001 ** i for i in range(80)], index=dates)
    prices = pd.DataFrame({"QQQ": 100.0, "ETF": curve}, index=dates)
    volume = pd.DataFrame(1000.0, index=dates, columns=prices.columns)
    theme = Theme("etf", "ETF测试", "technology", "QQQ", proxy="ETF")
    rows = analyze(prices, volume, dates, (theme,), amount_verified=True)
    amount = rows[0]["production"]["amount_proxy"]
    attach_net_creation(rows)
    payload = rows[0]["net_creation"]
    assert payload["status"] == "unavailable"
    assert payload["daily"] is None
    assert payload["amount_proxy"] is None
    assert payload["daily"] != amount
    assert "成交额" in payload["note"] or "成交" in payload["note"]


def test_synthetic_delta_shares_times_nav_and_labels():
    dates = pd.bdate_range("2026-01-05", periods=25)
    shares = pd.Series(1e8, index=dates)
    shares.iloc[-1] = 1.01e8
    nav = pd.Series(100.0, index=dates)
    nav.iloc[-1] = 102.0
    frame = compute_net_creation(shares, nav)
    assert frame["net_creation"].iloc[-1] == pytest.approx(1e6 * 102.0)
    assert pd.isna(frame["net_creation"].iloc[0])
    assert flow_label(1.0, 1.0) == "价格涨+净流入"
    assert flow_label(1.0, -1.0) == "价格涨+净流出"
    assert flow_label(-1.0, 1.0) == "价格跌+净流入"
    assert flow_label(-1.0, -1.0) == "价格跌+净流出"
    rows = [{
        "id": "etf", "proxy": "ETF", "definition": {"members": ()},
        "production": {"abs1": 1.0, "amount_proxy": 999},
    }]
    attach_net_creation(rows, {"ETF": frame}, audit_status="PASSED")
    assert rows[0]["net_creation"]["status"] == "available"
    assert rows[0]["net_creation"]["daily"] == pytest.approx(1e6 * 102.0)
    assert rows[0]["net_creation"]["label"] == "价格涨+净流入"
    assert rows[0]["net_creation"]["amount_proxy"] is None
