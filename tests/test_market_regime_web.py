from __future__ import annotations

import asyncio

import httpx
import pandas as pd
import pytest

from src.market_regime_research.artifacts import file_sha256
from src.webapp import research_routes
from src.webapp.app import create_app
from src.webapp.market_regime_status import (
    MarketRegimeViewError,
    _episode_rows,
    _safe_child,
    _validated_ohlc,
    _validated_screening_predictions,
    _verify_hashes,
)
from src.webapp.security import AUTH_PASSWORD_ENV, AUTH_USER_ENV


def _status_payload() -> dict:
    return {
        "status": "RESEARCH_ONLY",
        "research_status": "STAGE_1_CANDIDATE",
        "message": "已有阶段性底部候选，但当前没有获准生产的实时信号。",
        "expected_session": "2026-08-28",
        "observed_session": "2026-08-19",
        "data_delay_sessions": 7,
        "data_status": "STALE",
        "source_integrity": "PASS",
        "stage_a": {
            "status": "PASS",
            "run_id": "stage-a-test",
        },
        "stage_b": {
            "status": "PASS",
            "screening_id": "stage-b-test",
            "source_research_run_id": "stage-a-test",
            "source_date_end": "2026-07-30",
            "prediction_start": "2002-01-02",
            "prediction_end": "2020-06-26",
            "holdout_status": "SEALED_NOT_EVALUATED",
            "holdout_start": "2022-01-01",
            "production_approved_count": 0,
            "registry_version": "1.0.0",
        },
        "candidate": {
            "candidate_id": "bottom_spx_return_5d__5d",
            "screening_status": "STAGE_1_PASS",
            "signal_episodes": 25,
            "event_precision": 0.6,
            "event_recall": 0.283019,
            "roc_auc": 0.638328,
            "average_precision": 0.289173,
            "prevalence": 0.201859,
            "brier_skill": 0.029261,
            "false_alarm_episodes_per_year": 0.541,
        },
        "bottom_counts": {
            "stage_1_pass": 1,
            "stage_1_fail": 21,
            "insufficient": 16,
            "total": 38,
        },
        "market": {
            "spx": {
                "date": "2026-08-19",
                "close": 6500.0,
                "return_5d": 0.01,
                "return_20d": -0.02,
                "drawdown_252d": -0.03,
            },
            "ndx": {
                "date": "2026-08-19",
                "close": 25000.0,
                "return_5d": -0.01,
                "return_20d": 0.03,
                "drawdown_252d": -0.04,
            },
        },
        "risk": {
            "vix": {"date": "2026-08-19", "value": 14.89},
            "cor1m": {"date": "2026-08-19", "value": 7.95},
        },
        "pit": {
            "status": "FAIL",
            "asof": "2026-08-19",
            "inconsistency_count": 25,
            "note": "PIT 股票池未通过发布门禁。",
        },
        "current_signal": {
            "status": "NOT_RUNNING",
            "probability": None,
            "note": "每日影子评分尚未完成，禁止推算今日底部概率。",
        },
        "pipeline": [
            {"name": "核心特征与标签", "status": "COMPLETE", "note": "已完成。"},
            {"name": "G1-G6 有效性筛选", "status": "COMPLETE", "note": "已完成。"},
            {"name": "COR1M 与 PIT 市场宽度 v2", "status": "BLOCKED", "note": "PIT 阻断。"},
            {"name": "G7 参数扰动", "status": "PENDING", "note": "待执行。"},
            {"name": "G10 影子运行", "status": "NOT_STARTED", "note": "未开始。"},
        ],
        "periods": [
            {"id": "recent", "label": "近期 1 年"},
            {"id": "wf_2020_2021", "label": "2020-2021"},
        ],
        "instruments": [
            {"id": "spx", "label": "标普 500"},
            {"id": "ndx", "label": "纳斯达克 100"},
        ],
        "errors": [],
    }


def _chart_payload(instrument: str = "spx", period: str = "wf_2020_2021") -> dict:
    return {
        "instrument": instrument,
        "instrument_label": "标普 500" if instrument == "spx" else "纳斯达克 100",
        "period": period,
        "period_label": "2020-2021",
        "date_start": "2020-03-16",
        "date_end": "2020-03-17",
        "candles": [
            {"date": "2020-03-16", "open": 2500.0, "high": 2600.0, "low": 2400.0, "close": 2450.0},
            {"date": "2020-03-17", "open": 2450.0, "high": 2550.0, "low": 2420.0, "close": 2520.0},
        ],
        "signal_episodes": [{
            "episode": 1,
            "start_date": "2020-03-16",
            "end_date": "2020-03-16",
            "date": "2020-03-16",
            "feature_value": -0.12,
            "model_probability": 0.7,
            "baseline_probability": 0.2,
            "outcome": True,
            "touch_day": 2,
            "rows": 1,
        }],
        "outcome_episodes": [],
        "marker_contract": {
            "signal": "当日可知的样本外 Stage 1 候选报警",
            "outcome": "事后评估标签，不是实时信号",
            "holdout_included": False,
        },
    }


