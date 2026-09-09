from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import src.backtest.composer as composer
from src.strategies.definition import (
    StrategyComponent,
    StrategyDefinition,
    StrategyValidationError,
)
from src.watchlists.definition import WatchlistDefinition, WatchlistItem


@pytest.mark.parametrize("weight", [0.0, np.nan, np.inf, -np.inf])
def test_strategy_rejects_zero_or_non_finite_component_weight(weight):
    strategy = StrategyDefinition.new(
        "invalid",
        "",
        [StrategyComponent("MOM_1M", weight)],
    )
    with pytest.raises(StrategyValidationError):
        strategy.validate()


def test_composer_requires_every_factor_for_each_stock_date(monkeypatch):
    dates = pd.date_range("2026-01-05", periods=3, freq="B")
    first = pd.DataFrame(
        {
            "A": [1.0, 2.0, 3.0],
            "B": [2.0, 3.0, 4.0],
            "C": [3.0, 4.0, 5.0],
        },
        index=dates,
    )
    second = first.copy()
    second.loc[dates[1], "B"] = np.nan
    bundles = {
        "MOM_1M": (first.copy(), first),
        "MOM_3M": (second.copy(), second),
    }
    monkeypatch.setattr(
        composer,
        "_load_factor_bundle",
        lambda factor_id, _universe, **_kwargs: bundles[factor_id],
    )

    result = composer.compose_factor(
        [
            StrategyComponent("MOM_1M", 0.5),
            StrategyComponent("MOM_3M", 0.5),
        ],
        "TEST",
    )

    assert pd.isna(result.composite.loc[dates[1], "B"])
    assert result.composite.loc[dates[1], ["A", "C"]].notna().all()


def test_composer_rejects_factor_generation_change(monkeypatch):
    index = pd.date_range("2026-01-05", periods=2, freq="B")
    values = pd.DataFrame({"A": [1.0, 2.0]}, index=index)
    monkeypatch.setattr(
        composer,
        "load_factor_matrix_bundle",
        lambda _factor_id, universe: (
            values,
            values,
            {"generation_id": "generation-new", "universe": universe},
        ),
    )

    with pytest.raises(
        composer.FactorDataMissingError,
        match="Refusing a mixed-version run",
    ):
        composer.compose_factor(
            [StrategyComponent("MOM_1M", 1.0)],
            "TEST",
            expected_generations={"MOM_1M": "generation-frozen"},
        )


def test_watchlist_rejects_non_finite_weight():
    watchlist = WatchlistDefinition.new(
        "invalid",
        items=[WatchlistItem("AAPL", np.nan)],
    )
    with pytest.raises(ValueError, match="有限"):
        watchlist.validate()
