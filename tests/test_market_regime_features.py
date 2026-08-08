from __future__ import annotations

import numpy as np
import pandas as pd

from src.market_regime_research.features import (
    _rolling_last_percentile,
    compute_breadth_features,
    compute_momentum_stress_features,
    compute_price_features,
)
from src.market_regime_research.settings import FeatureSettings


def _single_price(rows: int = 320) -> pd.DataFrame:
    index = pd.date_range("2024-01-02", periods=rows, freq="B")
    close = pd.Series(
        100 * np.exp(np.arange(rows) * 0.0005)
        * (1 + 0.01 * np.sin(np.arange(rows) / 5)),
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
            "volume": 1_000_000 + np.arange(rows) * 100,
        },
        index=index,
    )


def test_price_features_are_causal():
    original = _single_price()
    changed = original.copy()
    cutoff = 260
    changed.iloc[cutoff + 1 :, changed.columns.get_loc("adj_close")] *= 2
    changed.iloc[cutoff + 1 :, changed.columns.get_loc("adj_open")] *= 2
    changed.iloc[cutoff + 1 :, changed.columns.get_loc("adj_high")] *= 2
    changed.iloc[cutoff + 1 :, changed.columns.get_loc("adj_low")] *= 2

    first = compute_price_features({"SPY": original}).values
    second = compute_price_features({"SPY": changed}).values

    pd.testing.assert_frame_equal(
        first.iloc[: cutoff + 1],
        second.iloc[: cutoff + 1],
    )


def test_amihud_uses_market_close_but_total_return_numerator():
    frame = _single_price(rows=40)
    frame[["open", "high", "low", "close"]] *= 2.0

    result = compute_price_features({"SPY": frame}).values
    total_return = frame["adj_close"].pct_change(fill_method=None)
    expected = (
        (total_return.abs() / (frame["close"] * frame["volume"]))
        .rolling(20, min_periods=20)
        .mean()
        * 1_000_000
    )
    expected.index.name = "date"

    pd.testing.assert_series_equal(
        result["spy_amihud_20d_x1m"],
        expected.rename("spy_amihud_20d_x1m"),
    )


def test_breadth_excludes_non_members_even_when_their_return_is_extreme():
    index = pd.date_range("2026-01-02", periods=70, freq="B")
    prices = pd.DataFrame(
        {
            "A": 100 * (1.001 ** np.arange(70)),
            "B": 100 * (0.999 ** np.arange(70)),
            "OUT": np.r_[np.repeat(100.0, 69), 300.0],
        },
        index=index,
    )
    mask = pd.DataFrame(True, index=index, columns=prices.columns)
    mask["OUT"] = False
    settings = FeatureSettings(
        moving_average_windows=(5, 10, 20),
        realized_volatility_windows=(5, 20),
        correlation_window=20,
        correlation_min_members=2,
        momentum_lookback=40,
        momentum_skip=5,
        momentum_quantile=0.25,
        min_cross_section_members=2,
    )

    result = compute_breadth_features(
        prices,
        mask,
        benchmark_close=prices["A"],
        settings=settings,
    ).values

    latest = result.iloc[-1]
    assert latest["breadth_advance_pct"] == 0.5
    assert latest["breadth_decline_pct"] == 0.5
    assert latest["breadth_net"] == 0.0


def test_momentum_uses_previous_session_ranks():
    index = pd.date_range("2025-01-02", periods=80, freq="B")
    tickers = ["A", "B", "C", "D", "E", "F"]
    prices = pd.DataFrame(index=index, columns=tickers, dtype=float)
    for offset, ticker in enumerate(tickers):
        prices[ticker] = 100 * (1 + (offset - 2) * 0.001) ** np.arange(80)
    mask = pd.DataFrame(True, index=index, columns=tickers)
    settings = FeatureSettings(
        moving_average_windows=(5, 10, 20),
        realized_volatility_windows=(5, 20),
        correlation_window=20,
        correlation_min_members=3,
        momentum_lookback=40,
        momentum_skip=5,
        momentum_quantile=0.20,
        min_cross_section_members=6,
    )

    baseline = compute_momentum_stress_features(
        prices,
        mask,
        settings,
    ).values
    changed = prices.copy()
    target_position = 70
    changed.iloc[target_position] = changed.iloc[target_position] * pd.Series(
        {"A": 2.0, "B": 1.0, "C": 1.0, "D": 1.0, "E": 1.0, "F": 0.5}
    )
    modified = compute_momentum_stress_features(
        changed,
        mask,
        settings,
    ).values

    # A same-day shock changes returns, but cannot change which names belong to
    # winner/loser legs until the following formation date.
    formation_before = (
        prices.shift(settings.momentum_skip)
        / prices.shift(settings.momentum_lookback)
        - 1
    ).shift(1).rank(axis=1, pct=True)
    formation_after = (
        changed.shift(settings.momentum_skip)
        / changed.shift(settings.momentum_lookback)
        - 1
    ).shift(1).rank(axis=1, pct=True)
    pd.testing.assert_series_equal(
        formation_before.iloc[target_position],
        formation_after.iloc[target_position],
    )
    assert (
        baseline.iloc[target_position]["momentum_factor_return_1d"]
        != modified.iloc[target_position]["momentum_factor_return_1d"]
    )


def test_rolling_percentile_counts_observations_across_source_gaps():
    index = pd.date_range("2025-01-02", periods=8, freq="B")
    series = pd.Series([1.0, 2.0, np.nan, 3.0, 4.0, 5.0, np.nan, 6.0], index=index)

    result = _rolling_last_percentile(series, 5)

    assert result.iloc[-1] == 1.0
