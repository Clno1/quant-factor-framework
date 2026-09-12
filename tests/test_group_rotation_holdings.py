from pathlib import Path

import pandas as pd
import pytest

from src.group_analytics.rotation.engine import analyze
from src.group_analytics.rotation.holdings import (
    HOLDINGS_NOTE,
    attach_holdings_breadth,
    is_observation_stale,
    load_latest_observations,
    normalize_observation,
    observation_breadth,
    save_observation,
)
from src.group_analytics.rotation.replay import replay_snapshot
from src.group_analytics.rotation.service import run_rotation
from src.group_analytics.rotation.store import RotationStore
from src.group_analytics.rotation.themes import Theme, proxy_etf_symbols


def rows():
    return [{"symbol":"SMH","asset":s,"isin":"US"+s,"weightPercentage":20,
             "updatedAt":"2026-09-08 17:00:00"} for s in ("NVDA","AMD","TSM","AVGO","INTC")]


def test_update_timestamp_never_becomes_holding_effective_date():
    obs=normalize_observation(rows(),"SMH","2026-09-08T18:00:00Z")
    assert obs["holdings_effective_at"] is None
    assert not obs["point_in_time"]
    dates=pd.bdate_range("2026-08-01",periods=21)
    frames={m["ticker"]:pd.DataFrame({"adj_close":range(100,121)},index=dates) for m in obs["members"]}
    frames["AMD"].loc[dates[-2],"adj_close"]=float("nan")
    result=observation_breadth(obs,frames,dates)
    assert result["above_ma20_pct"]==100
    assert result["eligible_members"]==4
    assert result["member_coverage"]==.8
    assert not result["measurement_complete"]  # five minimum
    assert not result["production_eligible"]


@pytest.mark.parametrize("change", ["fund","duplicate","partial","negative"])
def test_reject_ambiguous_holdings(change):
    data=rows()
    if change=="fund": data[0]["symbol"]="SPY"
    elif change=="duplicate": data[0]["asset"]="AMD"
    elif change=="partial": data.pop()
    else: data[0]["weightPercentage"]=-1
    with pytest.raises(ValueError): normalize_observation(data,"SMH","2026-09-08T18:00:00Z")


def test_unmapped_weight_never_disappears():
    data=rows();data[-1]["asset"]="1234.T"
    obs=normalize_observation(data,"SMH","2026-09-08T18:00:00Z")
    assert len(obs["members"])==4
    assert obs["excluded"][0]["weight_pct"]==20


def test_seventeen_proxy_etfs_are_registered():
    symbols = proxy_etf_symbols()
    assert len(symbols) == 17
    assert "SMH" in symbols and "XLK" in symbols and "XLRE" in symbols


def _sample_frames(n=80):
    dates = pd.bdate_range("2026-01-05", periods=n)
    t = range(n)
    curve = 100 * pd.Series([1.001 ** i for i in t], index=dates)
    prices = pd.DataFrame({"QQQ": 100.0, "ETF": curve, **{f"S{i}": curve for i in range(5)}}, index=dates)
    volume = pd.DataFrame(1000.0, index=dates, columns=prices.columns)
    themes = (
        Theme("etf", "ETF测试", "technology", "QQQ", proxy="ETF"),
        Theme("basket", "篮子测试", "technology", "QQQ", members=tuple(f"S{i}" for i in range(5))),
    )
    return dates, prices, volume, themes


def _etf_observation(captured="2026-04-20T00:00:00Z"):
    data = [{"symbol": "ETF", "asset": f"S{i}", "isin": f"US{i}", "weightPercentage": 20,
             "updatedAt": "2026-04-20 00:00:00"} for i in range(5)]
    return normalize_observation(data, "ETF", captured)


def test_dual_calibre_overlay_does_not_enter_production_or_priority():
    dates, prices, volume, themes = _sample_frames()
    rows = analyze(prices, volume, dates, themes)
    before = rows[0]["production"]["priority"]
    assert pd.isna(rows[0]["production"]["breadth"])
    frames = {f"S{i}": pd.DataFrame({"adj_close": prices[f"S{i}"]}, index=dates) for i in range(5)}
    attach_holdings_breadth(
        rows, {"ETF": _etf_observation()}, frames, dates, now="2026-04-24T00:00:00Z",
    )
    overlay = rows[0]["holdings_breadth"]
    assert overlay["breadth_kind"] == "etf_holdings_observation"
    assert overlay["point_in_time"] is False
    assert overlay["holdings_effective_at"] is None
    assert overlay["note"] == HOLDINGS_NOTE
    assert overlay["breadth_equal_weight_pct"] == pytest.approx(100)
    assert overlay["breadth_weighted_pct"] == pytest.approx(100)
    assert pd.isna(rows[0]["production"]["breadth"])
    assert rows[0]["production"]["priority"] == before
    assert "ETF_HOLDINGS_NOT_LINKED" in rows[0]["production"]["evidence_gaps"]
    assert "LOW_PARTICIPATION" not in overlay["observation_gaps"]
    assert rows[1]["holdings_breadth"]["breadth_kind"] == "member_above_ma"


