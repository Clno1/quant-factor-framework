from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.webapp.app import create_app
from src.webapp.routes_v2 import (
    _backtest_failure_view,
    _backtest_group_view,
    _backtest_timing_view,
)
from src.strategies.definition import StrategyComponent, StrategyDefinition


def test_backtest_detail_renders_factor_directions_from_frozen_snapshot():
    task_id = "db026d38-0b27-46a5-bdf8-3d26240fe26a"
    task = {
        "id": task_id,
        "name": "Template regression",
        "universe": "SP500",
        "strategy_snapshot": {
            "name": "Momentum and low volatility",
            "components": [
                {"factor_id": "MOM_12M", "weight": 0.6},
                {"factor_id": "VOL_60D", "weight": -0.4},
            ],
        },
        "date_range": {
            "resolved_start": "2022-08-01",
            "resolved_end": "2026-08-07",
        },
        "n_groups": 5,
        "top_group": 5,
        "rebalance_mode": "monthly",
        "rebalance_days": 20,
        "execution": {
            "timing": "next_open",
            "fee_model": "ibkr_us_pro_fixed",
            "slippage_model": "volume_share",
        },
        "research_evidence_snapshot": {
            "captured_at": "2026-08-08T01:00:00+00:00",
            "cross_publication": {
                "status": "PUBLISHED",
                "target_session": "2026-08-07",
                "generation_id": "cross-v1",
            },
            "components": [
                {
                    "factor_id": "MOM_12M",
                    "weight": 0.6,
                    "sp500_verdict": "PASS",
                    "nasdaq100_verdict": "WATCH",
                    "verdict": "ROBUST",
                    "research_target_session": "2026-08-07",
                }
            ],
        },
        "target_universe_snapshot": {
            "requested_universe": "SP500",
            "universe_type": "RESEARCH_PRESET_AS_TARGET",
            "universe_id": "SP500",
        },
        "risk_config": {
            "require_point_in_time_universe": True,
            "tradability": {"enabled": True, "min_price": 1.0},
        },
        "status": "pending",
        "diagnostics": None,
        "metrics": None,
    }

    with patch("src.webapp.app._recover_application_state", return_value=(0, 0)):
        with patch("src.webapp.routes_v2.bt_store.load_task", return_value=task):
            with TestClient(create_app()) as client:
                response = client.get(f"/backtests/{task_id}")

    assert response.status_code == 200
    assert ">+1<" in response.text
    assert ">-1<" in response.text
    assert "创建时冻结的研究与数据契约" in response.text
    assert "cross-v1" in response.text
    assert "ROBUST" in response.text


def test_backtest_timing_uses_persisted_task_timestamps():
    now = datetime.fromisoformat("2026-08-11T12:10:00")
    running = _backtest_timing_view(
        {
            "status": "running",
            "created_at": "2026-08-11T12:00:00",
            "started_at": "2026-08-11T12:07:00",
        },
        now=now,
    )
    waiting = _backtest_timing_view(
        {
            "status": "waiting_for_data",
            "created_at": "2026-08-11T12:00:00",
            "status_changed_at": "2026-08-11T12:08:00",
            "started_at": None,
        },
        now=now,
    )

    assert running == {
        "active": True,
        "stage": "running",
        "total_elapsed_sec": 600.0,
        "stage_elapsed_sec": 180.0,
    }
    assert waiting == {
        "active": True,
        "stage": "waiting_for_data",
        "total_elapsed_sec": 600.0,
        "stage_elapsed_sec": 120.0,
    }


def test_backtest_group_view_recovers_legacy_small_watchlist_execution_plan():
    view = _backtest_group_view({
        "n_groups": 5,
        "top_group": 5,
        "watchlist_snapshot": {
            "items": [
                {"ticker": ticker}
                for ticker in ("SPCX", "RKLB", "ASTS", "RDW", "ECHO", "SATS")
            ],
        },
        "diagnostics": {"capacity_error": {"code": "ADV_CAPACITY_EXCEEDED"}},
    })

    assert view == {
        "requested_n_groups": 5,
        "effective_n_groups": 3,
        "requested_top_group": 5,
        "effective_top_group": 3,
        "n_tickers": 6,
        "small_universe_adjusted": True,
    }


