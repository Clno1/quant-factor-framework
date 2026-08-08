"""Validation helpers for filesystem-backed resource identifiers."""
from __future__ import annotations

import re
from typing import Any
from uuid import UUID


_PATH_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_TICKER = re.compile(r"[A-Z0-9^][A-Z0-9.^-]{0,31}")


class InvalidResourceId(ValueError):
    """Raised when an external identifier is unsafe or non-canonical."""


def canonical_uuid(value: Any, *, label: str = "resource_id") -> str:
    """
    Return a canonical UUID string and reject every alternate path-like form.

    Backtest and paper-account IDs become directory names. Requiring the exact
    canonical UUID representation prevents traversal before a Path is built.
    """
    raw = str(value or "")
    try:
        parsed = UUID(raw)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidResourceId(f"{label} must be a canonical UUID") from exc
    canonical = str(parsed)
    if raw != canonical:
        raise InvalidResourceId(f"{label} must be a canonical UUID")
    return canonical


def safe_path_component(value: Any, *, label: str = "name") -> str:
    """Return one ASCII filesystem component, rejecting traversal forms."""
    raw = str(value or "")
    if (
        raw in {"", ".", ".."}
        or raw != raw.strip()
        or _PATH_COMPONENT.fullmatch(raw) is None
    ):
        raise InvalidResourceId(
            f"{label} must be a safe ASCII identifier"
        )
    return raw


def canonical_ticker(value: Any, *, label: str = "ticker") -> str:
    """Normalize a market symbol while forbidding path separators."""
    normalized = str(value or "").strip().upper()
    if _TICKER.fullmatch(normalized) is None:
        raise InvalidResourceId(
            f"{label} contains unsupported characters"
        )
    return normalized


__all__ = [
    "InvalidResourceId",
    "canonical_ticker",
    "canonical_uuid",
    "safe_path_component",
]
