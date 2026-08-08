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
