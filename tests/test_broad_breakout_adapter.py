from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from src.breakouts.broad_daily_data import validate_breakout_daily_data_contract
from src.breakouts.daily_data import daily_frames_from_bars, load_breakout_daily_dataset
from src.data.foundation import DataFoundationError


def test_us_liquid_breakout_loader_uses_broad_parent_adapter():
    expected = object()
    with patch(
        "src.breakouts.broad_daily_data.load_broad_breakout_daily_dataset",
        return_value=expected,
    ) as loader:
        observed = load_breakout_daily_dataset(
            requested_universe="US_ACTIVE",
            data_universe="US_LIQUID_5M",
            tickers=["MDB"],
            end="2026-08-24",
        )

    assert observed is expected
    assert loader.call_args.kwargs["requested_universe"] == "US_ACTIVE"
    assert loader.call_args.kwargs["tickers"] == ["MDB"]
    assert loader.call_args.kwargs["end"] == "2026-08-24"


def test_breakout_frames_require_authenticated_adjusted_close():
    bars = pd.DataFrame(
        {
            "date": ["2026-08-24"],
            "ticker": ["MDB"],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [100.0],
        }
    )
    with pytest.raises(ValueError, match="adj_close"):
        daily_frames_from_bars(bars)


def _derived_contract() -> dict:
    return {
        "coverage": {
            "derived_universe": {
                "universe": "US_LIQUID_5M",
                "universe_version_id": "pit-v1",
                "parent_dataset_version_id": "coverage-v1",
                "target_session": "2026-08-24",
                "membership_sha256": "membership-sha",
                "eligibility_sha256": "eligibility-sha",
                "manifest_sha256": "pit-manifest-sha",
                "security_master_generation_id": "security-v1",
                "security_master_manifest_sha256": "security-sha",
            }
        }
    }


def test_breakout_contract_authenticates_exact_derived_universe():
    parent = SimpleNamespace(version_id="coverage-v1")
    universe = SimpleNamespace(
        target_session=pd.Timestamp("2026-08-24").date(),
        parent_dataset_version_id="coverage-v1",
        membership_sha256="membership-sha",
        eligibility_sha256="eligibility-sha",
        manifest_sha256="pit-manifest-sha",
        security_master_generation_id="security-v1",
        security_master_manifest_sha256="security-sha",
    )
    store = Mock()
    store.get.return_value = universe
    with (
        patch(
            "src.breakouts.broad_daily_data.validate_daily_data_contract",
            return_value=parent,
        ),
        patch("src.breakouts.broad_daily_data.MarketDataReader"),
        patch("src.breakouts.broad_daily_data._universe_store", return_value=store),
    ):
        assert validate_breakout_daily_data_contract(_derived_contract()) is parent
    store.verify.assert_called_once_with(universe)


def test_breakout_contract_rejects_pit_hash_mismatch():
    parent = SimpleNamespace(version_id="coverage-v1")
    universe = SimpleNamespace(
        target_session=pd.Timestamp("2026-08-24").date(),
        parent_dataset_version_id="coverage-v1",
        membership_sha256="different-membership-sha",
        eligibility_sha256="eligibility-sha",
        manifest_sha256="pit-manifest-sha",
        security_master_generation_id="security-v1",
        security_master_manifest_sha256="security-sha",
    )
    store = Mock()
    store.get.return_value = universe
    with (
        patch(
            "src.breakouts.broad_daily_data.validate_daily_data_contract",
            return_value=parent,
        ),
        patch("src.breakouts.broad_daily_data.MarketDataReader"),
        patch("src.breakouts.broad_daily_data._universe_store", return_value=store),
        pytest.raises(DataFoundationError, match="membership_sha256"),
    ):
        validate_breakout_daily_data_contract(_derived_contract())
