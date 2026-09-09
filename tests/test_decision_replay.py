from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.backtest.quintile import quintile_backtest
from src.decision_replay.builder import (
    build_backtest_snapshot,
    build_paper_snapshot,
)
from src.decision_replay.query import (
    _action,
    _event_rows,
    date_snapshot,
    replay_meta,
    stock_history,
)
from src.decision_replay.store import (
    load_snapshot,
    save_snapshot,
    upsert_snapshot,
)


def _synthetic_matrices() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    dates = pd.date_range("2026-01-05", periods=10, freq="B")
    tickers = [f"S{i}" for i in range(6)]
    base = np.arange(len(dates), dtype=float)[:, None]
    cross_section = np.arange(len(tickers), dtype=float)[None, :]
    scores = pd.DataFrame(
        cross_section + base * 0.01,
        index=dates,
        columns=tickers,
    )
    returns = pd.DataFrame(
        (cross_section - 2.5) * 0.001 + base * 0.00001,
        index=dates,
        columns=tickers,
    )
    prices = 100.0 * (1.0 + returns).cumprod()
    return scores, returns, prices


def test_backtest_snapshot_round_trip_and_audits(tmp_path):
    scores, returns, prices = _synthetic_matrices()
    tradable = pd.DataFrame(True, index=scores.index, columns=scores.columns)
    result = quintile_backtest(
        scores,
        returns,
        n_groups=3,
        rebalance_days=2,
        rebalance_mode="every_n_days",
        factor_direction=1,
        open_df=prices,
        price_df=prices,
        volume_df=pd.DataFrame(
            1_000_000.0,
            index=scores.index,
            columns=scores.columns,
        ),
        tradable_mask=tradable,
        execution={
            "timing": "next_open",
            "fee_model": "simple_bps",
            "slippage_model": "none",
            "commission_bps": 0.0,
            "slippage_bps": 0.0,
            "portfolio_value": 100_000.0,
        },
    )
    snapshot = build_backtest_snapshot(
        source_id="test-run",
        strategy_snapshot={
            "id": "strategy-1",
            "name": "single factor",
            "components": [{"factor_id": "MOM_TEST", "weight": 1.0}],
        },
        universe="TEST",
        composite=scores,
        factor_raw={"MOM_TEST": scores},
        factor_clean={"MOM_TEST": scores},
        factor_inputs={"MOM_TEST": scores},
        factor_contributions={"MOM_TEST": scores},
        close_prices=prices,
        market_returns=returns,
        volumes=pd.DataFrame(
            1_000_000.0,
            index=scores.index,
            columns=scores.columns,
        ),
        membership_mask=pd.DataFrame(
            True,
            index=scores.index,
            columns=scores.columns,
        ),
        result=result,
        n_groups=3,
        top_group=3,
        normalized_weights={"MOM_TEST": 1.0},
        execution=result.config["execution"],
        pit_diagnostics={"applied": True, "source": "test"},
    )

    assert snapshot.manifest["audit"]["max_factor_contribution_error"] == 0.0
    assert snapshot.manifest["audit"]["max_portfolio_contribution_error"] < 1e-12
    assert snapshot.signals["rank"].iloc[0]["S5"] == 1
    assert snapshot.signals["daily_signal_group"].iloc[0]["S5"] == 3
    decision_flags = snapshot.daily_summary["is_rebalance"].astype(bool)
    assert (
        decision_flags
        == snapshot.daily_summary["execution_date"].astype(str).ne("")
    ).all()
    assert snapshot.portfolio["decision_target_weights"].loc[
        ~decision_flags
    ].isna().all().all()

    save_snapshot(tmp_path, snapshot)
    loaded = load_snapshot(tmp_path)
    assert loaded is not None
    pd.testing.assert_frame_equal(
        loaded.signals["composite"],
        snapshot.signals["composite"],
        check_freq=False,
    )
    assert loaded.manifest["artifact_sha256"]
    meta = replay_meta(loaded)
    assert meta["latest_date"] == scores.index[-1].strftime("%Y-%m-%d")
    loaded.daily_summary.loc[scores.index[-1], "eligible_count"] = 0
    usable_meta = replay_meta(loaded)
    assert usable_meta["latest_date"] == scores.index[-2].strftime("%Y-%m-%d")
    assert date_snapshot(loaded, None)["date"] == usable_meta["latest_date"]
    date_payload = date_snapshot(loaded, meta["latest_date"])
    assert date_payload["rows"][0]["rank"] == 1
    assert date_payload["rows"][0]["ticker"] == "S5"
    stock_payload = stock_history(loaded, "S5")
    assert len(stock_payload["rows"]) == len(scores.index)


