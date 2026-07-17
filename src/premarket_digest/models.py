"""Small, serializable contracts for the premarket digest integration."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class DigestChannel(StrEnum):
    MOMENTUM = "momentum"
    SECTOR_ROTATION = "sector-rotation"

    @property
    def destination(self) -> str:
        return "momentum-alerts" if self is self.MOMENTUM else "sector-rotation"


class DeliveryState(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class PremarketContext:
    target_session: str
    source_session: str
    now_utc: datetime
    now_et: datetime

    @property
    def generated_at(self) -> str:
        return self.now_utc.isoformat(timespec="seconds").replace("+00:00", "Z")


class ScheduleSkip(RuntimeError):
    """A normal no-send schedule outcome (holiday, weekend, or late wakeup)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SourceGateError(RuntimeError):
    """A source was readable but did not meet the frozen publication gate."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.details = details or {}


__all__ = [
    "DeliveryState",
    "DigestChannel",
    "PremarketContext",
    "ScheduleSkip",
    "SourceGateError",
]
