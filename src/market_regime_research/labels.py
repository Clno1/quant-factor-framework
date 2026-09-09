"""
Volatility-scaled first-touch labels for broad-market tops and bottoms.

Only the outcome columns look forward.  Eligibility, drawdown thresholds, and
barrier sizes use information available at the candidate date.  A daily bar
that touches both barriers is deliberately labeled ambiguous because OHLC data
cannot reveal which threshold was reached first.
"""
from __future__ import annotations

import math
from typing import Literal

import numpy as np
import pandas as pd

from src.market_regime_research.models import DataContractError
from src.market_regime_research.settings import LabelSettings


def _label_ohlc(frame: pd.DataFrame) -> pd.DataFrame:
    """Select one internally consistent OHLC basis for event labeling."""
    adjusted = {"adj_open", "adj_high", "adj_low", "adj_close"}
    raw = {"open", "high", "low", "close"}
    if adjusted.issubset(frame.columns):
        output = frame[
            ["adj_open", "adj_high", "adj_low", "adj_close"]
        ].rename(
            columns={
                "adj_open": "open",
                "adj_high": "high",
                "adj_low": "low",
                "adj_close": "close",
            }
        )
    elif raw.issubset(frame.columns):
        output = frame[["open", "high", "low", "close"]].copy()
    else:
        raise DataContractError(
            "Turning-point labels require raw or adjusted OHLC columns"
        )
    output.index = pd.to_datetime(output.index, errors="coerce")
    if output.empty or output.index.isna().any() or output.index.has_duplicates:
        raise DataContractError("Label OHLC index is empty, invalid, or duplicated")
    if output.index.tz is not None:
        output.index = output.index.tz_convert(None)
    output.index = output.index.normalize()
    output = output.sort_index()
    for column in output.columns:
        output[column] = pd.to_numeric(output[column], errors="coerce")
    if output.isna().any().any() or (output <= 0).any().any():
        raise DataContractError("Label OHLC contains missing or non-positive prices")
    if (
        output["high"] < output[["open", "close"]].max(axis=1)
    ).any() or (
        output["low"] > output[["open", "close"]].min(axis=1)
    ).any():
        raise DataContractError("Label OHLC violates high/low invariants")
    output.index.name = "date"
    return output


def _first_touch(
    *,
    base_price: float,
    future_high: np.ndarray,
    future_low: np.ndarray,
    barrier: float,
    event_side: Literal["up", "down"],
) -> tuple[int | None, str, int | None, float, float]:
    """Resolve one path, preserving daily-bar ambiguity."""
    up_returns = future_high / base_price - 1.0
    down_returns = future_low / base_price - 1.0
    up_hits = np.flatnonzero(up_returns >= barrier)
    down_hits = np.flatnonzero(down_returns <= -barrier)
    first_up = int(up_hits[0]) + 1 if len(up_hits) else None
    first_down = int(down_hits[0]) + 1 if len(down_hits) else None
    mfe = float(np.nanmax(up_returns))
    mae = float(np.nanmin(down_returns))

    if first_up is not None and first_down is not None and first_up == first_down:
        return None, "ambiguous", first_up, mfe, mae
    if first_up is None and first_down is None:
        return 0, "none", None, mfe, mae

    up_first = first_up is not None and (
        first_down is None or first_up < first_down
    )
    event_hit = up_first if event_side == "up" else not up_first
    touch_day = first_up if up_first else first_down
    if event_side == "up":
        outcome = "reversal" if event_hit else "failure"
    else:
        outcome = "reversal" if event_hit else "continuation"
    return int(event_hit), outcome, touch_day, mfe, mae