def test_low_participation_is_observation_gap_only():
    dates, prices, volume, themes = _sample_frames()
    # Pull the last prints below MA20 while keeping history valid.
    for i in range(5):
        prices.loc[dates[-1], f"S{i}"] = prices[f"S{i}"].iloc[-25]
    rows = analyze(prices, volume, dates, (themes[0],))
    frames = {f"S{i}": pd.DataFrame({"adj_close": prices[f"S{i}"]}, index=dates) for i in range(5)}
    attach_holdings_breadth(rows, {"ETF": _etf_observation()}, frames, dates, now="2026-04-24T00:00:00Z")
    assert rows[0]["holdings_breadth"]["breadth_equal_weight_pct"] == pytest.approx(0)
    assert "LOW_PARTICIPATION" in rows[0]["holdings_breadth"]["observation_gaps"]
    assert "LOW_PARTICIPATION" not in rows[0]["production"]["evidence_gaps"]


def test_stale_observation_is_unused(tmp_path):
    obs = _etf_observation("2026-04-01T00:00:00Z")
    save_observation(tmp_path, obs)
    loaded = load_latest_observations(tmp_path, ["ETF"])
    assert "ETF" in loaded
    assert is_observation_stale(loaded["ETF"], "2026-04-16T00:00:00Z")
    dates, prices, volume, themes = _sample_frames()
    rows = analyze(prices, volume, dates, (themes[0],))
    frames = {f"S{i}": pd.DataFrame({"adj_close": prices[f"S{i}"]}, index=dates) for i in range(5)}
    attach_holdings_breadth(rows, loaded, frames, dates, now="2026-04-16T00:00:00Z")
    assert rows[0]["holdings_breadth"]["status"] == "HOLDINGS_OBSERVATION_STALE"
    assert rows[0]["holdings_breadth"]["breadth_equal_weight_pct"] is None
    assert rows[0]["breadth_kind"] == "unavailable"
    assert pd.isna(rows[0]["production"]["breadth"])


def test_missing_observation_keeps_unlinked_gap():
    dates, prices, volume, themes = _sample_frames()
    rows = analyze(prices, volume, dates, (themes[0],))
    attach_holdings_breadth(rows, {}, {}, dates, now="2026-04-24T00:00:00Z")
    assert rows[0]["holdings_breadth"]["status"] == "ETF_HOLDINGS_NOT_LINKED"
    assert "ETF_HOLDINGS_NOT_LINKED" in rows[0]["production"]["evidence_gaps"]


def test_run_rotation_overlay_still_replays(tmp_path):
    import numpy as np
    import exchange_calendars as xcals
    cal = xcals.get_calendar("XNYS")
    dates = cal.sessions_in_range("2025-01-02", "2026-09-08")
    close = 100 * np.exp(.0001 * np.arange(len(dates)))
    frame = pd.DataFrame({"adj_close": close, "close": close, "volume": 10000}, index=dates)
    members = {f"S{i}": frame.copy() for i in range(5)}
    obs = _etf_observation("2026-09-01T00:00:00Z")
    save_observation(tmp_path / "holdings", obs)
    theme = Theme("etf", "测试", "technology", "QQQ", proxy="ETF")
    result = run_rotation(
        asof="2026-09-08", store=RotationStore(tmp_path / "out"),
        frames={"ETF": frame, "QQQ": frame, **members}, themes=[theme],
        now="2026-09-09T01:00:00Z", dry_run=True, holdings_root=tmp_path / "holdings",
        cache_root=tmp_path / "cache",
    )
    assert result["rows"][0]["holdings_breadth"]["breadth_kind"] == "etf_holdings_observation"
    assert result["rows"][0]["holdings_breadth"]["point_in_time"] is False
    assert pd.isna(result["rows"][0]["production"]["breadth"])
    assert replay_snapshot(result)["status"] == "MATCH"


def test_validation_module_does_not_import_holdings():
    text = Path("src/group_analytics/rotation/validation.py").read_text(encoding="utf-8")
    assert "observation_breadth" not in text
    assert "holdings_breadth" not in text
    assert "current-member" in text


def rows():
    return [{"symbol":"SMH","asset":s,"isin":"US"+s,"weightPercentage":20,
             "updatedAt":"2026-09-08 17:00:00"} for s in ("NVDA","AMD","TSM","AVGO","INTC")]


def test_update_timestamp_never_becomes_holding_effective_date():
    obs=normalize_observation(rows(),"SMH","2026-09-08T18:00:00Z")
    assert obs["holdings_effective_at"] is None
    assert not obs["point_in_time"]
    dates=pd.bdate_range("2026-08-01",periods=21)
    frames={m["ticker"]:pd.DataFrame({"adj_close":range(100,121)},index=dates) for m in obs["members"]}
    frames["AMD"].loc[dates[-2],"adj_close"]=float("nan")
    result=observation_breadth(obs,frames,dates)
    assert result["above_ma20_pct"]==100
    assert result["eligible_members"]==4
    assert result["member_coverage"]==.8
    assert not result["measurement_complete"]  # five minimum
    assert not result["production_eligible"]


@pytest.mark.parametrize("change", ["fund","duplicate","partial","negative"])
def test_reject_ambiguous_holdings(change):
    data=rows()
    if change=="fund": data[0]["symbol"]="SPY"
    elif change=="duplicate": data[0]["asset"]="AMD"
    elif change=="partial": data.pop()
    else: data[0]["weightPercentage"]=-1
    with pytest.raises(ValueError): normalize_observation(data,"SMH","2026-09-08T18:00:00Z")


def test_unmapped_weight_never_disappears():
    data=rows();data[-1]["asset"]="1234.T"
    obs=normalize_observation(data,"SMH","2026-09-08T18:00:00Z")
    assert len(obs["members"])==4
    assert obs["excluded"][0]["weight_pct"]==20
