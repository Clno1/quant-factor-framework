from __future__ import annotations

from unittest.mock import patch

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
        "execution": {"timing": "next_open"},
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

