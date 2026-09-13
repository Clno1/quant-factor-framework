from unittest.mock import Mock

import pandas as pd

from scripts.diagnose_cup_handle_data_gaps import audit_provider_gaps


def gap():
    return {
        "session_date": "2026-09-08", "ticker": "TEN", "algorithm_version": "v3",
        "gap_start": "2026-09-08 10:15:00", "gap_end": "2026-09-08 10:20:00",
        "classification": "UNRESOLVED_SOURCE_GAP",
    }


def bars(at):
    return pd.DataFrame({"open": [10], "high": [11], "low": [9], "close": [10], "volume": [100]}, index=pd.to_datetime([at]))


def test_missing_both_intervals_is_not_confirmed_no_trade():
    fetch = Mock(return_value=bars("2026-09-08 10:20"))
    result = audit_provider_gaps([gap()], fetch)
    assert fetch.call_count == 2
    assert result["results"][0]["requery_status"] == "STILL_ABSENT_BOTH_INTERVALS"
    assert result["results"][0]["classification"] == "UNRESOLVED_SOURCE_GAP"
    assert result["responses"][0]["normalized_response_sha256"]


def test_late_provider_rows_do_not_change_historical_classification():
    result = audit_provider_gaps([gap()], Mock(return_value=bars("2026-09-08 10:16")))
    assert result["results"][0]["requery_status"] == "ROWS_PRESENT_ON_REQUERY"
    assert result["results"][0]["classification"] == "UNRESOLVED_SOURCE_GAP"
    assert result["historical_observation_unchanged"] is True


def test_failed_or_empty_requests_are_inconclusive_without_leaking_secrets():
    result = audit_provider_gaps([gap()], Mock(side_effect=[RuntimeError("apikey=secret"), None]))
    assert result["results"][0]["requery_status"] == "INCONCLUSIVE_REQUEST"
    assert "secret" not in str(result)


def test_requery_groups_by_session_as_well_as_ticker():
    second = {**gap(), "session_date": "2026-09-09", "gap_start": "2026-09-09 10:15:00", "gap_end": "2026-09-09 10:20:00"}
    fetch = Mock(side_effect=lambda ticker, interval, start, end: bars(start + " 10:15"))
    result = audit_provider_gaps([gap(), second], fetch)
    assert fetch.call_count == 4
    assert all(row["row_counts"] == {"1min": 1, "5min": 1} for row in result["results"])


def test_interval_mismatch_is_evidence_not_an_automatic_gap_reclassification():
    minute = bars("2026-09-08 10:15")
    native = minute.copy()
    native["volume"] = 200
    report = audit_provider_gaps([gap()], Mock(side_effect=[minute, native]))
    row = report["results"][0]
    assert row["bucket_evidence"]["comparison"] == "OHLCV_DISAGREEMENT"
    assert set(row["bucket_evidence"]["differences"]) == {"volume"}
    assert row["classification"] == "UNRESOLVED_SOURCE_GAP"
    assert row["bucket_evidence"]["can_confirm_provider_gap"] is False
    assert report["counts_for_shadow_promotion"] is False


def test_zero_volume_and_duplicates_are_not_hidden_in_comparison():
    frame = bars("2026-09-08 10:15")
    frame["volume"] = 0
    duplicate = pd.concat([frame, frame])
    row = audit_provider_gaps([gap()], Mock(side_effect=[duplicate, frame]))["results"][0]
    one = row["bucket_evidence"]["intervals"]["1min"]
    assert one["duplicate_timestamps"] == 1
    assert one["nonpositive_volume_rows"] == 2
    assert row["bucket_evidence"]["comparison"] == "NO_COMPARABLE_PAIR"
    assert row["bucket_evidence"]["can_confirm_no_trade"] is False


def test_empty_frame_is_inconclusive_not_a_comparison_crash():
    row = audit_provider_gaps([gap()], Mock(return_value=pd.DataFrame()))["results"][0]
    assert row["requery_status"] == "INCONCLUSIVE_REQUEST"
    assert row["bucket_evidence"]["comparison"] == "NO_COMPARABLE_PAIR"