def test_backtest_detail_renders_terminal_wall_clock_and_compute_time():
    task_id = "db026d38-0b27-46a5-bdf8-3d26240fe26c"
    task = {
        "id": task_id,
        "name": "Failed after waiting",
        "universe": "SP500",
        "strategy_snapshot": {"name": "Momentum", "components": []},
        "date_range": {
            "resolved_start": "2026-01-01",
            "resolved_end": "2026-02-01",
        },
        "n_groups": 5,
        "top_group": 5,
        "rebalance_mode": "month_end",
        "rebalance_days": 5,
        "execution": {"timing": "next_open"},
        "status": "failed",
        "created_at": "2026-08-11T12:00:00",
        "started_at": "2026-08-11T12:09:52",
        "finished_at": "2026-08-11T12:10:00",
        "duration_sec": 8.0,
        "diagnostics": None,
        "metrics": None,
        "error": "test failure",
    }

    with patch("src.webapp.app._recover_application_state", return_value=(0, 0)):
        with patch("src.webapp.routes_v2.bt_store.load_task", return_value=task):
            with TestClient(create_app()) as client:
                response = client.get(f"/backtests/{task_id}")

    assert response.status_code == 200
    assert "总历时：<span class=\"mono\">600.0 秒</span>" in response.text
    assert "实际计算：<span class=\"mono\">8.0 秒</span>" in response.text


def test_backtest_detail_renders_per_security_cost_ledger():
    task_id = "db026d38-0b27-46a5-bdf8-3d26240fe26b"
    task = {
        "id": task_id,
        "name": "Execution ledger",
        "universe": "SP500",
        "strategy_snapshot": {
            "name": "Momentum",
            "components": [{"factor_id": "MOM_12M", "weight": 1.0}],
        },
        "date_range": {
            "resolved_start": "2026-01-01",
            "resolved_end": "2026-02-01",
        },
        "n_groups": 5,
        "top_group": 5,
        "rebalance_mode": "month_end",
        "rebalance_days": 5,
        "execution": {
            "timing": "next_open",
            "fee_model": "ibkr_us_pro_fixed",
            "slippage_model": "volume_share",
        },
        "status": "success",
        "diagnostics": {
            "composite_shape": [20, 10],
            "composite_date_start": "2026-01-01",
            "composite_date_end": "2026-02-01",
            "effective_n_groups": 5,
            "effective_top_group": 5,
            "n_trading_days": 20,
            "execution_used": {
                "timing": "next_open",
                "fee_model": "ibkr_us_pro_fixed",
                "slippage_model": "volume_share",
                "slippage_bps": 5.0,
                "commission_bps": 2.0,
            },
            "cost_bps_per_year": 12.5,
        },
        "duration_sec": 1.25,
        "metrics": {},
        "research_evidence_snapshot": {},
        "target_universe_snapshot": {},
        "risk_config": {},
        "data_contract": {},
    }
    artifacts = {
        "nav": pd.DataFrame(),
        "holdings_detail": pd.DataFrame(
            [{
                "decision_date": "2026-01-30",
                "date": "2026-02-02",
                "ticker": "AAPL",
                "target_weight": 0.1,
            }]
        ),
        "trades": pd.DataFrame(
            [{
                "decision_date": "2026-01-30",
                "date": "2026-02-02",
                "ticker": "AAPL",
                "side": "BUY",
                "old_weight": 0.0,
                "new_weight": 0.1,
                "estimated_quantity": 10.0,
                "raw_price": 100.0,
                "fill_price": 100.05,
                "participation_rate": 0.001,
                "slippage_bps": 5.0,
                "slippage_cost": 0.5,
                "broker_commission": 1.0,
                "sec_fee": 0.1234,
                "finra_taf": 0.02,
                "finra_cat": 0.003,
                "clearing_fee": 0.04,
                "pass_through_fee": 0.01,
                "exchange_fee": 0.0,
                "fee": 1.1964,
                "total_cost_cash": 1.6964,
            }]
        ),
        "costs": pd.DataFrame(
            [{
                "decision_date": "2026-01-30",
                "date": "2026-02-02",
                "turnover": 0.1,
                "traded_weight": 0.1,
                "avg_slippage_bps": 5.0,
                "total_slippage_cost": 0.5,
                "total_fee": 1.1964,
                "total_cost_cash": 1.6964,
            }]
        ),
    }

    with patch("src.webapp.app._recover_application_state", return_value=(0, 0)):
        with patch("src.webapp.routes_v2.bt_store.load_task", return_value=task):
            with patch(
                "src.webapp.routes_v2.bt_store.load_task_artifacts",
                return_value=artifacts,
            ):
                with TestClient(create_app()) as client:
                    response = client.get(f"/backtests/{task_id}")

    assert response.status_code == 200
    assert "逐票成交与费用明细" in response.text
    assert "FINRA TAF" in response.text
    assert "0.1234" in response.text
    assert "AAPL" in response.text
    assert "UNKNOWN" in response.text


