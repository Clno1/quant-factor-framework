from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.analysis.hac import newey_west_mean_stats
from src.analysis.ic import compute_forward_returns, compute_ic, ic_summary
from src.backtest.integrity import build_tradable_mask_integrity
from src.backtest.metrics import relative_performance_summary
from src.breakouts import historical_backtest as breakout_history
from src.breakouts.historical_backtest import (
    BreakoutBacktestConfig,
    backtest_breakout_frames,
)
from src.data.price_semantics import PriceSemantics
import src.preprocessing.neutralize as neutralize_module
from src.preprocessing.neutralize import NeutralizationDataError


def _wide_prices() -> dict[str, pd.DataFrame]:
    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    columns = ["AAA"]
    return {
        "open": pd.DataFrame([100.0, 100.0, 100.0], index=dates, columns=columns),
        "close": pd.DataFrame([100.0, 100.0, 100.0], index=dates, columns=columns),
        "adj_close": pd.DataFrame([95.0, 96.0, 96.0], index=dates, columns=columns),
        "volume": pd.DataFrame([1_000.0, 1_000.0, 1_000.0], index=dates, columns=columns),
    }


def test_price_semantics_separates_execution_and_total_return_open() -> None:
    semantics = PriceSemantics.from_wide(_wide_prices())
    assert semantics.execution_open.iloc[0, 0] == pytest.approx(100.0)
    assert semantics.total_return_open.iloc[0, 0] == pytest.approx(95.0)
    assert semantics.total_return_open.iloc[1, 0] == pytest.approx(96.0)
    # Raw executable opens are flat, but the total-return open captures the
    # dividend adjustment change.
    forward = semantics.forward_open_to_open_total_returns()
    assert forward.iloc[0, 0] == pytest.approx(96.0 / 95.0 - 1.0)
    assert semantics.execution_dollar_volume().iloc[0, 0] == pytest.approx(100_000.0)


def test_tradability_uses_execution_close_not_adjusted_close() -> None:
    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    cols = pd.Index(["AAA"])
    adjusted = pd.DataFrame(5.0, index=dates, columns=cols)
    execution_close = pd.DataFrame(20.0, index=dates, columns=cols)
    total_return_open = pd.DataFrame(5.0, index=dates, columns=cols)
    adjusted.attrs["execution_close"] = execution_close
    adjusted.attrs["total_return_open"] = total_return_open
    returns = pd.DataFrame(0.0, index=dates, columns=cols)
    volume = pd.DataFrame(1000.0, index=dates, columns=cols)
    mask = build_tradable_mask_integrity(
        index=dates,
        columns=cols,
        returns_df=returns,
        price_df=adjusted,
        open_df=execution_close,
        volume_df=volume,
        timing="next_open",
        tradability={
            "enabled": True,
            "min_price": 10.0,
            "min_dollar_volume": 0.0,
            "min_valid_return_lookback": 0,
            "min_valid_return_ratio": 0.0,
        },
    )
    assert mask["AAA"].all()


def test_relative_metrics_use_arithmetic_active_return_for_ir() -> None:
    strategy = pd.Series([0.02, 0.00], index=pd.date_range("2024-01-02", periods=2))
    benchmark = pd.Series([0.00, 0.00], index=strategy.index)
    summary = relative_performance_summary(strategy, benchmark)
    assert summary["ExcessAnnReturn"] == pytest.approx(0.01 * 252.0)
    expected_ir = 0.01 / np.std([0.02, 0.0], ddof=1) * np.sqrt(252.0)
    assert summary["InformationRatio"] == pytest.approx(expected_ir)
    assert summary["RelativeWealthAnnReturn"] != pytest.approx(
        summary["ExcessAnnReturn"]
    )


def test_newey_west_increases_se_for_positive_autocorrelation() -> None:
    rng = np.random.default_rng(7)
    values = np.zeros(500)
    shocks = rng.normal(0.0, 1.0, size=len(values))
    for i in range(1, len(values)):
        values[i] = 0.8 * values[i - 1] + shocks[i] + 0.05
    series = pd.Series(values)
    hac = newey_west_mean_stats(series, max_lag=4)
    naive_se = series.std(ddof=1) / np.sqrt(len(series))
    assert hac.max_lag == 4
    assert hac.standard_error > naive_se


def test_ic_summary_uses_forward_horizon_hac_lag() -> None:
    series = pd.Series(
        np.linspace(-0.05, 0.08, 100),
        index=pd.date_range("2024-01-02", periods=100, freq="B"),
    )
    series.attrs["forward_periods"] = 5
    summary = ic_summary(series)
    assert summary["HAC_lags"] == 4
    assert np.isfinite(summary["t_stat"])


def test_forward_returns_preserve_total_loss() -> None:
    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    returns = pd.DataFrame({"AAA": [0.0, -1.0, 0.0]}, index=dates)
    forward = compute_forward_returns(returns, periods=1)
    assert forward.loc[dates[0], "AAA"] == pytest.approx(-1.0)


