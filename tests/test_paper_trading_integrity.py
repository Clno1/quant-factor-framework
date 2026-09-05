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


def _cross_day_fixture(monkeypatch, tmp_path):
    _paper_root(monkeypatch, tmp_path)
    account = _account(str(uuid4()))
    account["execution"]["slippage_model"] = "none"
    dates = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    target = SimpleNamespace(decision_date="2026-01-07", data_contract={"dataset_version_id": "new"},
                             open_prices=pd.DataFrame({"A": [100., np.nan, 100.], "B": [100., 100., 100.]}, index=dates),
                             volumes=pd.DataFrame(1_000_000., index=dates, columns=["A", "B"]))
    ledger = pd.DataFrame([dict(fill_id="initial", order_id="initial-order", ticker="A", side="BUY", quantity=10,
                                fill_date="2026-01-05", filled_at="2026-01-05T14:30:00Z", raw_open_price=100.,
                                fill_price=100., notional=1000., fee=0., dataset_version_id="old")])
    orders = pd.DataFrame([dict(order_id=f"order-{side}", ticker=ticker, side=side, quantity=10,
                                decision_date="2026-01-05", status="pending", ref_price=100.)
                           for side, ticker in (("SELL", "A"), ("BUY", "B"))])
    store.save_table(account["id"], "fills", ledger)
    store.save_table(account["id"], "orders", orders)
    return account, target, ledger


def test_cross_day_pending_orders_cannot_use_future_sale_cash(monkeypatch, tmp_path):
    account, target, ledger = _cross_day_fixture(monkeypatch, tmp_path)
    cash, positions = runner._state_from_fill_ledger(account, ledger)
    cash, fills, orders = runner._fill_pending_orders(account=account, target=target,
        cash=cash, positions_map=positions, cutoff="2026-01-07")
    assert [(row["side"], row["fill_date"]) for row in fills] == [("SELL", "2026-01-07")]
    assert orders.set_index("side").loc["BUY", "reject_reason"] == "insufficient_cash"
    assert cash == pytest.approx(1000.)
    assert positions == {}
    runner._state_from_fill_ledger(account, store.load_table(account["id"], "fills"))


def test_failed_order_projection_retry_cannot_backdate_a_buy(monkeypatch, tmp_path):
    account, target, ledger = _cross_day_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "now_iso", lambda: "2026-01-07T21:00:00Z")
    save = runner.save_table
    def fail_orders(account_id, name, frame):
        if name == "orders":
            raise OSError("interrupted projection")
        return save(account_id, name, frame)
    monkeypatch.setattr(runner, "save_table", fail_orders)
    cash, positions = runner._state_from_fill_ledger(account, ledger)
    with pytest.raises(OSError, match="interrupted projection"):
        runner._fill_pending_orders(account=account, target=target, cash=cash, positions_map=positions, cutoff="2026-01-07")
    monkeypatch.setattr(runner, "save_table", save)
    cash, positions = runner._state_from_fill_ledger(account, store.load_table(account["id"], "fills"))
    _, fills, _ = runner._fill_pending_orders(account=account, target=target, cash=cash, positions_map=positions, cutoff="2026-01-07")
    assert [(row["side"], row["fill_date"]) for row in fills] == [("BUY", "2026-01-07")]
    cash, _ = runner._state_from_fill_ledger(account, store.load_table(account["id"], "fills"))
    assert cash == pytest.approx(0.)


def test_fill_ledger_rejects_funding_from_later_cash_events():
    ledger = pd.DataFrame([dict(fill_id="buy", ticker="A", side="BUY", quantity=1,
                                fill_date="2026-01-06", fill_price=100., notional=100., fee=0.)])
    cash_events = pd.DataFrame([dict(event_id="cash", date="2026-01-07", amount=100.)])
    with pytest.raises(ValueError, match="unavailable cash"):
        runner._state_from_fill_ledger({"initial_cash": 0.}, ledger, cash_events)
    cash_events["date"] = "2026-01-06"
    assert runner._state_from_fill_ledger({"initial_cash": 0.}, ledger, cash_events)[0] == 0.


