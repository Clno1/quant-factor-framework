#!/usr/bin/env python3
"""Probe FMP US-stock WebSocket entitlement without logging credentials."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import ssl
import sys
import time
from typing import Any

import websockets
from requests.certs import where as requests_ca_bundle


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.fmp import get_api_key  # noqa: E402


ENDPOINTS = {
    "legacy": "wss://websockets.financialmodelingprep.com",
    "us-stocks": "wss://financialmodelingprep.com/ws/us-stocks",
}
DEFAULT_ENDPOINT = ENDPOINTS["legacy"]
DEFAULT_SYMBOLS = ("aapl", "aeva", "okta", "peng", "vast")
MAX_PROBE_SYMBOLS = 25


def parse_symbols(raw: str) -> tuple[str, ...]:
    symbols = tuple(dict.fromkeys(
        value.strip().lower()
        for value in str(raw or "").split(",")
        if value.strip()
    ))
    if not symbols:
        raise ValueError("at least one symbol is required")
    if len(symbols) > MAX_PROBE_SYMBOLS:
        raise ValueError(
            f"probe accepts at most {MAX_PROBE_SYMBOLS} symbols to avoid entitlement overrun"
        )
    if any(not symbol.replace(".", "").replace("-", "").isalnum() for symbol in symbols):
        raise ValueError("symbols may contain only letters, digits, dots and hyphens")
    return symbols


def parse_message(raw: str | bytes) -> Any:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"_non_json": True}


def control_state(payload: Any) -> str:
    """Classify control responses without returning their raw text."""
    text = json.dumps(payload, ensure_ascii=True, default=str).casefold()
    if any(token in text for token in ("unauthorized", "forbidden", "invalid api", "denied")):
        return "unauthorized"
    if any(token in text for token in ("authenticated", "logged in", "login success")):
        return "authenticated"
    if isinstance(payload, dict):
        status = payload.get("status")
        event = str(payload.get("event") or "").casefold()
        if event == "login" and status in {200, "200", True, "success", "ok"}:
            return "authenticated"
    return "unknown"


def _symbol(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("s") or payload.get("symbol") or payload.get("ticker")
    if value is None and isinstance(payload.get("data"), dict):
        data = payload["data"]
        value = data.get("s") or data.get("symbol") or data.get("ticker")
    return str(value).strip().lower() if value else None


def _message_type(payload: Any) -> str:
    if not isinstance(payload, dict):
        return type(payload).__name__
    value = payload.get("type") or payload.get("event") or payload.get("status")
    return str(value or "dict")


def _safe_error(exc: BaseException) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    error = {
        "type": type(exc).__name__,
        "http_status": getattr(response, "status_code", None),
    }
    if type(exc).__name__ in {"InvalidURI", "InvalidProxy"}:
        error["reason"] = str(exc)
    return error


async def probe(
    symbols: tuple[str, ...],
    *,
    duration_seconds: float,
    endpoint: str = DEFAULT_ENDPOINT,
    login_timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {
        "endpoint": endpoint,
        "symbols": list(symbols),
        "connected": False,
        "login_state": "unknown",
        "messages": 0,
        "symbols_seen": [],
        "types": {},
        "payload_keys": [],
        "elapsed_seconds": 0.0,
    }
    message_types: Counter[str] = Counter()
    symbols_seen: set[str] = set()
    payload_keys: set[str] = set()
    ssl_context = ssl.create_default_context(cafile=requests_ca_bundle())

    try:
        async with websockets.connect(
            endpoint,
            proxy=None,
            ssl=ssl_context,
            open_timeout=10,
            close_timeout=3,
            ping_interval=20,
            ping_timeout=10,
            max_size=1_048_576,
        ) as socket:
            result["connected"] = True
            await socket.send(json.dumps({
                "event": "login",
                "data": {"apiKey": get_api_key()},
            }))

            login_deadline = time.monotonic() + max(1.0, login_timeout_seconds)
            while time.monotonic() < login_deadline:
                timeout = max(0.1, login_deadline - time.monotonic())
                try:
                    payload = parse_message(
                        await asyncio.wait_for(socket.recv(), timeout=timeout)
                    )
                except TimeoutError:
                    break
                state = control_state(payload)
                if state != "unknown":
                    result["login_state"] = state
                    break
                message_types[_message_type(payload)] += 1
                if isinstance(payload, dict):
                    payload_keys.update(str(key) for key in payload)

            if result["login_state"] == "unauthorized":
                return result

            await socket.send(json.dumps({
                "event": "subscribe",
                "data": {"ticker": list(symbols)},
            }))
            data_deadline = time.monotonic() + max(1.0, duration_seconds)
            while time.monotonic() < data_deadline:
                timeout = min(1.0, max(0.1, data_deadline - time.monotonic()))
                try:
                    payload = parse_message(
                        await asyncio.wait_for(socket.recv(), timeout=timeout)
                    )
                except TimeoutError:
                    continue
                state = control_state(payload)
                if state == "unauthorized":
                    result["login_state"] = state
                    break
                if state == "authenticated":
                    result["login_state"] = state
                    continue
                result["messages"] += 1
                message_types[_message_type(payload)] += 1
                symbol = _symbol(payload)
                if symbol:
                    symbols_seen.add(symbol)
                if isinstance(payload, dict):
                    payload_keys.update(str(key) for key in payload)

            await socket.send(json.dumps({
                "event": "unsubscribe",
                "data": {"ticker": list(symbols)},
            }))
    except Exception as exc:  # noqa: BLE001
        result["error"] = _safe_error(exc)
    finally:
        result["symbols_seen"] = sorted(symbols_seen)
        result["types"] = dict(sorted(message_types.items()))
        result["payload_keys"] = sorted(payload_keys)
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help=f"Comma-separated lowercase subscriptions; maximum {MAX_PROBE_SYMBOLS}.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=15.0,
        help="Seconds to collect messages after subscribing (1-60).",
    )
    parser.add_argument(
        "--endpoint",
        choices=tuple(ENDPOINTS),
        default="legacy",
        help="FMP WebSocket endpoint alias.",
    )
    args = parser.parse_args()
    symbols = parse_symbols(args.symbols)
    duration = min(60.0, max(1.0, float(args.duration)))
    result = asyncio.run(probe(
        symbols,
        duration_seconds=duration,
        endpoint=ENDPOINTS[args.endpoint],
    ))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))

    if result.get("login_state") == "unauthorized":
        return 2
    if not result.get("connected"):
        return 3
    if not result.get("messages"):
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