def test_ic_selective_censoring_invalidates_cross_section_instead_of_survivor_drop() -> None:
    dates = pd.date_range("2024-01-02", periods=4, freq="B")
    factor = pd.DataFrame(
        {
            "AAA": [1.0, 1.0, 1.0, 1.0],
            "BBB": [2.0, 2.0, 2.0, 2.0],
            "CCC": [3.0, 3.0, 3.0, 3.0],
        },
        index=dates,
    )
    returns = pd.DataFrame(
        {
            "AAA": [0.0, 0.01, 0.02, 0.01],
            "BBB": [0.0, 0.02, 0.03, 0.02],
            "CCC": [0.0, np.nan, 0.04, 0.03],
        },
        index=dates,
    )
    ic = compute_ic(factor, returns, periods=1, min_stocks=2)
    assert ic.attrs["selectively_censored_dates"] >= 1
    assert ic.attrs["selectively_censored_observations"] >= 1
    diagnostics = ic.attrs["censor_diagnostics"]
    first = next(row for row in diagnostics if row["status"] == "selectively_censored")
    assert "CCC" in first["censored_tickers_sample"]
    assert pd.Timestamp(first["date"]) not in ic.index


def test_latest_known_industry_is_skipped_and_audited(monkeypatch) -> None:
    monkeypatch.setattr(
        neutralize_module,
        "CONFIG",
        SimpleNamespace(
            preprocessing=SimpleNamespace(
                neutralize_industry=True,
                neutralize_mcap=False,
                neutralize_min_obs=2,
            )
        ),
    )
    dates = pd.date_range("2024-01-02", periods=2, freq="B")
    factor = pd.DataFrame({"AAA": [1.0, 2.0], "BBB": [2.0, 3.0]}, index=dates)
    sector = pd.DataFrame({"sector": ["Tech", "Finance"]}, index=["AAA", "BBB"])
    sector.attrs["classification_policy"] = "LATEST_KNOWN_BACKFILL_NOT_PIT"
    result, audit = neutralize_module.neutralize_industry(
        factor,
        sector_map=sector,
        return_audit=True,
    )
    pd.testing.assert_frame_equal(result, factor)
    assert audit.requested_industry is True
    assert audit.enabled_industry is False
    assert audit.industry_temporal_policy == "LATEST_KNOWN_BACKFILL_NOT_PIT"
    assert "non_pit" in str(audit.industry_skip_reason)


def test_market_cap_neutralization_fails_closed_without_pit_matrix(monkeypatch) -> None:
    monkeypatch.setattr(
        neutralize_module,
        "CONFIG",
        SimpleNamespace(
            preprocessing=SimpleNamespace(
                neutralize_industry=False,
                neutralize_mcap=True,
                neutralize_min_obs=2,
            )
        ),
    )
    dates = pd.date_range("2024-01-02", periods=2, freq="B")
    factor = pd.DataFrame({"AAA": [1.0, 2.0], "BBB": [2.0, 3.0]}, index=dates)
    latest_mcap = pd.DataFrame({"market_cap": [1e9, 2e9]}, index=["AAA", "BBB"])
    latest_mcap.attrs["market_cap_policy"] = "LATEST_KNOWN_NOT_PIT"
    with pytest.raises(NeutralizationDataError, match="point-in-time"):
        neutralize_module.neutralize_industry(factor, mcap_df=latest_mcap)


def test_breakout_event_backtest_enters_next_open_and_uses_total_return(monkeypatch) -> None:
    dates = pd.bdate_range("2024-01-02", periods=100)
    raw = pd.DataFrame(
        {
            "open": 100.0,
            "high": 102.0,
            "low": 98.0,
            "close": 100.0,
            "adj_close": np.linspace(90.0, 100.0, len(dates)),
            "volume": 1_000_000.0,
        },
        index=dates,
    )

    def fake_setup(frame, *, ticker, **kwargs):
        status = "BREAKOUT" if len(frame) == 81 else "FORMING"
        return {
            "ticker": ticker,
            "status": status,
            "base_pass": True,
            "score": 90,
            "pivot": 100.0,
            "close": 100.0,
            "return_20d": 30.0,
            "adr_20d": 6.0,
            "prior_move": 50.0,
            "consolidation_days": 12,
            "tightness": 0.4,
            "volume_dryup": 0.7,
        }

    monkeypatch.setattr(breakout_history, "evaluate_daily_setup", fake_setup)
    cfg = BreakoutBacktestConfig(
        horizons=(1, 5),
        warmup_sessions=80,
        cooldown_sessions=20,
        round_trip_cost_bps=0.0,
    )
    result = backtest_breakout_frames(
        {"AAA": raw},
        config=cfg,
        start=dates[79],
        end=dates[-1],
    )
    assert len(result.events) == 1
    event = result.events.iloc[0]
    assert event["signal_date"] == dates[80].strftime("%Y-%m-%d")
    assert event["execution_date"] == dates[81].strftime("%Y-%m-%d")
    total_open = raw["open"] * (raw["adj_close"] / raw["close"])
    expected = total_open.iloc[82] / total_open.iloc[81] - 1.0
    assert event["h1_gross_return"] == pytest.approx(expected)
    assert result.summary["h1_observations"] == 1
