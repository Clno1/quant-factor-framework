from __future__ import annotations

from src.backtest import store as backtest_store
from src.papertrading.definition import create_account_payload
from src.papertrading import store as paper_store
from src.strategies.definition import StrategyComponent, StrategyDefinition


def _strategy() -> StrategyDefinition:
    return StrategyDefinition.new(
        name="Frozen inputs",
        description="",
        components=[StrategyComponent("MOM_1M", 1.0)],
    )


def _execution() -> dict:
    return {
        "timing": "next_open",
        "fee_model": "simple_bps",
        "commission_bps": 3.0,
        "slippage_model": "constant_bps",
        "slippage_bps": 7.0,
    }


def _snapshots() -> tuple[dict, dict, dict]:
    research = {
        "schema_version": 1,
        "components": [{"factor_id": "MOM_1M", "verdict": "ROBUST"}],
    }
    target = {
        "universe_type": "TARGET",
        "requested_universe": "watchlist:example",
        "ticker_revision_sha256": "sha256:frozen",
    }
    risk = {
        "require_point_in_time_universe": True,
        "tradability": {"enabled": False, "min_price": 123.0},
    }
    return research, target, risk


def test_backtest_creation_deep_freezes_complete_inputs(monkeypatch, tmp_path):
    monkeypatch.setattr(
        backtest_store,
        "BACKTEST_ROOT",
        tmp_path / "backtests",
    )
    research, target, risk = _snapshots()
    task = backtest_store.create_task(
        strategy=_strategy(),
        universe="SP500",
        start="2026-01-01",
        end="2026-02-01",
        resolved_start="2026-01-02",
        resolved_end="2026-01-30",
        n_groups=5,
        rebalance_days=5,
        top_group=5,
        execution=_execution(),
        research_evidence_snapshot=research,
        target_universe_snapshot=target,
        risk_config=risk,
    )

    research["components"][0]["verdict"] = "MUTATED"
    target["ticker_revision_sha256"] = "sha256:mutated"
    risk["tradability"]["min_price"] = 999.0
    restored = backtest_store.load_task(task["id"])

    assert restored is not None
    assert restored["schema_version"] == 2
    assert restored["research_evidence_snapshot"]["components"][0][
        "verdict"
    ] == "ROBUST"
    assert restored["target_universe_snapshot"][
        "ticker_revision_sha256"
    ] == "sha256:frozen"
    assert restored["risk_config"]["tradability"]["min_price"] == 123.0
    assert "min_dollar_volume" in restored["risk_config"]["tradability"]
    assert restored["execution"]["commission_bps"] == 3.0
    assert restored["execution"]["fees"]["include_regulatory"] is True
    assert restored["execution"]["slippage"]["adv_window"] == 20


def test_paper_creation_deep_freezes_complete_inputs(monkeypatch, tmp_path):
    monkeypatch.setattr(
        paper_store,
        "PAPER_ROOT",
        tmp_path / "papertrading",
    )
    research, target, risk = _snapshots()
    account = create_account_payload(
        name="Frozen paper",
        strategy=_strategy(),
        universe="SP500",
        watchlist_snapshot=None,
        initial_cash=100_000,
        n_groups=5,
        top_group=5,
        rebalance_mode="month_end",
        execution=_execution(),
        research_evidence_snapshot=research,
        target_universe_snapshot=target,
        risk_config=risk,
    )
    paper_store.create_account(account)

    research["components"][0]["verdict"] = "MUTATED"
    target["ticker_revision_sha256"] = "sha256:mutated"
    risk["tradability"]["min_price"] = 999.0
    restored = paper_store.load_account(account["id"])

    assert restored is not None
    assert restored["schema_version"] == 2
    assert restored["research_evidence_snapshot"]["components"][0][
        "verdict"
    ] == "ROBUST"
    assert restored["target_universe_snapshot"][
        "ticker_revision_sha256"
    ] == "sha256:frozen"
    assert restored["risk_config"]["tradability"]["min_price"] == 123.0
    assert "min_dollar_volume" in restored["risk_config"]["tradability"]
    assert restored["execution"]["commission_bps"] == 3.0
    assert restored["execution"]["fees"]["include_regulatory"] is True
    assert restored["execution"]["slippage"]["adv_window"] == 20
