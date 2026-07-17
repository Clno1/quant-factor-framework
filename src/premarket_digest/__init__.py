"""Isolated premarket-report orchestration for Discord delivery.

This package is intentionally a leaf integration layer: it may read the
momentum and group-analytics domains, while neither domain imports it.
"""

from .models import DigestChannel, PremarketContext, SourceGateError
from .service import PremarketDigestService
from .settings import PremarketDigestSettings, load_premarket_digest_settings

__all__ = [
    "DigestChannel",
    "PremarketContext",
    "PremarketDigestService",
    "PremarketDigestSettings",
    "SourceGateError",
    "load_premarket_digest_settings",
]