def build_turning_point_labels(
    price_frame: pd.DataFrame,
    settings: LabelSettings | None = None,
) -> pd.DataFrame:
    """
    Build top-risk and bottom-reversal labels for every configured horizon.

    Top candidates must be near their trailing high.  Bottom candidates must
    have both a fixed meaningful drawdown and a causal trailing-quantile
    drawdown.  Labels outside those candidate sets are nullable rather than
    being counted as easy negatives.
    """
    config = settings or LabelSettings()
    ohlc = _label_ohlc(price_frame)
    close = ohlc["close"]
    returns = close.pct_change(fill_method=None)
    daily_volatility = returns.rolling(
        config.volatility_window,
        min_periods=config.volatility_window,
    ).std(ddof=1)
    rolling_high = close.rolling(
        config.high_lookback,
        min_periods=config.minimum_history,
    ).max()
    drawdown = close / rolling_high - 1.0

    # shift(1) is essential: today's eligibility threshold cannot include the
    # drawdown observation it is deciding whether to classify as extreme.
    adaptive_bottom_threshold = drawdown.rolling(
        config.bottom_quantile_lookback,
        min_periods=config.minimum_history,
    ).quantile(config.bottom_drawdown_quantile).shift(1)
    fixed_bottom_threshold = -abs(config.bottom_min_drawdown_pct)
    bottom_threshold = adaptive_bottom_threshold.where(
        adaptive_bottom_threshold.notna(),
        fixed_bottom_threshold,
    )
    bottom_threshold = pd.Series(
        np.minimum(bottom_threshold.to_numpy(dtype=float), fixed_bottom_threshold),
        index=ohlc.index,
    )

    enough_history = rolling_high.notna() & daily_volatility.notna()
    top_eligible = enough_history & (
        drawdown >= -abs(config.top_near_high_pct)
    )
    bottom_eligible = enough_history & (drawdown <= bottom_threshold)

    labels = pd.DataFrame(index=ohlc.index)
    labels["reference_close"] = close
    labels[f"daily_vol_{config.volatility_window}d"] = daily_volatility
    labels[f"drawdown_{config.high_lookback}d"] = drawdown
    labels["bottom_drawdown_threshold"] = bottom_threshold
    labels["top_eligible"] = top_eligible.astype(bool)
    labels["bottom_eligible"] = bottom_eligible.astype(bool)

    high_values = ohlc["high"].to_numpy(dtype=float)
    low_values = ohlc["low"].to_numpy(dtype=float)
    close_values = close.to_numpy(dtype=float)
    count = len(ohlc)

    for horizon in config.horizons:
        barrier = (
            daily_volatility
            * math.sqrt(int(horizon))
            * config.barrier_vol_multiplier
        ).clip(lower=config.minimum_barrier_pct)
        labels[f"barrier_{horizon}d"] = barrier
        labels[f"forward_return_{horizon}d"] = (
            close.shift(-int(horizon)) / close - 1.0
        )

        top_values: list[int | None] = []
        top_outcomes: list[str] = []
        top_days: list[int | None] = []
        bottom_values: list[int | None] = []
        bottom_outcomes: list[str] = []
        bottom_days: list[int | None] = []
        mfe_values: list[float] = []
        mae_values: list[float] = []

        for position in range(count):
            current_barrier = float(barrier.iloc[position])
            has_history = np.isfinite(current_barrier)
            has_future = position + int(horizon) < count
            if not has_history:
                top_values.append(None)
                bottom_values.append(None)
                top_outcomes.append("insufficient_history")
                bottom_outcomes.append("insufficient_history")
                top_days.append(None)
                bottom_days.append(None)
                mfe_values.append(float("nan"))
                mae_values.append(float("nan"))
                continue
            if not has_future:
                top_values.append(None)
                bottom_values.append(None)
                top_outcomes.append("insufficient_future")
                bottom_outcomes.append("insufficient_future")
                top_days.append(None)
                bottom_days.append(None)
                mfe_values.append(float("nan"))
                mae_values.append(float("nan"))
                continue

            future_slice = slice(position + 1, position + int(horizon) + 1)
            future_high = high_values[future_slice]
            future_low = low_values[future_slice]
            base_price = close_values[position]
            # Compute path excursions once.  The event direction differs, but
            # MFE/MAE are properties of the same future path.
            top_label, top_outcome, top_day, mfe, mae = _first_touch(
                base_price=base_price,
                future_high=future_high,
                future_low=future_low,
                barrier=current_barrier,
                event_side="down",
            )
            bottom_label, bottom_outcome, bottom_day, _, _ = _first_touch(
                base_price=base_price,
                future_high=future_high,
                future_low=future_low,
                barrier=current_barrier,
                event_side="up",
            )
            mfe_values.append(mfe)
            mae_values.append(mae)

            if not bool(top_eligible.iloc[position]):
                top_values.append(None)
                top_outcomes.append("ineligible")
                top_days.append(None)
            else:
                top_values.append(top_label)
                top_outcomes.append(top_outcome)
                top_days.append(top_day)

            if not bool(bottom_eligible.iloc[position]):
                bottom_values.append(None)
                bottom_outcomes.append("ineligible")
                bottom_days.append(None)
            else:
                bottom_values.append(bottom_label)
                bottom_outcomes.append(bottom_outcome)
                bottom_days.append(bottom_day)

        labels[f"future_mfe_{horizon}d"] = mfe_values
        labels[f"future_mae_{horizon}d"] = mae_values
        labels[f"top_label_{horizon}d"] = pd.array(top_values, dtype="Int8")
        labels[f"top_first_touch_{horizon}d"] = top_outcomes
        labels[f"top_touch_day_{horizon}d"] = pd.array(top_days, dtype="Int16")
        labels[f"bottom_label_{horizon}d"] = pd.array(
            bottom_values,
            dtype="Int8",
        )
        labels[f"bottom_first_touch_{horizon}d"] = bottom_outcomes
        labels[f"bottom_touch_day_{horizon}d"] = pd.array(
            bottom_days,
            dtype="Int16",
        )

    labels.index.name = "date"
    return labels


__all__ = ["build_turning_point_labels"]
