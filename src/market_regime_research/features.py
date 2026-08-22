"""
Point-in-time P0 features for broad-market top/bottom research.

Every function is causal: values dated T use observations available no later
than T.  Future data is confined to ``labels.py``.  The returned registry is a
required artifact, not optional documentation, so each column remains
traceable when candidate studies are reviewed later.
"""
from __future__ import annotations

import math
import re
from typing import Mapping

import numpy as np
import pandas as pd

from src.market_regime_research.models import (
    DataContractError,
    FeatureBundle,
    FeatureDefinition,
)
from src.market_regime_research.settings import FeatureSettings


_ALIASES = {
    "^GSPC": "spx",
    "^NDX": "ndx",
    "SPY": "spy",
    "QQQ": "qqq",
    "IWM": "iwm",
    "HYG": "hyg",
    "LQD": "lqd",
}


def _alias(symbol: str) -> str:
    if symbol in _ALIASES:
        return _ALIASES[symbol]
    value = re.sub(r"[^a-z0-9]+", "_", str(symbol).lower()).strip("_")
    if not value:
        raise ValueError(f"Unable to create feature alias for {symbol!r}")
    return value


def _normalize_index(frame: pd.DataFrame, *, label: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise DataContractError(f"{label} is empty")
    output = frame.copy()
    output.index = pd.to_datetime(output.index, errors="coerce")
    if output.index.isna().any() or output.index.has_duplicates:
        raise DataContractError(f"{label} has invalid or duplicate dates")
    if output.index.tz is not None:
        output.index = output.index.tz_convert(None)
    output.index = output.index.normalize()
    return output.sort_index()


def _feature_ohlcv(frame: pd.DataFrame, *, symbol: str) -> pd.DataFrame:
    frame = _normalize_index(frame, label=f"{symbol} prices")
    adjusted = {"adj_open", "adj_high", "adj_low", "adj_close"}
    raw = {"open", "high", "low", "close"}
    if adjusted.issubset(frame.columns):
        output = frame[
            ["adj_open", "adj_high", "adj_low", "adj_close", "volume"]
        ].rename(
            columns={
                "adj_open": "open",
                "adj_high": "high",
                "adj_low": "low",
                "adj_close": "close",
            }
        )
    elif raw.issubset(frame.columns) and "volume" in frame.columns:
        output = frame[["open", "high", "low", "close", "volume"]].copy()
    else:
        raise DataContractError(f"{symbol} lacks a consistent OHLCV basis")
    for column in output.columns:
        output[column] = pd.to_numeric(output[column], errors="coerce")
    return output


def _add(
    values: dict[str, pd.Series],
    registry: list[FeatureDefinition],
    *,
    name: str,
    series: pd.Series,
    group: str,
    instrument: str,
    formula: str,
    lookback: int,
    description: str,
    availability: str = "known_after_same_session_close",
) -> None:
    if name in values:
        raise DataContractError(f"Duplicate feature name: {name}")
    values[name] = pd.to_numeric(series, errors="coerce")
    registry.append(
        FeatureDefinition(
            feature_name=name,
            group=group,
            instrument=instrument,
            formula=formula,
            lookback_sessions=int(lookback),
            description=description,
            availability=availability,
        )
    )


def _bundle(
    values: dict[str, pd.Series],
    registry: list[FeatureDefinition],
    *,
    diagnostics: dict | None = None,
) -> FeatureBundle:
    frame = pd.concat(values, axis=1).sort_index() if values else pd.DataFrame()
    frame.index.name = "date"
    return FeatureBundle(
        values=frame,
        registry=registry,
        diagnostics=diagnostics or {},
    )


def compute_price_features(
    prices: Mapping[str, pd.DataFrame],
    settings: FeatureSettings | None = None,
) -> FeatureBundle:
    """Compute index/ETF trend, volatility, range, gap, and liquidity features."""
    config = settings or FeatureSettings()
    values: dict[str, pd.Series] = {}
    registry: list[FeatureDefinition] = []

    for symbol, raw_frame in prices.items():
        source_frame = _normalize_index(raw_frame, label=f"{symbol} prices")
        frame = _feature_ohlcv(source_frame, symbol=symbol)
        prefix = _alias(symbol)
        close = frame["close"]
        market_close = pd.to_numeric(
            source_frame["close"] if "close" in source_frame else close,
            errors="coerce",
        ).reindex(frame.index)
        returns = close.pct_change(fill_method=None)

        for window in (1, 5, 20, 60):
            series = returns if window == 1 else close.pct_change(
                periods=window,
                fill_method=None,
            )
            _add(
                values,
                registry,
                name=f"{prefix}_return_{window}d",
                series=series,
                group="price_trend",
                instrument=symbol,
                formula=f"close_t / close_t-{window} - 1",
                lookback=window,
                description=f"{window}-session total return",
            )

        rolling_high = close.rolling(252, min_periods=252).max()
        _add(
            values,
            registry,
            name=f"{prefix}_drawdown_252d",
            series=close / rolling_high - 1.0,
            group="price_trend",
            instrument=symbol,
            formula="close / rolling_max_252(close) - 1",
            lookback=252,
            description="Drawdown from the trailing one-year high",
        )

        for window in config.moving_average_windows:
            moving_average = close.rolling(window, min_periods=window).mean()
            _add(
                values,
                registry,
                name=f"{prefix}_distance_ma{window}",
                series=close / moving_average - 1.0,
                group="price_trend",
                instrument=symbol,
                formula=f"close / mean_{window}(close) - 1",
                lookback=window,
                description=f"Distance from {window}-session moving average",
            )

        for window in config.realized_volatility_windows:
            _add(
                values,
                registry,
                name=f"{prefix}_realized_vol_{window}d",
                series=returns.rolling(window, min_periods=window).std(ddof=1)
                * math.sqrt(252),
                group="realized_volatility",
                instrument=symbol,
                formula=f"std_{window}(daily_return) * sqrt(252)",
                lookback=window,
                description=f"Annualized {window}-session realized volatility",
            )

        previous_close = close.shift(1)
        true_range = pd.concat(
            [
                frame["high"] - frame["low"],
                (frame["high"] - previous_close).abs(),
                (frame["low"] - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        _add(
            values,
            registry,
            name=f"{prefix}_atr14_pct",
            series=true_range.rolling(14, min_periods=14).mean() / close,
            group="realized_volatility",
            instrument=symbol,
            formula="mean_14(true_range) / close",
            lookback=14,
            description="Average true range as a fraction of price",
        )
        _add(
            values,
            registry,
            name=f"{prefix}_gap_1d",
            series=frame["open"] / previous_close - 1.0,
            group="price_structure",
            instrument=symbol,
            formula="open_t / close_t-1 - 1",
            lookback=1,
            description="Overnight opening gap",
        )
        day_range = frame["high"] - frame["low"]
        _add(
            values,
            registry,
            name=f"{prefix}_close_location",
            series=(close - frame["low"]) / day_range.replace(0, np.nan),
            group="price_structure",
            instrument=symbol,
            formula="(close - low) / (high - low)",
            lookback=1,
            description="Close location within the daily range, from 0 to 1",
        )

        positive_volume = frame["volume"].where(frame["volume"] > 0)
        if positive_volume.notna().sum() >= 20:
            dollar_volume = market_close * positive_volume
            # Index "volume" is an aggregate vendor field, not shares of a
            # tradable security.  It can support activity shocks, but using it
            # as Amihud dollar volume would have no stable economic units.
            if not str(symbol).startswith("^"):
                amihud = (returns.abs() / dollar_volume).rolling(
                    20,
                    min_periods=20,
                ).mean() * 1_000_000
                _add(
                    values,
                    registry,
                    name=f"{prefix}_amihud_20d_x1m",
                    series=amihud,
                    group="liquidity",
                    instrument=symbol,
                    formula=(
                        "1e6 * mean_20(abs(total_return) / "
                        "(market_close * volume))"
                    ),
                    lookback=20,
                    description="Amihud price-impact proxy scaled by one million",
                )
            median_volume = positive_volume.rolling(20, min_periods=20).median()
            _add(
                values,
                registry,
                name=f"{prefix}_volume_shock_20d",
                series=positive_volume / median_volume - 1.0,
                group="liquidity",
                instrument=symbol,
                formula="volume / median_20(volume) - 1",
                lookback=20,
                description="Volume shock relative to trailing median volume",
            )
            down_volume = positive_volume.where(returns < 0, 0.0)
            _add(
                values,
                registry,
                name=f"{prefix}_down_volume_share_20d",
                series=down_volume.rolling(20, min_periods=20).sum()
                / positive_volume.rolling(20, min_periods=20).sum(),
                group="liquidity",
                instrument=symbol,
                formula="sum_20(volume where return<0) / sum_20(volume)",
                lookback=20,
                description="Share of trailing volume occurring on down days",
            )

    return _bundle(values, registry)


def _rolling_last_percentile(series: pd.Series, window: int) -> pd.Series:
    def percentile(values: np.ndarray) -> float:
        latest = values[-1]
        finite = values[np.isfinite(values)]
        if not np.isfinite(latest) or len(finite) == 0:
            return float("nan")
        return float(np.mean(finite <= latest))

    # Cboe's term indexes occasionally omit a date present in the longer VIX
    # calendar.  Count actual observations rather than requiring 252 perfectly
    # consecutive rows in the outer-joined table.
    observed = series.dropna()
    result = observed.rolling(window, min_periods=window).apply(
        percentile,
        raw=True,
    )
    return result.reindex(series.index)


def compute_volatility_features(volatility: pd.DataFrame) -> FeatureBundle:
    """Compute Cboe volatility/correlation state and volatility term structure."""
    frame = _normalize_index(volatility, label="Cboe volatility history")
    required = {"VIX", "VIX9D", "VIX3M", "COR1M"}
    missing = required - set(frame.columns)
    if missing:
        raise DataContractError(
            f"Cboe volatility history missing fields: {sorted(missing)}"
        )
    values: dict[str, pd.Series] = {}
    registry: list[FeatureDefinition] = []
    instruments = (
        ("VIX", "implied_volatility", "VIX closing level"),
        ("VIX9D", "implied_volatility", "VIX9D closing level"),
        ("VIX3M", "implied_volatility", "VIX3M closing level"),
        (
            "COR1M",
            "implied_correlation",
            "Cboe one-month option-implied average correlation",
        ),
    )
    for symbol, group, level_description in instruments:
        series = pd.to_numeric(frame[symbol], errors="coerce")
        lower = symbol.lower()
        _add(
            values,
            registry,
            name=f"{lower}_level",
            series=series,
            group=group,
            instrument=symbol,
            formula=f"{symbol}_close",
            lookback=1,
            description=level_description,
            availability="Cboe value available after 17:00 America/New_York",
        )
        for window in (1, 5, 20):
            _add(
                values,
                registry,
                name=f"{lower}_change_{window}d",
                series=series.diff(window),
                group=group,
                instrument=symbol,
                formula=f"{symbol}_t - {symbol}_t-{window}",
                lookback=window,
                description=f"{window}-session change in {symbol}",
                availability="Cboe value available after 17:00 America/New_York",
            )
        _add(
            values,
            registry,
            name=f"{lower}_percentile_252d",
            series=_rolling_last_percentile(series, 252),
            group=group,
            instrument=symbol,
            formula="empirical percentile of latest value in trailing 252 sessions",
            lookback=252,
            description=f"One-year rolling percentile of {symbol}",
            availability="Cboe value available after 17:00 America/New_York",
        )

    _add(
        values,
        registry,
        name="vix_vix3m_ratio",
        series=frame["VIX"] / frame["VIX3M"],
        group="volatility_term_structure",
        instrument="VIX/VIX3M",
        formula="VIX / VIX3M",
        lookback=1,
        description="One-month versus three-month implied-volatility slope",
        availability="Cboe values available after 17:00 America/New_York",
    )
    _add(
        values,
        registry,
        name="vix9d_vix3m_ratio",
        series=frame["VIX9D"] / frame["VIX3M"],
        group="volatility_term_structure",
        instrument="VIX9D/VIX3M",
        formula="VIX9D / VIX3M",
        lookback=1,
        description="Nine-day versus three-month implied-volatility slope",
        availability="Cboe values available after 17:00 America/New_York",
    )
    return _bundle(values, registry)


def compute_cross_asset_features(
    prices: Mapping[str, pd.DataFrame],
) -> FeatureBundle:
    """Compute relative-price state for size, growth, and credit proxies."""
    closes = {
        symbol: _feature_ohlcv(frame, symbol=symbol)["close"]
        for symbol, frame in prices.items()
    }
    values: dict[str, pd.Series] = {}
    registry: list[FeatureDefinition] = []
    pairs = (
        ("IWM", "SPY", "iwm_spy", "small-cap versus large-cap risk appetite"),
        ("QQQ", "SPY", "qqq_spy", "growth/technology leadership"),
        ("HYG", "LQD", "hyg_lqd", "high-yield versus investment-grade credit risk"),
    )
    for numerator, denominator, prefix, description in pairs:
        if numerator not in closes or denominator not in closes:
            continue
        ratio = closes[numerator] / closes[denominator]
        _add(
            values,
            registry,
            name=f"{prefix}_ratio",
            series=ratio,
            group="cross_asset",
            instrument=f"{numerator}/{denominator}",
            formula=f"{numerator}_adj_close / {denominator}_adj_close",
            lookback=1,
            description=description,
        )
        for window in (5, 20, 60):
            _add(
                values,
                registry,
                name=f"{prefix}_return_{window}d",
                series=ratio.pct_change(window, fill_method=None),
                group="cross_asset",
                instrument=f"{numerator}/{denominator}",
                formula=f"ratio_t / ratio_t-{window} - 1",
                lookback=window,
                description=f"{window}-session change in {description}",
            )
    return _bundle(values, registry)


def _align_credit_to_availability(credit: pd.DataFrame) -> pd.DataFrame:
    frame = _normalize_index(credit, label="credit history")
    if "hy_oas" not in frame.columns:
        raise DataContractError("Credit history missing hy_oas")
    if "hy_oas_available_at" not in frame.columns:
        raise DataContractError("Credit history missing hy_oas_available_at")
    available = pd.to_datetime(
        frame["hy_oas_available_at"],
        errors="coerce",
        utc=True,
    )
    if available.isna().any():
        raise DataContractError("Credit history contains invalid available_at")
    available_date = (
        pd.DatetimeIndex(available)
        .tz_convert("America/New_York")
        .tz_localize(None)
        .normalize()
    )
    output = pd.DataFrame(
        {"hy_oas": pd.to_numeric(frame["hy_oas"], errors="coerce").to_numpy()},
        index=available_date,
    )
    output = output.groupby(level=0).last().sort_index()
    output.index.name = "date"
    return output


def compute_credit_features(credit: pd.DataFrame) -> FeatureBundle:
    """Compute HY OAS features only on dates when the source was available."""
    frame = _align_credit_to_availability(credit)
    series = frame["hy_oas"]
    values: dict[str, pd.Series] = {}
    registry: list[FeatureDefinition] = []
    availability = "conservative FRED available_at date"
    _add(
        values,
        registry,
        name="hy_oas_level",
        series=series,
        group="credit",
        instrument="BAMLH0A0HYM2",
        formula="ICE BofA US High Yield OAS",
        lookback=1,
        description="High-yield option-adjusted spread",
        availability=availability,
    )
    for window in (5, 20, 60):
        _add(
            values,
            registry,
            name=f"hy_oas_change_{window}d",
            series=series.diff(window),
            group="credit",
            instrument="BAMLH0A0HYM2",
            formula=f"HY_OAS_t - HY_OAS_t-{window}",
            lookback=window,
            description=f"{window}-observation widening in high-yield spreads",
            availability=availability,
        )
    _add(
        values,
        registry,
        name="hy_oas_percentile_252d",
        series=_rolling_last_percentile(series, 252),
        group="credit",
        instrument="BAMLH0A0HYM2",
        formula="empirical percentile in trailing 252 available observations",
        lookback=252,
        description="One-year percentile of high-yield spreads",
        availability=availability,
    )
    return _bundle(values, registry)


def _validate_cross_section(
    adj_close: pd.DataFrame,
    membership_mask: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    prices = _normalize_index(adj_close, label="PIT adjusted-close matrix")
    mask = _normalize_index(membership_mask, label="PIT membership mask")
    prices.columns = prices.columns.astype(str).str.upper()
    mask.columns = mask.columns.astype(str).str.upper()
    if prices.columns.has_duplicates or mask.columns.has_duplicates:
        raise DataContractError("PIT matrix contains duplicate tickers")
    if not prices.index.equals(mask.index) or not prices.columns.equals(mask.columns):
        raise DataContractError(
            "PIT membership mask must exactly match adjusted-close index/columns"
        )
    return prices.apply(pd.to_numeric, errors="coerce"), mask.astype(bool)


def _average_pairwise_correlation(
    returns: pd.DataFrame,
    membership_mask: pd.DataFrame,
    *,
    window: int,
    minimum_members: int,
    minimum_coverage: float,
) -> pd.Series:
    """
    Efficiently estimate average pairwise correlation without 500x500 matrices.

    For complete standardized columns, the sum of all off-diagonal products
    divided by ``(window-1) * n * (n-1)`` equals average sample correlation.
    """
    output = pd.Series(np.nan, index=returns.index, dtype=float)
    for position in range(window - 1, len(returns)):
        active = membership_mask.iloc[position]
        columns = active.index[active].tolist()
        if len(columns) < minimum_members:
            continue
        sample = returns.iloc[position - window + 1 : position + 1][columns]
        sample = sample.dropna(axis=1, how="any")
        standard_deviation = sample.std(axis=0, ddof=1)
        sample = sample.loc[:, standard_deviation > 0]
        count = sample.shape[1]
        required_count = max(
            minimum_members,
            int(math.ceil(len(columns) * minimum_coverage)),
        )
        if count < required_count:
            continue
        standardized = (
            sample - sample.mean(axis=0)
        ) / sample.std(axis=0, ddof=1)
        row_sum = standardized.sum(axis=1)
        numerator = ((row_sum ** 2) - (standardized ** 2).sum(axis=1)).sum()
        output.iloc[position] = float(
            numerator / ((len(sample) - 1) * count * (count - 1))
        )
    return output


def compute_breadth_features(
    adj_close: pd.DataFrame,
    membership_mask: pd.DataFrame,
    *,
    benchmark_close: pd.Series,
    settings: FeatureSettings | None = None,
) -> FeatureBundle:
    """Compute survivorship-safe market breadth and cross-sectional state."""
    config = settings or FeatureSettings()
    prices, mask = _validate_cross_section(adj_close, membership_mask)
    returns = prices.pct_change(fill_method=None)
    continuously_active = mask & mask.shift(1, fill_value=False)
    active_returns = returns.where(continuously_active)
    valid_count = active_returns.notna().sum(axis=1)
    return_member_count = continuously_active.sum(axis=1)
    return_coverage = valid_count / return_member_count.replace(0, np.nan)
    enough = (
        (valid_count >= config.min_cross_section_members)
        & (return_coverage >= config.min_cross_section_coverage)
    )

    advances = (active_returns > 0).sum(axis=1)
    declines = (active_returns < 0).sum(axis=1)
    unchanged = (active_returns == 0).sum(axis=1)
    values: dict[str, pd.Series] = {}
    registry: list[FeatureDefinition] = []

    _add(
        values,
        registry,
        name="breadth_advance_pct",
        series=(advances / valid_count).where(enough),
        group="breadth",
        instrument="SP500_PIT",
        formula="advancing_members / valid_continuous_members",
        lookback=1,
        description="Share of PIT constituents with a positive daily return",
    )
    _add(
        values,
        registry,
        name="breadth_decline_pct",
        series=(declines / valid_count).where(enough),
        group="breadth",
        instrument="SP500_PIT",
        formula="declining_members / valid_continuous_members",
        lookback=1,
        description="Share of PIT constituents with a negative daily return",
    )
    _add(
        values,
        registry,
        name="breadth_net",
        series=((advances - declines) / valid_count).where(enough),
        group="breadth",
        instrument="SP500_PIT",
        formula="(advancing_members - declining_members) / valid_members",
        lookback=1,
        description="Net normalized advance-decline breadth",
    )
    _add(
        values,
        registry,
        name="breadth_unchanged_pct",
        series=(unchanged / valid_count).where(enough),
        group="breadth",
        instrument="SP500_PIT",
        formula="unchanged_members / valid_continuous_members",
        lookback=1,
        description="Share of PIT constituents with zero daily return",
    )

    active_member_count = mask.sum(axis=1)
    moving_average_coverage: dict[int, pd.Series] = {}
    for window in config.moving_average_windows:
        moving_average = prices.rolling(window, min_periods=window).mean()
        valid = mask & prices.notna() & moving_average.notna()
        denominator = valid.sum(axis=1)
        coverage = denominator / active_member_count.replace(0, np.nan)
        numerator = ((prices > moving_average) & valid).sum(axis=1)
        breadth = (numerator / denominator).where(
            (denominator >= config.min_cross_section_members)
            & (coverage >= config.min_cross_section_coverage)
        )
        moving_average_coverage[window] = coverage
        _add(
            values,
            registry,
            name=f"breadth_above_ma{window}_pct",
            series=breadth,
            group="breadth",
            instrument="SP500_PIT",
            formula=f"members_above_MA{window} / valid_PIT_members",
            lookback=window,
            description=f"Share of PIT constituents above MA{window}",
        )
        for change_window in config.breadth_change_windows:
            _add(
                values,
                registry,
                name=(
                    f"breadth_above_ma{window}_change_{change_window}d"
                ),
                series=breadth.diff(change_window),
                group="breadth",
                instrument="SP500_PIT",
                formula=(
                    f"breadth_above_MA{window}_t - "
                    f"breadth_above_MA{window}_t-{change_window}"
                ),
                lookback=window + change_window,
                description=(
                    f"{change_window}-session change in the share of PIT "
                    f"constituents above MA{window}"
                ),
            )

    rolling_high = prices.rolling(252, min_periods=252).max()
    rolling_low = prices.rolling(252, min_periods=252).min()
    high_valid = mask & prices.notna() & rolling_high.notna()
    low_valid = mask & prices.notna() & rolling_low.notna()
    high_count = high_valid.sum(axis=1)
    low_count = low_valid.sum(axis=1)
    high_enough = (
        (high_count >= config.min_cross_section_members)
        & (
            high_count / active_member_count.replace(0, np.nan)
            >= config.min_cross_section_coverage
        )
    )
    low_enough = (
        (low_count >= config.min_cross_section_members)
        & (
            low_count / active_member_count.replace(0, np.nan)
            >= config.min_cross_section_coverage
        )
    )
    _add(
        values,
        registry,
        name="breadth_new_high_252d_pct",
        series=(
            ((prices >= rolling_high) & high_valid).sum(axis=1) / high_count
        ).where(high_enough),
        group="breadth",
        instrument="SP500_PIT",
        formula="members_at_252d_high / valid_PIT_members",
        lookback=252,
        description="Share of PIT constituents making a one-year closing high",
    )
    _add(
        values,
        registry,
        name="breadth_new_low_252d_pct",
        series=(
            ((prices <= rolling_low) & low_valid).sum(axis=1) / low_count
        ).where(low_enough),
        group="breadth",
        instrument="SP500_PIT",
        formula="members_at_252d_low / valid_PIT_members",
        lookback=252,
        description="Share of PIT constituents making a one-year closing low",
    )

    equal_weight_return = active_returns.mean(axis=1).where(enough)
    benchmark = pd.to_numeric(benchmark_close, errors="coerce").reindex(prices.index)
    benchmark_return = benchmark.pct_change(fill_method=None)
    _add(
        values,
        registry,
        name="sp500_pit_equal_weight_return_1d",
        series=equal_weight_return,
        group="cross_section",
        instrument="SP500_PIT",
        formula="mean(valid PIT constituent daily returns)",
        lookback=1,
        description="PIT equal-weight constituent return",
    )
    _add(
        values,
        registry,
        name="sp500_ew_cw_spread_1d",
        series=equal_weight_return - benchmark_return,
        group="cross_section",
        instrument="SP500_PIT/SPY",
        formula="PIT equal-weight return - SPY return",
        lookback=1,
        description="Equal-weight minus cap-weight proxy return",
    )
    _add(
        values,
        registry,
        name="cross_section_dispersion_std_1d",
        series=active_returns.std(axis=1, ddof=1).where(enough),
        group="cross_section",
        instrument="SP500_PIT",
        formula="cross_section_std(valid constituent returns)",
        lookback=1,
        description="Cross-sectional standard deviation of daily returns",
    )
    row_median = active_returns.median(axis=1)
    dispersion_mad = active_returns.sub(row_median, axis=0).abs().median(axis=1)
    _add(
        values,
        registry,
        name="cross_section_dispersion_mad_1d",
        series=dispersion_mad.where(enough),
        group="cross_section",
        instrument="SP500_PIT",
        formula="median(abs(return - cross_section_median_return))",
        lookback=1,
        description="Robust cross-sectional return dispersion",
    )
    _add(
        values,
        registry,
        name=f"average_pairwise_correlation_{config.correlation_window}d",
        series=_average_pairwise_correlation(
            returns,
            mask,
            window=config.correlation_window,
            minimum_members=config.correlation_min_members,
            minimum_coverage=config.min_cross_section_coverage,
        ),
        group="cross_section",
        instrument="SP500_PIT",
        formula="average off-diagonal sample correlation of active members",
        lookback=config.correlation_window,
        description="Average pairwise constituent correlation",
    )
    diagnostics = {
        "minimum_valid_members": int(valid_count.min()),
        "median_valid_members": float(valid_count.median()),
        "maximum_valid_members": int(valid_count.max()),
        "low_coverage_dates": int((~enough).sum()),
        "minimum_required_member_coverage": float(
            config.min_cross_section_coverage
        ),
        "return_member_coverage": {
            "minimum": float(return_coverage.min()),
            "median": float(return_coverage.median()),
        },
        "moving_average_member_coverage": {
            f"ma{window}": {
                "minimum": float(coverage.min()),
                "median": float(coverage.median()),
            }
            for window, coverage in moving_average_coverage.items()
        },
    }
    return _bundle(values, registry, diagnostics=diagnostics)


def compute_momentum_stress_features(
    adj_close: pd.DataFrame,
    membership_mask: pd.DataFrame,
    settings: FeatureSettings | None = None,
) -> FeatureBundle:
    """Build a reproducible PIT cross-sectional momentum stress proxy."""
    config = settings or FeatureSettings()
    prices, mask = _validate_cross_section(adj_close, membership_mask)
    returns = prices.pct_change(fill_method=None)
    score = (
        prices.shift(config.momentum_skip)
        / prices.shift(config.momentum_lookback)
        - 1.0
    )

    # The score dated T-1 excludes at least the most recent session, so its
    # latest price input is known no later than T-2.  Combined with PIT
    # membership at T-1, these ranks can be formed before earning the T-1 -> T
    # close return.
    formation_score = score.shift(1).where(mask.shift(1, fill_value=False))
    ranks = formation_score.rank(axis=1, pct=True, method="average")
    eligible_return = returns.where(mask & mask.shift(1, fill_value=False))
    winners = eligible_return.where(
        ranks >= 1.0 - config.momentum_quantile
    )
    losers = eligible_return.where(ranks <= config.momentum_quantile)
    minimum_leg_members = max(
        3,
        int(math.ceil(config.min_cross_section_members * config.momentum_quantile)),
    )
    winner_count = winners.notna().sum(axis=1)
    loser_count = losers.notna().sum(axis=1)
    winner_return = winners.mean(axis=1).where(winner_count >= minimum_leg_members)
    loser_return = losers.mean(axis=1).where(loser_count >= minimum_leg_members)
    factor_return = winner_return - loser_return
    valid_factor = factor_return.notna()
    wealth = (1.0 + factor_return.fillna(0.0)).cumprod()
    drawdown = wealth / wealth.cummax() - 1.0
    drawdown = drawdown.where(valid_factor.cummax())

    values: dict[str, pd.Series] = {}
    registry: list[FeatureDefinition] = []
    _add(
        values,
        registry,
        name="momentum_winner_leg_return_1d",
        series=winner_return,
        group="positioning_stress",
        instrument="SP500_PIT_MOMENTUM",
        formula="mean(return_t of top momentum decile ranked at t-1)",
        lookback=config.momentum_lookback,
        description="Daily return of the lagged momentum winner leg",
    )
    _add(
        values,
        registry,
        name="momentum_loser_leg_return_1d",
        series=loser_return,
        group="positioning_stress",
        instrument="SP500_PIT_MOMENTUM",
        formula="mean(return_t of bottom momentum decile ranked at t-1)",
        lookback=config.momentum_lookback,
        description="Daily return of the lagged momentum loser leg",
    )
    _add(
        values,
        registry,
        name="momentum_factor_return_1d",
        series=factor_return,
        group="positioning_stress",
        instrument="SP500_PIT_MOMENTUM",
        formula="winner_leg_return - loser_leg_return",
        lookback=config.momentum_lookback,
        description="PIT long-winner/short-loser momentum return",
    )
    _add(
        values,
        registry,
        name="momentum_factor_drawdown",
        series=drawdown,
        group="positioning_stress",
        instrument="SP500_PIT_MOMENTUM",
        formula="cumulative_momentum_wealth / running_max - 1",
        lookback=config.momentum_lookback,
        description="Drawdown of the reproducible momentum proxy",
    )
    _add(
        values,
        registry,
        name="momentum_factor_realized_vol_20d",
        series=factor_return.rolling(20, min_periods=20).std(ddof=1)
        * math.sqrt(252),
        group="positioning_stress",
        instrument="SP500_PIT_MOMENTUM",
        formula="std_20(momentum_factor_return) * sqrt(252)",
        lookback=20,
        description="Annualized short-run volatility of momentum returns",
    )
    two_sided_loss = ((winner_return < 0) & (loser_return > 0)).astype(float)
    two_sided_loss = two_sided_loss.where(winner_return.notna() & loser_return.notna())
    _add(
        values,
        registry,
        name="momentum_two_sided_loss",
        series=two_sided_loss,
        group="positioning_stress",
        instrument="SP500_PIT_MOMENTUM",
        formula="1[winner_return < 0 and loser_return > 0]",
        lookback=1,
        description="Long winners fall while crowded shorts rise",
    )
    _add(
        values,
        registry,
        name="momentum_two_sided_loss_count_20d",
        series=two_sided_loss.rolling(20, min_periods=20).sum(),
        group="positioning_stress",
        instrument="SP500_PIT_MOMENTUM",
        formula="sum_20(two_sided_loss)",
        lookback=20,
        description="Frequency of two-sided momentum losses",
    )
    score_dispersion = formation_score.std(axis=1, ddof=1)
    _add(
        values,
        registry,
        name="momentum_score_dispersion",
        series=score_dispersion,
        group="positioning_stress",
        instrument="SP500_PIT_MOMENTUM",
        formula="cross_section_std(lagged 12-1 momentum score)",
        lookback=config.momentum_lookback,
        description="Cross-sectional spread of lagged momentum scores",
    )
    diagnostics = {
        "minimum_leg_members": minimum_leg_members,
        "valid_factor_dates": int(valid_factor.sum()),
        "first_valid_factor_date": (
            factor_return.first_valid_index().date().isoformat()
            if factor_return.first_valid_index() is not None
            else None
        ),
    }
    return _bundle(values, registry, diagnostics=diagnostics)


def combine_feature_bundles(*bundles: FeatureBundle) -> FeatureBundle:
    """Outer-join feature domains while enforcing a one-to-one registry."""
    frames = [bundle.values for bundle in bundles if not bundle.values.empty]
    values = pd.concat(frames, axis=1).sort_index() if frames else pd.DataFrame()
    duplicate_columns = values.columns[values.columns.duplicated()].tolist()
    if duplicate_columns:
        raise DataContractError(f"Duplicate combined features: {duplicate_columns}")
    registry = [
        definition
        for bundle in bundles
        for definition in bundle.registry
    ]
    registry_names = [definition.feature_name for definition in registry]
    if set(registry_names) != set(values.columns) or len(registry_names) != len(
        values.columns
    ):
        raise DataContractError("Feature registry does not match feature matrix")
    diagnostics = {
        f"bundle_{position}": bundle.diagnostics
        for position, bundle in enumerate(bundles)
        if bundle.diagnostics
    }
    values.index.name = "date"
    return FeatureBundle(values=values, registry=registry, diagnostics=diagnostics)


__all__ = [
    "combine_feature_bundles",
    "compute_breadth_features",
    "compute_credit_features",
    "compute_cross_asset_features",
    "compute_momentum_stress_features",
    "compute_price_features",
    "compute_volatility_features",
]
