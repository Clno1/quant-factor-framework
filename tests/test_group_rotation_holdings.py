import pandas as pd
import pytest

from src.group_analytics.rotation.holdings import normalize_observation, observation_breadth


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
