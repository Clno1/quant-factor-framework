from __future__ import annotations

import pandas as pd
import pytest

from src.data.sp500_pit import apply_sp500_pit_corrections
from src.market_regime_research.models import DataContractError
from src.market_regime_research.pit import reconstruct_sp500_snapshots


def _registry() -> dict:
    return {
        "schema_version": 1,
        "universe": "SP500",
        "event_ticker_corrections": [
            {
                "id": "solstice",
                "effective_date": "2025-10-31",
                "field": "added_ticker",
                "provider_value": "SOLSV",
                "corrected_value": "SOLS",
                "security_contains": "Solstice Advanced Materials",
                "sources": ["https://example.test/solstice"],
            }
        ],
        "symbol_transitions": [
            {
                "id": "echostar",
                "effective_date": "2026-06-24",
                "removed_ticker": "SATS",
                "added_ticker": "ECHO",
                "security_name": "EchoStar Corporation",
                "sources": ["https://example.test/echostar"],
            }
        ],
    }


def _raw_changes() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2026-03-23",
                "symbol": "SATS",
                "addedSecurity": "EchoStar Corporation",
                "removedTicker": "PAYC",
                "removedSecurity": "Paycom Software",
                "reason": "rebalance",
            },
            {
                "date": "2025-12-22",
                "symbol": "CVNA",
                "addedSecurity": "Carvana Co.",
                "removedTicker": "SOLS",
                "removedSecurity": "Solstice Advanced Materials",
                "reason": "rebalance",
            },
            {
                "date": "2025-10-31",
                "symbol": "SOLSV",
                "addedSecurity": (
                    "Solstice Advanced Materials Inc. Common Stock When Issued"
                ),
                "removedTicker": "KMX",
                "removedSecurity": "CarMax Inc.",
                "reason": "replacement",
            },
        ]
    )


def test_reviewed_corrections_make_main_window_strictly_reconstructable():
    corrected, audit = apply_sp500_pit_corrections(
        _raw_changes(),
        _registry(),
        asof="2026-07-31",
    )

    solstice = corrected.loc[
        corrected["effective_date"].eq(pd.Timestamp("2025-10-31"))
    ].iloc[0]
    transition = corrected.loc[
        corrected["effective_date"].eq(pd.Timestamp("2026-06-24"))
    ].iloc[0]
    assert solstice["added_ticker"] == "SOLS"
    assert transition["added_ticker"] == "ECHO"
    assert transition["removed_ticker"] == "SATS"
    assert {item["action"] for item in audit} == {
        "corrected",
        "synthetic_event_added",
    }

    result = reconstruct_sp500_snapshots(
        ["ECHO", "CVNA"],
        corrected,
        start="2025-01-01",
        asof="2026-07-31",
        min_snapshot_members=1,
        max_snapshot_members=10,
        strict=True,
    )
    assert result.diagnostics["quality_status"] == "PASS"


def test_reviewed_event_correction_fails_if_provider_row_drifted():
    changes = _raw_changes()
    changes.loc[
        changes["date"].eq("2025-10-31"),
        "symbol",
    ] = "UNEXPECTED"

    with pytest.raises(DataContractError, match="expected added_ticker"):
        apply_sp500_pit_corrections(
            changes,
            _registry(),
            asof="2026-07-31",
        )


def test_future_symbol_transition_is_not_injected():
    corrected, audit = apply_sp500_pit_corrections(
        _raw_changes(),
        _registry(),
        asof="2026-06-23",
    )

    assert not corrected["effective_date"].eq(pd.Timestamp("2026-06-24")).any()
    assert audit[-1]["action"] == "future_not_applied"