def test_future_dividend_does_not_increase_an_earlier_order_budget(monkeypatch, tmp_path):
    account, target, ledger = _cross_day_fixture(monkeypatch, tmp_path)
    ledger.loc[:, "quantity"] = 5
    ledger.loc[:, "notional"] = 500.
    store.save_table(account["id"], "fills", ledger)
    store.save_table(account["id"], "orders", pd.DataFrame([dict(
        order_id="buy", ticker="B", side="BUY", quantity=6, decision_date="2026-01-05", status="pending")]))
    target.prices = pd.DataFrame({"A": [100., 100., 80.], "B": [100., 100., 100.]}, index=target.open_prices.index)
    target.total_return_close_prices = target.prices * 0 + 100.
    cash, positions = runner._state_from_fill_ledger(account, ledger)
    _, fills, _ = runner._fill_pending_orders(account=account, target=target, cash=cash, positions_map=positions, cutoff="2026-01-07")
    assert fills[0]["quantity"] == 5
    assert fills[0]["fill_date"] == "2026-01-06"


def test_split_units_block_before_valuation_but_ordinary_moves_do_not(monkeypatch, tmp_path):
    account, target, ledger = _cross_day_fixture(monkeypatch, tmp_path)
    target.prices = target.open_prices.ffill()
    runner._validate_execution_units(account=account, target=target, fills=ledger, orders=pd.DataFrame())
    target.open_prices.loc["2026-01-05", "A"] = 50.
    with pytest.raises(ValueError, match="PAPER_PRICE_UNITS_CHANGED"):
        runner._validate_execution_units(account=account, target=target, fills=ledger, orders=pd.DataFrame())
    target.open_prices.loc["2026-01-05", "A"] = 100.
    target.prices.loc["2026-01-07", "A"] = 50.  # An ordinary current-day drop.
    runner._validate_execution_units(account=account, target=target, fills=ledger, orders=pd.DataFrame())
    target.prices.loc["2026-01-05", "B"] = 50.
    pending = store.load_table(account["id"], "orders").query("side == 'BUY'")
    with pytest.raises(ValueError, match="PAPER_PRICE_UNITS_CHANGED"):
        runner._validate_execution_units(account=account, target=target, fills=pd.DataFrame(), orders=pending)


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


def test_dividend_derivation_ignores_fmp_cent_quantization_noise():
    dates = pd.DatetimeIndex(["2021-11-17", "2021-11-18"])
    target = SimpleNamespace(
        prices=pd.DataFrame({"LUNA": [9.295, 9.380]}, index=dates),
        total_return_close_prices=pd.DataFrame(
            {"LUNA": [9.30, 9.38]},
            index=dates,
        ),
    )

    distributions = runner._derived_dividend_cash_per_share(target)

    assert distributions.loc[pd.Timestamp("2021-11-18"), "LUNA"] == 0.0


def test_dividend_derivation_rejects_negative_beyond_source_precision():
    dates = pd.DatetimeIndex(["2026-01-05", "2026-01-06"])
    target = SimpleNamespace(
        prices=pd.DataFrame({"A": [100.0, 100.0]}, index=dates),
        total_return_close_prices=pd.DataFrame(
            {"A": [100.0, 90.0]},
            index=dates,
        ),
    )

    with pytest.raises(ValueError, match="beyond source precision"):
        runner._derived_dividend_cash_per_share(target)


def test_historical_asof_resolves_previous_xnys_session():
    assert runner._expected_target_session("2026-01-04") == pd.Timestamp(
        "2026-01-02"
    )


def test_historical_asof_after_latest_session_resolves_previous_session():
    assert runner._expected_target_session("2026-08-29") == pd.Timestamp(
        "2026-08-28"
    )