def test_paper_snapshot_upsert_replaces_same_date(tmp_path):
    scores, returns, prices = _synthetic_matrices()
    decision_date = scores.index[-1]
    target_table = pd.DataFrame(
        {
            "decision_date": decision_date.strftime("%Y-%m-%d"),
            "ticker": scores.columns,
            "score": scores.loc[decision_date].values,
            "group": [1, 1, 2, 2, 3, 3],
            "target_weight": [0, 0, 0, 0, 0.5, 0.5],
            "decision_price": prices.loc[decision_date].values,
        }
    )
    target = SimpleNamespace(
        decision_date=decision_date.strftime("%Y-%m-%d"),
        composite=scores,
        membership_mask=pd.DataFrame(
            True,
            index=scores.index,
            columns=scores.columns,
        ),
        tradable_mask=pd.DataFrame(
            True,
            index=scores.index,
            columns=scores.columns,
        ),
        effective_n_groups=3,
        top_group=3,
        target_weights=target_table,
        market_returns=returns,
        prices=prices,
        volumes=pd.DataFrame(
            1_000_000.0,
            index=scores.index,
            columns=scores.columns,
        ),
        factor_raw={"MOM_TEST": scores},
        factor_clean={"MOM_TEST": scores},
        factor_inputs={"MOM_TEST": scores},
        factor_contributions={"MOM_TEST": scores},
        normalized_weights={"MOM_TEST": 1.0},
        pit_diagnostics={"applied": True, "source": "test"},
    )
    account = {
        "id": "paper-1",
        "universe": "TEST",
        "initial_cash": 100_000.0,
        "execution": {"timing": "next_open"},
        "strategy_snapshot": {
            "id": "strategy-1",
            "name": "single factor",
            "components": [{"factor_id": "MOM_TEST", "weight": 1.0}],
        },
    }
    positions = pd.DataFrame(
        {"ticker": ["S5"], "weight": [0.4]}
    )
    first = build_paper_snapshot(
        source_id="paper-1",
        account=account,
        target=target,
        positions=positions,
        cash=60_000.0,
        equity=100_000.0,
        is_rebalance=True,
    )
    upsert_snapshot(tmp_path, first)

    second = build_paper_snapshot(
        source_id="paper-1",
        account=account,
        target=target,
        positions=positions,
        cash=59_000.0,
        equity=101_000.0,
        is_rebalance=True,
    )
    upsert_snapshot(tmp_path, second)
    loaded = load_snapshot(tmp_path)
    assert loaded is not None
    assert len(loaded.daily_summary) == 1
    assert loaded.daily_summary.iloc[0]["equity"] == 101_000.0
    assert loaded.manifest["trading_days"] == 1
    assert loaded.manifest["portfolio_contribution_available"] is False
    assert loaded.portfolio["daily_contributions"].isna().all().all()


def test_partial_fill_events_are_aggregated_without_claiming_full_fill():
    fills = pd.DataFrame([
        {
            "decision_date": "2026-01-05",
            "fill_date": "2026-01-06",
            "ticker": "A",
            "side": "BUY",
            "quantity": 2,
            "raw_open_price": 10.0,
            "fill_price": 10.1,
            "slippage_bps": 100.0,
            "slippage_cost": 0.2,
            "fee": 1.0,
            "total_cost_cash": 1.2,
        },
        {
            "decision_date": "2026-01-05",
            "fill_date": "2026-01-07",
            "ticker": "A",
            "side": "BUY",
            "quantity": 3,
            "raw_open_price": 11.0,
            "fill_price": 11.1,
            "slippage_bps": 90.0,
            "slippage_cost": 0.3,
            "fee": 1.0,
            "total_cost_cash": 1.3,
        },
    ])
    orders = pd.DataFrame([{
        "decision_date": "2026-01-05",
        "ticker": "A",
        "status": "pending",
        "side": "BUY",
        "quantity": 10,
    }])

    event = _event_rows(
        decision_date="2026-01-05",
        trades=None,
        orders=orders,
        fills=fills,
    )["A"]

    assert event["status"] == "partial"
    assert event["quantity"] == 5
    assert event["fill_price"] == pytest.approx(10.7)
    assert event["fee"] == 2.0
    assert event["total_cost_cash"] == 2.5


def test_excluded_existing_position_is_a_sell():
    assert _action(
        is_rebalance=True,
        eligible=False,
        current_weight=0.5,
        target_weight=0.0,
    ) == "卖出"
