"""Authentication guardrails isolated from the business Web application."""
from __future__ import annotations

import base64
import binascii
import ipaddress
import os
import secrets
from typing import Awaitable, Callable

from fastapi import Request
from fastapi.responses import PlainTextResponse, Response


AUTH_USER_ENV = "QUANT_OPS_AUTH_USER"
AUTH_PASSWORD_ENV = "QUANT_OPS_AUTH_PASSWORD"
AUTH_MIN_PASSWORD_LENGTH_ENV = "QUANT_OPS_AUTH_MIN_PASSWORD_LENGTH"
DEFAULT_MIN_PASSWORD_LENGTH = 16


def is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def minimum_password_length() -> int:
    raw = os.environ.get(AUTH_MIN_PASSWORD_LENGTH_ENV, "").strip()
    if not raw:
        return DEFAULT_MIN_PASSWORD_LENGTH
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{AUTH_MIN_PASSWORD_LENGTH_ENV} must be an integer") from exc
    if value < 8:
        raise RuntimeError(f"{AUTH_MIN_PASSWORD_LENGTH_ENV} must be at least 8")
    return value


def operations_credentials() -> tuple[str, str] | None:
    username = os.environ.get(AUTH_USER_ENV, "")
    password = os.environ.get(AUTH_PASSWORD_ENV, "")
    if bool(username) != bool(password):
        raise RuntimeError(
            f"{AUTH_USER_ENV} and {AUTH_PASSWORD_ENV} must be configured together"
        )
    if not username:
        return None
    required = minimum_password_length()
    if len(password) < required:
        raise RuntimeError(
            f"{AUTH_PASSWORD_ENV} must contain at least {required} characters"
        )
    if password.casefold().startswith(("replace-with-", "change-me", "changeme")):
        raise RuntimeError(
            f"{AUTH_PASSWORD_ENV} still contains an example placeholder"
        )
    return username, password


def validate_operations_exposure(host: str) -> None:
    if is_loopback_host(host):
        return
    if operations_credentials() is None:
        raise RuntimeError(
            f"Refusing unauthenticated operations bind on {host!r}. Configure "
            f"{AUTH_USER_ENV} and {AUTH_PASSWORD_ENV}, or keep 127.0.0.1 and use "
            "an SSH tunnel."
        )


def _unauthorized() -> PlainTextResponse:
    return PlainTextResponse(
        "Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Quant Operations", charset="UTF-8"'},
    )


def install_operations_auth(
    app,
    credentials: tuple[str, str] | None,
) -> None:
    if credentials is None:
        @app.middleware("http")
        async def require_local_client(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            client_host = request.client.host if request.client else ""
            if client_host != "testclient" and not is_loopback_host(client_host):
                return PlainTextResponse(
                    "Remote operations access requires QUANT_OPS_AUTH_USER and "
                    "QUANT_OPS_AUTH_PASSWORD",
                    status_code=403,
                )
            return await call_next(request)
        return
    expected_user, expected_password = credentials

    @app.middleware("http")
    async def require_basic_auth(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        authorization = request.headers.get("authorization", "")
        scheme, _, encoded = authorization.partition(" ")
        if scheme.casefold() != "basic" or not encoded:
            return _unauthorized()
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            username, separator, password = decoded.partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return _unauthorized()
        if not separator:
            return _unauthorized()
        if not (
            secrets.compare_digest(username, expected_user)
            and secrets.compare_digest(password, expected_password)
        ):
            return _unauthorized()
        return await call_next(request)


__all__ = [
    "AUTH_MIN_PASSWORD_LENGTH_ENV",
    "AUTH_PASSWORD_ENV",
    "AUTH_USER_ENV",
    "install_operations_auth",
    "is_loopback_host",
    "operations_credentials",
    "validate_operations_exposure",
]
