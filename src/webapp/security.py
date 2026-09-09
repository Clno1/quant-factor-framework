"""Authentication and network-exposure guardrails for the Web application."""
from __future__ import annotations

import base64
import binascii
import ipaddress
import os
import secrets
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import PlainTextResponse, Response


AUTH_USER_ENV = "QUANT_WEB_AUTH_USER"
AUTH_PASSWORD_ENV = "QUANT_WEB_AUTH_PASSWORD"
AUTH_MIN_PASSWORD_LENGTH_ENV = "QUANT_WEB_AUTH_MIN_PASSWORD_LENGTH"
DEFAULT_MIN_PASSWORD_LENGTH = 16


def minimum_password_length() -> int:
    """Return the operator-configured password floor, preserving a safe default."""
    raw_value = os.environ.get(AUTH_MIN_PASSWORD_LENGTH_ENV, "").strip()
    if not raw_value:
        return DEFAULT_MIN_PASSWORD_LENGTH
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            f"{AUTH_MIN_PASSWORD_LENGTH_ENV} must be an integer"
        ) from exc
    if value < 8:
        raise RuntimeError(f"{AUTH_MIN_PASSWORD_LENGTH_ENV} must be at least 8")
    return value


def basic_auth_credentials() -> tuple[str, str] | None:
    """Load a complete Basic Auth pair or fail on partial configuration."""
    username = os.environ.get(AUTH_USER_ENV, "")
    password = os.environ.get(AUTH_PASSWORD_ENV, "")
    if bool(username) != bool(password):
        raise RuntimeError(
            f"{AUTH_USER_ENV} and {AUTH_PASSWORD_ENV} must be configured together"
    )
    if not username:
        return None
    required_length = minimum_password_length()
    if len(password) < required_length:
        raise RuntimeError(
            f"{AUTH_PASSWORD_ENV} must contain at least {required_length} characters"
        )
    return username, password


def is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_web_exposure(host: str) -> None:
    """Refuse an unauthenticated bind reachable beyond the local machine."""
    if is_loopback_host(host):
        return
    if basic_auth_credentials() is None:
        raise RuntimeError(
            f"Refusing unauthenticated Web bind on {host!r}. Configure "
            f"{AUTH_USER_ENV} and {AUTH_PASSWORD_ENV}, or bind to 127.0.0.1 "
            "and access the service through an SSH tunnel."
        )


def _unauthorized() -> PlainTextResponse:
    return PlainTextResponse(
        "Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Quant", charset="UTF-8"'},
    )


def _cross_origin_mutation(request: Request) -> PlainTextResponse | None:
    """Reject browser cross-origin writes while preserving CLI/API clients."""
    if request.method.upper() in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return None
    fetch_site = request.headers.get("sec-fetch-site", "").casefold()
    if fetch_site not in {"", "same-origin", "none"}:
        return PlainTextResponse(
            "Cross-origin state changes are forbidden",
            status_code=403,
        )
    origin = request.headers.get("origin")
    if not origin:
        return None
    origin_host = urlsplit(origin).netloc.casefold()
    request_host = request.headers.get("host", "").casefold()
    if not origin_host or not request_host or origin_host != request_host:
        return PlainTextResponse(
            "Cross-origin state changes are forbidden",
            status_code=403,
        )
    return None


def install_basic_auth_middleware(app, credentials: tuple[str, str] | None) -> None:
    """Protect every page, API, static asset, and generated API document."""
    if credentials is None:
        @app.middleware("http")
        async def require_local_client(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            forbidden = _cross_origin_mutation(request)
            if forbidden is not None:
                return forbidden
            client_host = request.client.host if request.client else ""
            # "testclient" is Starlette's in-process test transport, not a
            # network address a remote peer can obtain.
            if client_host != "testclient" and not is_loopback_host(client_host):
                return PlainTextResponse(
                    "Remote access requires QUANT_WEB_AUTH_USER and "
                    "QUANT_WEB_AUTH_PASSWORD",
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
        forbidden = _cross_origin_mutation(request)
        if forbidden is not None:
            return forbidden
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
        valid_user = secrets.compare_digest(username, expected_user)
        valid_password = secrets.compare_digest(password, expected_password)
        if not (valid_user and valid_password):
            return _unauthorized()
        return await call_next(request)


__all__ = [
    "AUTH_MIN_PASSWORD_LENGTH_ENV",
    "AUTH_PASSWORD_ENV",
    "AUTH_USER_ENV",
    "basic_auth_credentials",
    "install_basic_auth_middleware",
    "is_loopback_host",
    "minimum_password_length",
    "validate_web_exposure",
]
