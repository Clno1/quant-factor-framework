from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from src.operations.models import DeliveryObservation, JobSnapshot, JobStatus
from src.operations.registry import OperationsRegistry
from src.operations.store import OperationsReader, OperationsStore
from src.operations_web.app import create_app
from src.operations_web.security import (
    operations_credentials,
    validate_operations_exposure,
)


def _client(tmp_path: Path) -> TestClient:
    registry = OperationsRegistry("configs/operations.yaml")
    store = OperationsStore(tmp_path / "ops.sqlite3", tmp_path / "snapshot.sqlite3")
    store.initialize()
    store.sync_job_definitions(
        registry.list(),
        observed_at="2026-08-12T16:00:00+00:00",
    )
    store.upsert_snapshots([JobSnapshot(
        job_id="premarket_digest",
        status=JobStatus.SUCCESS,
        observed_at="2026-08-12T16:00:00+00:00",
        target_session="2026-08-12",
        status_reason="两个业务频道均已发送",
    )])
    store.upsert_deliveries([DeliveryObservation(
        delivery_id="expected_intraday",
        job_id="intraday_momentum",
        channel="盘中动量突破",
        status="NO_SIGNAL",
        observed_at="2026-08-12T16:00:00+00:00",
        target_session="2026-08-12",
        error_summary="当前尚无需要投递的盘中突破信号",
    )])
    store.publish_snapshot()
    app = create_app(
        registry=registry,
        reader=OperationsReader(tmp_path / "snapshot.sqlite3"),
        credentials=None,
    )
    return TestClient(app)


def test_operations_pages_and_apis_are_read_only(tmp_path: Path):
    client = _client(tmp_path)
    for path in (
        "/",
        "/jobs",
        "/jobs/premarket_digest",
        "/freshness",
        "/deliveries",
        "/projects",
        "/incidents",
        "/api/overview",
        "/healthz",
    ):
        assert client.get(path).status_code == 200
    assert client.get("/favicon.ico").status_code == 204
    assert client.post("/api/jobs/premarket_digest/run").status_code == 404
    assert "两个业务频道均已发送" in client.get("/").text
    deliveries = client.get("/deliveries").text
    assert "盘中动量突破" in deliveries
    assert "无信号" in deliveries


def test_operations_credentials_are_independent(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("QUANT_OPS_AUTH_USER", "ops")
    monkeypatch.setenv("QUANT_OPS_AUTH_PASSWORD", "operations-password")
    monkeypatch.setenv("QUANT_WEB_AUTH_USER", "business")
    monkeypatch.setenv("QUANT_WEB_AUTH_PASSWORD", "business-password")
    registry = OperationsRegistry("configs/operations.yaml")
    app = create_app(
        registry=registry,
        reader=OperationsReader(tmp_path / "missing.sqlite3"),
    )
    client = TestClient(app)
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("business", "business-password")).status_code == 401
    assert client.get("/", auth=("ops", "operations-password")).status_code == 200


def test_operations_example_password_is_rejected(monkeypatch):
    monkeypatch.setenv("QUANT_OPS_AUTH_USER", "ops")
    monkeypatch.setenv(
        "QUANT_OPS_AUTH_PASSWORD",
        "replace-with-at-least-16-random-characters",
    )
    try:
        operations_credentials()
    except RuntimeError as exc:
        assert "placeholder" in str(exc)
    else:
        raise AssertionError("example operations password must fail closed")


def test_operations_public_bind_requires_independent_credentials(monkeypatch):
    monkeypatch.delenv("QUANT_OPS_AUTH_USER", raising=False)
    monkeypatch.delenv("QUANT_OPS_AUTH_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="Refusing unauthenticated"):
        validate_operations_exposure("0.0.0.0")


def test_operations_public_bind_accepts_valid_independent_credentials(monkeypatch):
    monkeypatch.setenv("QUANT_OPS_AUTH_USER", "ops")
    monkeypatch.setenv("QUANT_OPS_AUTH_PASSWORD", "operations-password")

    validate_operations_exposure("0.0.0.0")


def test_business_app_does_not_mount_operations_routes():
    from src.webapp.app import create_app as create_business_app

    paths = {route.path for route in create_business_app().routes}
    assert "/incidents" not in paths
    assert "/api/overview" not in paths
    assert "/projects" not in paths