def test_episode_rows_cluster_by_trading_sessions() -> None:
    sessions = pd.bdate_range("2020-01-02", periods=12)
    predictions = pd.DataFrame({
        "date": [sessions[0], sessions[2], sessions[8]],
        "signal": [True, True, True],
        "actual": [0, 1, 0],
        "feature_value": [-0.05, -0.11, -0.06],
        "model_probability": [0.55, 0.75, 0.60],
        "baseline_probability": [0.20, 0.20, 0.20],
        "touch_day": [None, 3, None],
    })

    episodes = _episode_rows(
        predictions,
        sessions=sessions,
        value_column="signal",
        horizon=5,
    )

    assert len(episodes) == 2
    assert episodes[0]["date"] == sessions[2].date().isoformat()
    assert episodes[0]["rows"] == 2
    assert episodes[0]["outcome"] is True
    assert episodes[0]["touch_day"] == 3
    assert episodes[1]["outcome"] is False


def test_view_adapter_rejects_tampering_and_invalid_ohlc(tmp_path) -> None:
    artifact = tmp_path / "features.parquet"
    artifact.write_bytes(b"verified")
    hashes = {artifact.name: file_sha256(artifact)}
    _verify_hashes(tmp_path, hashes, (artifact.name,))

    artifact.write_bytes(b"tampered")
    with pytest.raises(MarketRegimeViewError, match="哈希不一致"):
        _verify_hashes(tmp_path, hashes, (artifact.name,))
    with pytest.raises(MarketRegimeViewError, match="越过允许目录"):
        _safe_child(tmp_path, "../escape.parquet")

    invalid = pd.DataFrame({
        "open": [100.0],
        "high": [99.0],
        "low": [98.0],
        "close": [101.0],
    })
    with pytest.raises(MarketRegimeViewError, match="high/low"):
        _validated_ohlc(invalid, label="测试行情")

    predictions = pd.DataFrame({
        "candidate_id": ["bottom_test"],
        "date": ["2022-01-03"],
    })
    with pytest.raises(MarketRegimeViewError, match="越过封存集"):
        _validated_screening_predictions(
            predictions,
            holdout_start="2022-01-01",
        )


def test_market_regime_page_and_api_are_explicitly_research_only(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("QUANT_APP_DB_PATH", str(tmp_path / "app.sqlite3"))
    monkeypatch.delenv(AUTH_USER_ENV, raising=False)
    monkeypatch.delenv(AUTH_PASSWORD_ENV, raising=False)
    monkeypatch.setattr(
        research_routes,
        "market_regime_status_payload",
        _status_payload,
    )

    def fake_chart(*, instrument: str, period: str) -> dict:
        if instrument not in {"spx", "ndx"}:
            raise ValueError("instrument must be spx or ndx")
        if period == "corrupt":
            raise MarketRegimeViewError("研究产物哈希不一致")
        if period not in {"recent", "wf_2020_2021"}:
            raise ValueError("unknown chart period")
        return _chart_payload(instrument, period)

    monkeypatch.setattr(
        research_routes,
        "market_regime_chart_payload",
        fake_chart,
    )
    app = create_app()

    async def exercise() -> None:
        transport = httpx.ASGITransport(
            app=app,
            client=("127.0.0.1", 12345),
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://quant.test",
        ) as client:
            page = await client.get("/research/market-regime")
            assert page.status_code == 200
            assert "市场研究状态" in page.text
            assert "研究观察 · 未获生产批准" in page.text
            assert "还不能判断“今日见底”" in page.text
            assert "今日底部概率" in page.text
            assert "未运行" in page.text
            assert "1 / 38" in page.text
            assert "market_regime_status.js" in page.text
            assert 'class="research-status-strip"' not in page.text

            status = await client.get("/api/research/market-regime/status")
            assert status.status_code == 200
            assert status.json()["status"] == "RESEARCH_ONLY"
            assert status.json()["stage_b"]["production_approved_count"] == 0

            chart = await client.get(
                "/api/research/market-regime/chart?instrument=spx&period=wf_2020_2021"
            )
            assert chart.status_code == 200
            assert chart.json()["marker_contract"]["holdout_included"] is False

            invalid = await client.get(
                "/api/research/market-regime/chart?instrument=invalid"
            )
            assert invalid.status_code == 400
            corrupt = await client.get(
                "/api/research/market-regime/chart?period=corrupt"
            )
            assert corrupt.status_code == 409

    asyncio.run(exercise())
