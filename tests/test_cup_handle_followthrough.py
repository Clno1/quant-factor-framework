import pandas as pd
import pytest

from src.breakouts.live.cup_handle_followthrough import assess_followthrough, summarize_followthrough


@pytest.fixture
def sample():
    signal = {"session_date": "2026-09-08", "price": 100, "pattern": {"handle_low": 98},
              "bar_timestamp": "2026-09-08T10:15:00-04:00", "triggered_at": "2026-09-08T10:20:08-04:00"}
    rows = [{"date": str(t), "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100}
            for t in pd.date_range("2026-09-08 10:20", periods=30, freq="min")]
    return signal, rows


def test_complete_no_target_is_false_proxy(sample):
    result = assess_followthrough(*sample)
    assert result["status"] == "TARGET_NOT_REACHED"
    assert result["false_positive_proxy"] is True
    assert len(result["bars"]) == 6


@pytest.mark.parametrize("field,value,status", [("high", 102, "TARGET_REACHED"), ("low", 98, "STOP_FIRST")])
def test_barriers(sample, field, value, status):
    sample[1][1][field] = value
    assert assess_followthrough(*sample)["status"] == status


@pytest.mark.parametrize("change,reason", [
    ("missing", "INCOMPLETE_CONTIGUOUS_HORIZON"), ("duplicate", "DUPLICATE_MINUTE"),
    ("zero", "NONPOSITIVE_OHLCV"), ("negative", "NONPOSITIVE_OHLCV"),
    ("nan", "NONFINITE_OHLCV"), ("order", "INVALID_OHLC_ORDER"),
    ("trigger", "TRIGGER_MINUTE_ORDER_UNKNOWN"), ("both", "SAME_MINUTE_BARRIER_ORDER_UNKNOWN"),
])
def test_uncertainty_does_not_become_success(sample, change, reason):
    signal, rows = sample
    if change == "missing": rows.pop(3)
    elif change == "duplicate": rows.append(dict(rows[3]))
    elif change == "zero": rows[3]["volume"] = 0
    elif change == "negative": rows[3]["volume"] = -1
    elif change == "nan": rows[3]["volume"] = float("nan")
    elif change == "order": rows[3]["close"] = 102
    elif change == "trigger": rows[0]["high"] = 103
    elif change == "both": rows[1].update(low=97, high=103)
    result = assess_followthrough(signal, rows)
    assert result["reason"] == reason
    assert result["false_positive_proxy"] is None


def test_missing_last_bar_even_with_early_target_stays_unresolved(sample):
    sample[1][1]["high"] = 103
    sample[1].pop()
    assert assess_followthrough(*sample)["false_positive_proxy"] is None


def test_zero_signals_and_partial_denominator():
    empty = summarize_followthrough([])
    assert empty["false_positive_proxy_pct_all"] is None
    mixed = summarize_followthrough([{"false_positive_proxy": False}, {"false_positive_proxy": None}])
    assert mixed["false_positive_proxy_pct_all"] is None
    assert mixed["false_positive_proxy_pct_resolved"] == 0


@pytest.mark.parametrize("field,value", [
    ("triggered_at", "2026-09-08T10:19:58-04:00"),
    ("bar_timestamp", "2026-09-08T10:16:00-04:00"),
    ("session_date", "2026-09-09"), ("price", float("inf")),
])
def test_invalid_signal_cannot_produce_rate(sample, field, value):
    sample[0][field] = value
    assert assess_followthrough(*sample)["false_positive_proxy"] is None
