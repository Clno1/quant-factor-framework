from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

import src.backtest.store as backtest_store
import src.papertrading.store as paper_store
import src.strategies.store as strategy_store
import src.watchlists.store as watchlist_store
from src.strategies.definition import StrategyComponent, StrategyDefinition
from src.utils.identifiers import InvalidResourceId
from src.watchlists.definition import WatchlistDefinition, WatchlistItem


def test_strategy_store_blocks_traversal_and_preserves_concurrent_creates(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "strategies"
    monkeypatch.setattr(strategy_store, "STRATEGY_ROOT", root)
    strategies = [
        StrategyDefinition.new(
            name=f"strategy-{index}",
            description="",
            components=[StrategyComponent("MOM_1M", 1.0)],
        )
        for index in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(strategy_store.create_strategy, strategies))

    assert {row["id"] for row in strategy_store.list_strategies()} == {
        strategy.id for strategy in strategies
    }
    with pytest.raises(InvalidResourceId):
        strategy_store.load_strategy("..")


def test_watchlist_store_blocks_traversal_and_preserves_concurrent_creates(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "watchlists"
    monkeypatch.setattr(watchlist_store, "WATCHLIST_ROOT", root)
    watchlists = [
        WatchlistDefinition.new(
            name=f"watchlist-{index}",
            items=[WatchlistItem(f"T{index}")],
        )
        for index in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(watchlist_store.create_watchlist, watchlists))

    rows = watchlist_store.list_watchlists()
    assert {row["id"] for row in rows} == {
        watchlist.id for watchlist in watchlists
    }
    assert all(row["universe_type"] == "TARGET" for row in rows)
    assert all(row["ticker_revision_sha256"].startswith("sha256:") for row in rows)

    activity = watchlist_store.record_ranking_activity(
        watchlists[0].id,
        strategy_id=str(uuid4()),
        decision_date="2026-07-20",
        data_contract={"dataset_version_id": "data-v1"},
    )
    assert activity["decision_date"] == "2026-07-20"
    assert watchlist_store.load_ranking_activity(watchlists[0].id) == activity
    with pytest.raises(InvalidResourceId):
        watchlist_store.load_watchlist("../strategies")


def test_backtest_sqlite_updates_are_serialized(monkeypatch, tmp_path):
    root = tmp_path / "backtests"
    monkeypatch.setattr(backtest_store, "BACKTEST_ROOT", root)
    strategy = StrategyDefinition.new(
        name="backtest-strategy",
        description="",
        components=[StrategyComponent("MOM_1M", 1.0)],
    )
    tasks = [
        backtest_store.create_task(
            strategy=strategy,
            universe="MAG7",
            start="2026-01-01",
            end="2026-01-31",
            resolved_start="2026-01-01",
            resolved_end="2026-01-31",
            n_groups=5,
            rebalance_days=5,
            top_group=5,
            name=f"task-{index}",
        )
        for index in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda task: backtest_store.update_task(
                    task["id"], {"status": "success"}
                ),
                tasks,
            )
        )

    assert {row["id"] for row in backtest_store.list_tasks()} == {
        task["id"] for task in tasks
    }


def test_paper_sqlite_creates_are_serialized(monkeypatch, tmp_path):
    root = tmp_path / "papertrading"
    monkeypatch.setattr(paper_store, "PAPER_ROOT", root)
    accounts = [
        {
            "id": str(uuid4()),
            "name": f"paper-{index}",
            "status": "active",
            "created_at": f"2026-01-0{index + 1}",
        }
        for index in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(paper_store.create_account, accounts))

    assert {row["id"] for row in paper_store.list_accounts()} == {
        account["id"] for account in accounts
    }
