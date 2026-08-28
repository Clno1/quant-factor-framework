from scripts.run_core_market_data import (
    _decode_json,
    _result_target_session,
    _semantic_drift_failures,
)


def test_only_exact_semantic_drift_is_recoverable():
    payload = {
        "failures": ["SP500", "MAG7"],
        "results": [
            {
                "universe": "SP500",
                "status": "FAILED",
                "error": (
                    "ATO: non-uniform adj_close revision in overlap window; "
                    "run a full rebuild"
                ),
            },
            {
                "universe": "MAG7",
                "status": "FAILED",
                "error": (
                    "AAPL: non-uniform volume revision in overlap window; "
                    "run a full rebuild"
                ),
            },
        ],
    }
    assert _semantic_drift_failures(payload) == ["MAG7", "SP500"]


def test_provider_failure_never_triggers_full_rebuild():
    payload = {
        "failures": ["SP500"],
        "results": [
            {
                "universe": "SP500",
                "status": "FAILED",
                "error": "FMP request timed out after 6 attempts",
            }
        ],
    }
    assert _semantic_drift_failures(payload) == []


def test_mixed_failure_set_never_partially_recovers():
    payload = {
        "failures": ["SP500", "NASDAQ100"],
        "results": [
            {
                "universe": "SP500",
                "status": "FAILED",
                "error": "non-uniform close revision; run a full rebuild",
            },
            {
                "universe": "NASDAQ100",
                "status": "FAILED",
                "error": "PIT membership hash mismatch",
            },
        ],
    }
    assert _semantic_drift_failures(payload) == []


def test_result_target_session_requires_one_consistent_session():
    assert _result_target_session(
        {
            "results": [
                {"universe": "SP500", "target_session": "2026-08-25"},
                {"universe": "MAG7", "target_session": "2026-08-25"},
            ]
        }
    ) == "2026-08-25"
    assert _result_target_session(
        {
            "results": [
                {"universe": "SP500", "target_session": "2026-08-25"},
                {"universe": "MAG7", "target_session": "2026-08-24"},
            ]
        }
    ) is None


def test_decode_json_accepts_streamed_logs_before_final_result():
    payload = _decode_json(
        "starting rebuild\nprogress 1/3\n"
        '{"results":[{"universe":"SP500"}],"failures":[]}\n'
    )
    assert payload == {
        "results": [{"universe": "SP500"}],
        "failures": [],
    }
