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
