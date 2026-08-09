from __future__ import annotations

from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from src.webapp.app import create_app


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
