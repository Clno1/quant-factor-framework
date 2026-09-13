"""Research arithmetic and leakage guards; not evidence of strategy returns."""
import numpy as np
import pandas as pd
import pytest

from src.group_analytics.rotation.themes import Theme
from src.group_analytics.rotation.validation import block_interval, make_panel, summarize


def fixture_data(n=160):
    dates = pd.bdate_range("2024-09-02", periods=n)
    frames = {}
    for symbol, rate in (("SMH", .002), ("QQQ", .001)):
        close = 100 * (1+rate)**np.arange(n)
        frames[symbol] = pd.DataFrame(
            {"adj_close": close, "close": close, "open": close * .99, "volume": 1000.},
            index=dates,
        )
    theme = Theme("semiconductors", "半导体", "technology", "QQQ", proxy="SMH")
    return dates,frames,(theme,)


def test_labels_next_open_and_horizon_close():
    dates,frames,themes = fixture_data()
    panel,_,_ = make_panel(frames,dates,themes)
    row=panel[(panel.date==dates[70]) & (panel.horizon==5)].iloc[0]
    expected=frames["SMH"].adj_close.iloc[75]/frames["SMH"].open.iloc[71]-1
    assert row.forward_return==pytest.approx(expected)
    assert row.exit_date==dates[75]
    assert panel.date.min()==dates[60]


@pytest.mark.parametrize("bad", [np.nan, 0, -1, np.inf])
def test_invalid_future_close_blocks_label(bad):
    dates,frames,themes=fixture_data()
    frames["SMH"].loc[dates[73],"adj_close"]=bad
    panel,_,_=make_panel(frames,dates,themes)
    assert panel[(panel.date==dates[70]) & (panel.horizon==5)].empty


def test_future_changes_do_not_change_signal():
    dates,frames,themes=fixture_data()
    before,_,_=make_panel(frames,dates,themes)
    frames["SMH"].loc[dates[71]:,"adj_close"] *= 2
    after,_,_=make_panel(frames,dates,themes)
    columns=["state","action","rs20","rs60","source_score"]
    pd.testing.assert_frame_equal(before.loc[before.date==dates[70],columns].reset_index(drop=True),
                                  after.loc[after.date==dates[70],columns].reset_index(drop=True))


def test_small_samples_never_have_confidence_interval():
    assert block_interval(np.ones(39),block=3) is None
    assert block_interval(np.ones(40),block=3)==[1.,1.]


def test_costs_cash_and_split_purge():
    dates,frames,themes=fixture_data()
    panel,prices,opens=make_panel(frames,dates,themes)
    report=summarize(panel,prices,opens,dates,themes)
    assert report["promotion"]=="NOT_APPROVED"
    low=next(p for p in report["portfolios"] if p["method"]=="rs20" and p["roundtrip_cost_bps"]==0)
    high=next(p for p in report["portfolios"] if p["method"]=="rs20" and p["roundtrip_cost_bps"]==50)
    assert low["rounds"]==high["rounds"]>0
    assert low["nav"]>high["nav"]
    assert all(r["date"] < "2024-12-04" or r["date"] >= "2025-01-01" for r in low["periods"])
    first=low["periods"][0]
    source=pd.Timestamp(first["date"])
    i=dates.get_loc(source)
    assert first["net_return"]==pytest.approx(.5*(prices.SMH.iloc[i+20]/opens.SMH.iloc[i+1]-1))
    assert high["max_drawdown"]<=0


def test_historical_current_holdings_forbidden():
    dates,frames,_=fixture_data()
    t=Theme("semiconductors","半导体","technology","QQQ",proxy="SMH",members=("NVDA",))
    with pytest.raises(ValueError,match="current-member"):
        make_panel(frames,dates,(t,))


def test_make_panel_uses_close_not_adj_for_amount():
    dates, frames, themes = fixture_data()
    frames["SMH"] = frames["SMH"].copy()
    frames["QQQ"] = frames["QQQ"].copy()
    frames["SMH"]["adj_close"] *= 2
    frames["QQQ"]["adj_close"] *= 2
    with_close, _, _ = make_panel(frames, dates, themes)
    no_close_frames = {
        symbol: frame.drop(columns=["close"]) for symbol, frame in frames.items()
    }
    without_close, _, _ = make_panel(no_close_frames, dates, themes)
    row_close = with_close[(with_close.date == dates[70]) & (with_close.horizon == 5)].iloc[0]
    row_none = without_close[(without_close.date == dates[70]) & (without_close.horizon == 5)].iloc[0]
    assert row_close.source_score != row_none.source_score
