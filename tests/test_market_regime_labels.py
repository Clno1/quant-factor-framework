from __future__ import annotations

import numpy as np
import pandas as pd

from src.market_regime_research.labels import (
    _first_touch,
    build_turning_point_labels,
)
from src.market_regime_research.settings import LabelSettings


def _prices(rows: int = 80) -> pd.DataFrame:
    index = pd.date_range("2025-01-02", periods=rows, freq="B")
    close = pd.Series(
        100.0 + np.sin(np.arange(rows) / 4.0) * 2.0 + np.arange(rows) * 0.05,
        index=index,
    )
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "adj_open": close * 0.999,
            "adj_high": close * 1.01,
            "adj_low": close * 0.99,
            "adj_close": close,
            "volume": 1_000_000,
        },
        index=index,
    )


def _settings() -> LabelSettings:
    return LabelSettings(
        horizons=(5,),
        volatility_window=5,
        high_lookback=20,
        top_near_high_pct=0.05,
        bottom_min_drawdown_pct=0.05,
        bottom_drawdown_quantile=0.20,
        bottom_quantile_lookback=30,
        minimum_history=20,
        barrier_vol_multiplier=1.0,
        minimum_barrier_pct=0.01,
    )


def test_first_touch_resolves_order_and_preserves_same_bar_ambiguity():
    down_first = _first_touch(
        base_price=100,
        future_high=np.array([100.5, 103.0]),
        future_low=np.array([97.0, 99.0]),
        barrier=0.02,
        event_side="down",
    )
    assert down_first[:3] == (1, "reversal", 1)

    ambiguous = _first_touch(
        base_price=100,
        future_high=np.array([103.0]),
        future_low=np.array([97.0]),
        barrier=0.02,
        event_side="down",
    )
    assert ambiguous[:3] == (None, "ambiguous", 1)


def test_label_preconditions_and_barriers_do_not_use_future_observations():
    original = _prices()
    changed = original.copy()
    cutoff = 55
    changed.iloc[cutoff + 1 :, changed.columns.get_loc("adj_close")] *= 1.8
    changed.iloc[cutoff + 1 :, changed.columns.get_loc("adj_open")] *= 1.8
    changed.iloc[cutoff + 1 :, changed.columns.get_loc("adj_high")] *= 1.8
    changed.iloc[cutoff + 1 :, changed.columns.get_loc("adj_low")] *= 1.8

    first = build_turning_point_labels(original, _settings())
    second = build_turning_point_labels(changed, _settings())
    causal_columns = [
        "daily_vol_5d",
        "drawdown_20d",
        "bottom_drawdown_threshold",
        "top_eligible",
        "bottom_eligible",
        "barrier_5d",
    ]
    pd.testing.assert_frame_equal(
        first.iloc[: cutoff + 1][causal_columns],
        second.iloc[: cutoff + 1][causal_columns],
    )


def test_last_horizon_has_no_fabricated_outcomes():
    labels = build_turning_point_labels(_prices(), _settings())
    tail = labels.tail(5)

    assert tail["top_label_5d"].isna().all()
    assert tail["bottom_label_5d"].isna().all()
    assert set(tail["top_first_touch_5d"]) == {"insufficient_future"}