def test_legacy_adv_failure_is_rendered_as_actionable_capacity_diagnosis():
    task = {
        "id": "125eaa41-9a9a-41d6-b1e5-543dd07bde0f",
        "strategy_id": "028feb7a-cc15-4f49-61d4-278e6cb2d8a2",
        "universe": "watchlist:36d324f4-4391-42b3-855b-3f9c91cfae80",
        "watchlist_snapshot": {
            "items": [
                {"ticker": ticker}
                for ticker in ("SPCX", "RKLB", "ASTS", "RDW", "ECHO", "SATS")
            ],
        },
        "status": "failed",
        "execution": {
            "portfolio_value": 100_000.0,
            "slippage": {"volume_limit": 0.025},
        },
        "error": (
            "ValueError: Backtest order exceeds the configured ADV fill limit: "
            "decision_date=2022-07-29 ticker=RDW requested=15060.2410 "
            "allowed=3100.5625 adv=124022.5.\n\nTraceback (most recent call last):"
        ),
    }

    failure = _backtest_failure_view(task)

    assert failure is not None
    assert failure["kind"] == "capacity"
    assert failure["order"]["ticker"] == "RDW"
    assert failure["order"]["participation_rate"] == pytest.approx(0.1214315)
    assert failure["max_portfolio_value"] == pytest.approx(20_587.73495)
    assert failure["suggested_portfolio_value"] == 18_500.0
    assert failure["recommendation_is_full_period"] is False
    assert "portfolio_value=18500.00" in failure["retry_url"]


def test_structured_capacity_failure_uses_full_period_safe_recommendation():
    task = {
        "id": "125eaa41-9a9a-41d6-b1e5-543dd07bde0f",
        "strategy_id": "028feb7a-cc15-4f49-61d4-278e6cb2d8a2",
        "universe": "SP500",
        "status": "failed",
        "error": "structured capacity failure",
        "error_details": {
            "code": "ADV_CAPACITY_EXCEEDED",
            "breach_count": 1,
            "portfolio_value": 18_500.0,
            "max_portfolio_value": 16_352.7771,
            "worst_order": {
                "decision_date": "2023-10-31",
                "ticker": "RDW",
                "requested_quantity": 3451.49,
                "allowed_quantity": 3050.89,
                "adv": 122035.65,
                "participation_rate": 0.02828,
                "volume_limit": 0.025,
            },
        },
    }

    failure = _backtest_failure_view(task)

    assert failure is not None
    assert failure["recommendation_is_full_period"] is True
    assert failure["suggested_portfolio_value"] == 14_700.0


