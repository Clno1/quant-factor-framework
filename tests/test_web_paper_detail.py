from __future__ import annotations

from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from src.webapp.app import create_app


def test_paper_detail_renders_frozen_contract_and_fee_components():
    account_id = "db026d38-0b27-46a5-bdf8-3d26240fe26c"
    account = {
        "id": account_id,
        "name": "Paper audit",
        "strategy_id": "strategy-one",
        "strategy_snapshot": {"name": "Momentum", "components": []},
        "universe": "SP500",
        "status": "active",
        "initial_cash": 100_000.0,
        "cash": 99_000.0,
        "last_equity": 100_500.0,
        "created_at": "2026-08-01T00:00:00",
        "last_decision_date": "2026-08-07",
        "last_mark_date": "2026-08-07",
        "last_error": None,
        "execution": {
            "timing": "next_open",
            "fee_model": "ibkr_us_pro_fixed",
            "slippage_model": "volume_share",
        },
        "research_evidence_snapshot": {
            "captured_at": "2026-08-01T00:00:00+00:00",
            "cross_publication": {
                "status": "PUBLISHED",
                "target_session": "2026-07-31",
                "generation_id": "cross-paper-v1",
            },
            "components": [{
                "factor_id": "MOM_12M",
                "weight": 1.0,
                "sp500_verdict": "PASS",
                "nasdaq100_verdict": "PASS",
                "verdict": "ROBUST",
                "research_target_session": "2026-07-31",
            }],
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
        "data_contract": {
            "requested_universe": "SP500",
            "data_universe": "SP500",
            "target_session": "2026-08-07",
            "dataset_version_id": "dataset-paper-v1",
            "dataset_run_id": "run-paper-v1",
            "bars_sha256": "sha256:bars",
            "universe_sha256": "sha256:universe",
            "membership_sha256": "sha256:membership",
            "manifest_sha256": "sha256:manifest",
            "factor_publication_id": "factor-paper-v1",
            "factor_generations": {"MOM_12M": "generation-paper-v1"},
        },
    }
    artifacts = {
        "positions": pd.DataFrame(),
        "orders": pd.DataFrame(),
        "target_weights": pd.DataFrame(),
        "equity_curve": pd.DataFrame(),
        "fills": pd.DataFrame([{
            "fill_date": "2026-08-07",
            "ticker": "AAPL",
            "side": "BUY",
            "quantity": 10,
            "fill_price": 100.05,
            "notional": 1_000.5,
            "raw_open_price": 100.0,
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
        }]),
        "cash_events": pd.DataFrame([{
            "date": "2026-08-07",
            "ticker": "AAPL",
            "event_type": "DIVIDEND_CASH",
            "quantity": 10,
            "amount_per_share": 0.25,
            "amount": 2.5,
            "dataset_version_id": "dataset-paper-v1",
        }]),
        "runs": pd.DataFrame([{
            "run_at": "2026-08-08T08:00:00",
            "decision_date": "2026-08-07",
            "expected_session": "2026-08-07",
            "mark_date": "2026-08-07",
            "is_rebalance": True,
            "fills_count": 1,
            "orders_created": 1,
            "pending_orders": 0,
            "equity": 100_500.0,
            "dataset_version_id": "dataset-paper-v1",
            "factor_publication_id": "factor-paper-v1",
        }]),
    }

    with patch("src.webapp.app._recover_application_state", return_value=(0, 0)):
        with patch(
            "src.webapp.routes_v2.load_paper_account",
            return_value=account,
        ):
            with patch(
                "src.webapp.routes_v2.load_account_artifacts",
                return_value=artifacts,
            ):
                with TestClient(create_app()) as client:
                    response = client.get(f"/paper/{account_id}")

    assert response.status_code == 200
    assert "cross-paper-v1" in response.text
    assert "dataset-paper-v1" in response.text
    assert "FINRA CAT" in response.text
    assert "0.1234" in response.text
    assert "分红现金事件" in response.text
    assert "DIVIDEND_CASH" in response.text
    assert "2.5000" in response.text
    assert "运行与版本绑定记录" in response.text
