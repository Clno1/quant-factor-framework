from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from src.papertrading import runner
from src.papertrading import store


def _paper_root(monkeypatch, tmp_path):
    root = tmp_path / "papertrading"
    monkeypatch.setattr(store, "PAPER_ROOT", root)
    return root


def _account(account_id: str) -> dict:
    return {
        "id": account_id,
        "initial_cash": 1_000.0,
        "rebalance_mode": "every_n_days",
        "rebalance_days": 2,
        "execution": {
            "timing": "next_open",
            "fee_model": "simple_bps",
            "commission_bps": 0.0,
            "slippage_model": "volume_share",
            "slippage": {
                "volume_limit": 0.025,
                "adv_window": 20,
                "price_impact": 0.0,
                "spread_bps": 0.0,
            },
        },
    }


def _target() -> SimpleNamespace:
    dates = pd.DatetimeIndex([
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
    ])
    return SimpleNamespace(
        decision_date="2026-01-06",
        open_prices=pd.DataFrame({"A": [10.0, 10.0, 10.0]}, index=dates),
        volumes=pd.DataFrame({"A": [100.0, 1_000_000.0, 100.0]}, index=dates),
    )


def _pending_order(account_id: str) -> pd.DataFrame:
    return pd.DataFrame([{
        "order_id": str(uuid4()),
        "account_id": account_id,
        "decision_date": "2026-01-05",
        "ticker": "A",
        "side": "BUY",
        "quantity": 5,
        "filled_quantity": 0,
        "status": "pending",
    }])


def test_fill_ledger_makes_partial_fill_retry_idempotent(
    monkeypatch,
    tmp_path,
):
    _paper_root(monkeypatch, tmp_path)
    account_id = str(uuid4())
    account = _account(account_id)
    target = _target()
    store.save_table(account_id, "orders", _pending_order(account_id))

    cash, first_fills, orders = runner._fill_pending_orders(
        account=account,
        target=target,
        cash=1_000.0,
        positions_map={},
        cutoff="2026-01-06",
    )
    assert len(first_fills) == 1
    assert first_fills[0]["quantity"] == 2
    assert orders.iloc[0]["status"] == "pending"

    ledger = store.load_table(account_id, "fills")
    rebuilt_cash, rebuilt_positions = runner._state_from_fill_ledger(
        account,
        ledger,
    )
    assert rebuilt_cash == pytest.approx(cash)
    assert rebuilt_positions["A"]["quantity"] == 2

    retry_cash, retry_fills, _ = runner._fill_pending_orders(
        account=account,
        target=target,
        cash=rebuilt_cash,
        positions_map=rebuilt_positions,
        cutoff="2026-01-06",
    )
    assert retry_fills == []
    assert retry_cash == pytest.approx(rebuilt_cash)
    assert len(store.load_table(account_id, "fills")) == 1


def test_pending_order_cannot_fill_beyond_asof(monkeypatch, tmp_path):
    _paper_root(monkeypatch, tmp_path)
    account_id = str(uuid4())
    account = _account(account_id)
    store.save_table(account_id, "orders", _pending_order(account_id))

    cash, fills, orders = runner._fill_pending_orders(
        account=account,
        target=_target(),
        cash=1_000.0,
        positions_map={},
        cutoff="2026-01-05",
    )

    assert cash == 1_000.0
    assert fills == []
    assert orders.iloc[0]["status"] == "pending"


def test_paper_rebalance_mode_is_enforced():
    dates = pd.date_range("2026-01-05", periods=5, freq="B")
    every_n_target = SimpleNamespace(
        decision_date=dates[1].strftime("%Y-%m-%d"),
        composite=pd.DataFrame(index=dates),
        prices=pd.DataFrame(index=dates),
    )
    assert runner._is_rebalance_decision(
        {"rebalance_mode": "every_n_days", "rebalance_days": 2},
        every_n_target,
    ) is False

    month_end_target = SimpleNamespace(
        decision_date="2026-01-30",
        composite=pd.DataFrame(index=pd.DatetimeIndex(["2026-01-30"])),
        prices=pd.DataFrame(
            index=pd.DatetimeIndex(["2026-01-30", "2026-02-02"])
        ),
    )
    assert runner._is_rebalance_decision(
        {"rebalance_mode": "month_end"},
        month_end_target,
    ) is True


def test_held_position_without_asof_mark_price_fails():
    with pytest.raises(ValueError, match="Cannot mark held position"):
        runner._positions_from_map(
            {"A": {"quantity": 2.0, "avg_price": 10.0}},
            prices=pd.Series({"A": np.nan}),
            equity=1_000.0,
        )


def test_duplicate_fill_ids_are_rejected():
    account = {"initial_cash": 1_000.0}
    fill_id = str(uuid4())
    fills = pd.DataFrame([
        {
            "fill_id": fill_id,
            "ticker": "A",
            "side": "BUY",
            "quantity": 1,
            "fill_price": 10.0,
            "notional": 10.0,
            "fee": 0.0,
        },
        {
            "fill_id": fill_id,
            "ticker": "A",
            "side": "BUY",
            "quantity": 1,
            "fill_price": 10.0,
            "notional": 10.0,
            "fee": 0.0,
        },
    ])
    with pytest.raises(ValueError, match="Duplicate fill_id"):
        runner._state_from_fill_ledger(account, fills)


def test_dividend_cash_ledger_is_economic_and_idempotent(monkeypatch, tmp_path):
    _paper_root(monkeypatch, tmp_path)
    account_id = str(uuid4())
    account = _account(account_id)
    account["initial_cash"] = 2_000.0
    dates = pd.DatetimeIndex([
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
    ])
    target = SimpleNamespace(
        decision_date="2026-01-07",
        prices=pd.DataFrame({"A": [100.0, 99.0, 100.0]}, index=dates),
        total_return_close_prices=pd.DataFrame(
            {"A": [100.0, 100.0, 100.0 * (100.0 / 99.0)]},
            index=dates,
        ),
        data_contract={"dataset_version_id": "dataset-v1"},
    )
    fills = pd.DataFrame([{
        "fill_id": str(uuid4()),
        "fill_date": "2026-01-05",
        "filled_at": "2026-01-05T14:30:00+00:00",
        "ticker": "A",
        "side": "BUY",
        "quantity": 10.0,
        "fill_price": 100.0,
        "notional": 1_000.0,
        "fee": 0.0,
    }])

    first = runner._accrue_dividend_cash_events(
        account=account,
        target=target,
        fills=fills,
    )
    second = runner._accrue_dividend_cash_events(
        account=account,
        target=target,
        fills=fills,
    )

    assert len(first) == 1
    assert len(second) == 1
    assert first.iloc[0]["date"] == "2026-01-06"
    assert first.iloc[0]["quantity"] == pytest.approx(10.0)
    assert first.iloc[0]["amount_per_share"] == pytest.approx(1.0)
    assert first.iloc[0]["amount"] == pytest.approx(10.0)
    assert first.iloc[0]["dataset_version_id"] == "dataset-v1"
    cash, positions = runner._state_from_fill_ledger(account, fills, second)
    assert cash == pytest.approx(1_010.0)
    assert positions["A"]["quantity"] == pytest.approx(10.0)


def test_historical_asof_resolves_previous_xnys_session():
    assert runner._expected_target_session("2026-01-04") == pd.Timestamp(
        "2026-01-02"
    )
