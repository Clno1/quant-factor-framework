from __future__ import annotations

import pytest

from src.market_regime_research.settings import (
    load_market_regime_research_settings,
)


def _config() -> dict:
    return {
        "market_regime_research": {
            "primary_symbol": "SPY",
            "end": "2026-01-10",
            "instruments": [
                {
                    "symbol": "SPY",
                    "start": "2026-01-02",
                    "kind": "etf",
                }
            ],
            "point_in_time": {
                "universe": "SP500",
                "start": "2026-01-01",
                "min_snapshot_members": 1,
                "max_snapshot_members": 10,
            },
        }
    }


def test_settings_reject_path_like_pit_universe(tmp_path):
    config = _config()
    config["market_regime_research"]["point_in_time"]["universe"] = "../../escape"

    with pytest.raises(ValueError, match="safe ASCII"):
        load_market_regime_research_settings(
            config,
            raw_root=tmp_path / "raw",
            output_root=tmp_path / "output",
        )


def test_settings_isolate_market_regime_data_and_pit_publications(tmp_path):
    config = _config()
    config["market_regime_research"]["point_in_time"].update(
        {
            "data_universe": "SP500",
            "publication_id": "SP500",
        }
    )

    with pytest.raises(ValueError, match="must be isolated"):
        load_market_regime_research_settings(
            config,
            raw_root=tmp_path / "raw",
            output_root=tmp_path / "output",
        )


def test_settings_reject_path_like_market_regime_publication_id(tmp_path):
    config = _config()
    config["market_regime_research"]["point_in_time"]["publication_id"] = (
        "../../escape"
    )

    with pytest.raises(ValueError, match="safe ASCII"):
        load_market_regime_research_settings(
            config,
            raw_root=tmp_path / "raw",
            output_root=tmp_path / "output",
        )


def test_settings_reject_invalid_rolling_windows(tmp_path):
    config = _config()
    config["market_regime_research"]["features"] = {
        "realized_volatility_windows": [1],
    }

    with pytest.raises(ValueError, match="at least 2"):
        load_market_regime_research_settings(
            config,
            raw_root=tmp_path / "raw",
            output_root=tmp_path / "output",
        )


def test_settings_reject_instrument_start_after_end(tmp_path):
    config = _config()
    config["market_regime_research"]["instruments"][0]["start"] = "2027-01-01"

    with pytest.raises(ValueError, match="start cannot be after"):
        load_market_regime_research_settings(
            config,
            raw_root=tmp_path / "raw",
            output_root=tmp_path / "output",
        )
