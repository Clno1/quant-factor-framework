from __future__ import annotations

import asyncio
import base64
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from src.utils.identifiers import (
    InvalidResourceId,
    canonical_ticker,
    canonical_uuid,
    safe_path_component,
)
from src.webapp.security import (
    AUTH_MIN_PASSWORD_LENGTH_ENV,
    AUTH_PASSWORD_ENV,
    AUTH_USER_ENV,
    install_basic_auth_middleware,
    validate_web_exposure,
)


def test_filesystem_resource_ids_require_canonical_uuid():
    value = str(uuid4())
    assert canonical_uuid(value) == value
    for invalid in ("..", "../other", value.upper(), f" {value} "):
        with pytest.raises(InvalidResourceId):
            canonical_uuid(invalid)


def test_filesystem_components_and_tickers_reject_traversal():
    assert safe_path_component("SP500", label="universe") == "SP500"
    assert canonical_ticker(" brk.b ") == "BRK.B"
    for invalid in ("..", "../SP500", "SP500/other", " SP500"):
        with pytest.raises(InvalidResourceId):
            safe_path_component(invalid, label="universe")
    for invalid in ("../AAPL", "AAPL/../../x", ""):
        with pytest.raises(InvalidResourceId):
            canonical_ticker(invalid)


def test_public_bind_requires_authentication(monkeypatch):
    monkeypatch.delenv(AUTH_USER_ENV, raising=False)
    monkeypatch.delenv(AUTH_PASSWORD_ENV, raising=False)
    validate_web_exposure("127.0.0.1")
    with pytest.raises(RuntimeError, match="Refusing unauthenticated"):
        validate_web_exposure("0.0.0.0")


def test_public_bind_accepts_strong_complete_credentials(monkeypatch):
    monkeypatch.setenv(AUTH_USER_ENV, "quant")
    monkeypatch.setenv(AUTH_PASSWORD_ENV, "long-enough-password")
    validate_web_exposure("0.0.0.0")


def test_partial_or_weak_credentials_are_rejected(monkeypatch):
    monkeypatch.delenv(AUTH_MIN_PASSWORD_LENGTH_ENV, raising=False)
    monkeypatch.setenv(AUTH_USER_ENV, "quant")
    monkeypatch.delenv(AUTH_PASSWORD_ENV, raising=False)
    with pytest.raises(RuntimeError, match="configured together"):
        validate_web_exposure("0.0.0.0")

    monkeypatch.setenv(AUTH_PASSWORD_ENV, "short")
    with pytest.raises(RuntimeError, match="at least 16"):
        validate_web_exposure("0.0.0.0")


def test_operator_can_explicitly_lower_password_floor_to_eight(monkeypatch):
    monkeypatch.setenv(AUTH_USER_ENV, "quant")
    monkeypatch.setenv(AUTH_PASSWORD_ENV, "hangdong")
    monkeypatch.setenv(AUTH_MIN_PASSWORD_LENGTH_ENV, "8")
    validate_web_exposure("0.0.0.0")


def test_password_floor_cannot_be_lower_than_eight(monkeypatch):
    monkeypatch.setenv(AUTH_USER_ENV, "quant")
    monkeypatch.setenv(AUTH_PASSWORD_ENV, "hangdong")
    monkeypatch.setenv(AUTH_MIN_PASSWORD_LENGTH_ENV, "7")
    with pytest.raises(RuntimeError, match="must be at least 8"):
        validate_web_exposure("0.0.0.0")


def test_unauthenticated_app_rejects_non_loopback_clients():
    app = FastAPI()
    install_basic_auth_middleware(app, None)

    @app.get("/")
    def home():
        return {"ok": True}

    async def request() -> int:
        transport = httpx.ASGITransport(
            app=app,
            client=("203.0.113.10", 12345),
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://quant.test",
        ) as client:
            return (await client.get("/")).status_code

    assert asyncio.run(request()) == 403


def test_basic_auth_protects_all_requests():
    app = FastAPI()
    install_basic_auth_middleware(
        app,
        ("quant", "long-enough-password"),
    )

    @app.get("/")
    def home():
        return {"ok": True}

    async def request(headers: dict[str, str] | None = None) -> int:
        transport = httpx.ASGITransport(
            app=app,
            client=("203.0.113.10", 12345),
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://quant.test",
        ) as client:
            return (await client.get("/", headers=headers)).status_code

    assert asyncio.run(request()) == 401
    token = base64.b64encode(
        b"quant:long-enough-password"
    ).decode("ascii")
    assert asyncio.run(request({"Authorization": f"Basic {token}"})) == 200


def test_authenticated_cross_origin_mutation_is_rejected():
    app = FastAPI()
    install_basic_auth_middleware(
        app,
        ("quant", "long-enough-password"),
    )

    @app.post("/run")
    def run():
        return {"ok": True}

    token = base64.b64encode(
        b"quant:long-enough-password"
    ).decode("ascii")
    authorization = f"Basic {token}"

    async def request(origin: str) -> int:
        transport = httpx.ASGITransport(
            app=app,
            client=("203.0.113.10", 12345),
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://quant.test",
        ) as client:
            response = await client.post(
                "/run",
                headers={
                    "Authorization": authorization,
                    "Origin": origin,
                },
            )
            return response.status_code

    assert asyncio.run(request("https://evil.example")) == 403
    assert asyncio.run(request("https://quant.test")) == 200
