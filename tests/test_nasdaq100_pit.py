from __future__ import annotations

import pandas as pd
import pytest

from src.data.nasdaq100_pit import (
    MINIMUM_VERIFIED_EVENT_GROUPS,
    compare_current_constituents,
    current_constituent_diagnostics,
    load_nasdaq100_verification_registry,
    normalize_fmp_nasdaq100_changes,
    verify_nasdaq100_event_groups,
)
from src.market_regime_research.models import DataContractError
from src.market_regime_research.pit import reconstruct_sp500_snapshots


def _changes() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2026-06-21",
                "dateAdded": "June 22, 2026",
                "symbol": "ALAB",
                "addedSecurity": "Astera Labs",
                "removedTicker": "INSM",
                "removedSecurity": "Insmed",
                "reason": "Quarterly rebalance",
            },
            {
                "date": "2025-05-18",
                "dateAdded": "May 19, 2025",
                "symbol": "SHOP",
                "addedSecurity": "Shopify",
                "removedTicker": "MDB",
                "removedSecurity": "MongoDB",
                "reason": "Minimum weight",
            },
        ]
    )


def test_nasdaq100_normalization_uses_effective_date_not_provider_date():
    normalized = normalize_fmp_nasdaq100_changes(_changes())

    row = normalized.loc[normalized["added_ticker"].eq("ALAB")].iloc[0]
    assert row["provider_date"] == pd.Timestamp("2026-06-21")
    assert row["effective_date"] == pd.Timestamp("2026-06-22")
    assert row["effective_date_source"] == "dateAdded"
    assert "PROVIDER_DATE_DIFFERS_FROM_EFFECTIVE_DATE" in row["reason_codes"]


def test_nasdaq100_normalized_events_reconstruct_cleanly():
    normalized = normalize_fmp_nasdaq100_changes(_changes())

    result = reconstruct_sp500_snapshots(
        ["ALAB", "SHOP"],
        normalized,
        asof="2026-06-30",
        start="2025-01-01",
        min_snapshot_members=1,
        max_snapshot_members=10,
        strict=True,
    )

    assert result.diagnostics["quality_status"] == "PASS"
    assert result.diagnostics["source_warning_events"] == 2


def test_current_constituents_require_exact_ticker_match():
    provider = pd.DataFrame({"ticker": ["AAPL", "MSFT"]})
    official = pd.DataFrame({"ticker": ["AAPL", "NVDA"]})

    with pytest.raises(DataContractError, match="current constituents differ"):
        compare_current_constituents(provider, official)
    diagnostics = current_constituent_diagnostics(provider, official)
    assert diagnostics["provider_only"] == ["MSFT"]
    assert diagnostics["official_only"] == ["NVDA"]


def test_verified_event_group_fails_closed_on_provider_drift():
    normalized = normalize_fmp_nasdaq100_changes(_changes())
    registry = {
        "verified_event_groups": [
            {
                "id": "shop",
                "effective_date": "2025-05-19",
                "additions": ["SHOP"],
                "removals": ["WRONG"],
                "sources": ["https://example.test/shop"],
            }
        ]
    }

    with pytest.raises(DataContractError, match="shop.*drifted"):
        verify_nasdaq100_event_groups(
            normalized,
            registry,
            asof="2026-06-30",
        )


def test_production_registry_has_required_official_event_coverage():
    registry, _path, _checksum = load_nasdaq100_verification_registry()

    groups = registry["verified_event_groups"]
    assert len(groups) >= MINIMUM_VERIFIED_EVENT_GROUPS
    assert all(group["sources"] for group in groups)
