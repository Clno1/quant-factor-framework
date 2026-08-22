from __future__ import annotations

import pandas as pd
import pytest

import scripts.run_market_regime_research as cli
from src.market_regime_research.settings import MarketRegimeResearchSettings


def test_pit_cli_cannot_backdate_the_current_constituent_snapshot(monkeypatch):
    monkeypatch.setattr(
        cli,
        "latest_completed_xnys_session",
        lambda: pd.Timestamp("2026-01-10"),
    )

    with pytest.raises(ValueError, match="current, not historical"):
        cli._run_pit(
            MarketRegimeResearchSettings(),
            asof="2026-01-09",
            candidate_only=True,
        )


def test_market_regime_pit_uses_a_dedicated_publication_path():
    target = cli._membership_target(MarketRegimeResearchSettings())

    assert target.name == "SP500_MARKET_REGIME.parquet"
    assert target.name != "SP500.parquet"


def test_screen_cli_accepts_an_explicit_frozen_candidate_registry():
    args = cli._parser().parse_args(
        [
            "screen",
            "--candidate-registry",
            "configs/market_regime_screening_candidates.yaml",
        ]
    )

    assert args.candidate_registry.name == "market_regime_screening_candidates.yaml"