def test_backtest_detail_hides_capacity_traceback_behind_chinese_diagnosis():
    task_id = "125eaa41-9a9a-41d6-b1e5-543dd07bde0f"
    task = {
        "id": task_id,
        "name": "Capacity failure",
        "strategy_id": "028feb7a-cc15-4f49-61d4-278e6cb2d8a2",
        "strategy_snapshot": {"name": "Momentum", "components": []},
        "universe": "watchlist:36d324f4-4391-42b3-855b-3f9c91cfae80",
        "watchlist_snapshot": {
            "items": [
                {"ticker": ticker}
                for ticker in ("SPCX", "RKLB", "ASTS", "RDW", "ECHO", "SATS")
            ],
        },
        "date_range": {
            "resolved_start": "2021-08-10",
            "resolved_end": "2026-08-10",
        },
        "n_groups": 5,
        "top_group": 5,
        "rebalance_mode": "month_end",
        "rebalance_days": 5,
        "status": "failed",
        "execution": {
            "timing": "next_open",
            "portfolio_value": 100_000.0,
            "slippage": {"volume_limit": 0.025},
        },
        "error": (
            "ValueError: Backtest order exceeds the configured ADV fill limit: "
            "decision_date=2022-07-29 ticker=RDW requested=15060.2410 "
            "allowed=3100.5625 adv=124022.5.\n\nTraceback (most recent call last):"
        ),
        "metrics": None,
        "diagnostics": None,
    }

    with patch("src.webapp.app._recover_application_state", return_value=(0, 0)):
        with patch("src.webapp.routes_v2.bt_store.load_task", return_value=task):
            with TestClient(create_app()) as client:
                response = client.get(f"/backtests/{task_id}")

    assert response.status_code == 200
    assert "组合规模超过历史流动性容量" in response.text
    assert "实际分组：3（Top = Q3）" in response.text
    assert "6 只股票，由原 5 组自动调整" in response.text
    assert "$100,000.00" in response.text
    assert "$20,587.73" in response.text
    assert "12.14% / 2.50%" in response.text
    assert "按初步估算资金" in response.text
    assert "$18,500 新建回测" in response.text
    assert "旧任务只记录首个超限订单" in response.text
    assert "<summary>技术详情</summary>" in response.text


def test_backtest_form_and_api_preserve_requested_portfolio_value():
    strategy = StrategyDefinition.new(
        "Momentum",
        "",
        [StrategyComponent("MOM_6M", 1.0)],
    )
    summary = {"id": strategy.id, "name": strategy.name, "n_components": 1}
    captured = {}

    def fake_create_task(**kwargs):
        captured.update(kwargs)
        return {"id": "d8b4f723-f326-4771-9875-70024c19e9f4", "status": "pending"}

    runner = MagicMock()
    with patch("src.webapp.app._recover_application_state", return_value=(0, 0)):
        with patch("src.webapp.routes_v2.list_strategies", return_value=[summary]):
            with patch("src.webapp.routes_v2._strategy_research_map", return_value={}):
                with patch("src.webapp.routes_v2._enabled_universes", return_value=["SP500"]):
                    with patch("src.watchlists.list_watchlists", return_value=[]):
                        with patch("src.webapp.routes_v2.load_strategy", return_value=strategy):
                            with patch("src.webapp.routes_v2.bt_store.create_task", side_effect=fake_create_task):
                                with patch("src.webapp.routes_v2.get_runner", return_value=runner):
                                    with patch("src.webapp.routes_v2._strategy_research_snapshot", return_value={}):
                                        with TestClient(create_app()) as client:
                                            page = client.get(
                                                f"/backtests/new?strategy_id={strategy.id}&portfolio_value=18500"
                                            )
                                            response = client.post(
                                                "/api/backtests",
                                                json={
                                                    "strategy_id": strategy.id,
                                                    "universe": "SP500",
                                                    "start": "1Y",
                                                    "end": "today",
                                                    "execution": {
                                                        "timing": "next_open",
                                                        "portfolio_value": 18_500,
                                                    },
                                                },
                                            )

    assert page.status_code == 200
    assert 'id="exec-portfolio-value"' in page.text
    assert 'value="18500.00"' in page.text
    assert response.status_code == 201
    assert captured["execution"]["portfolio_value"] == 18_500.0
    runner.submit.assert_called_once_with("d8b4f723-f326-4771-9875-70024c19e9f4")
