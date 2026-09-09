"""
Broad-market turning-point research.

This package is intentionally separate from ``src.breakouts.load_market_regime``.
The breakout helper is a short-horizon MA filter, while this domain builds an
auditable research dataset, retrospective labels, and candidate features for
top-risk and bottom-reversal models.
"""

SCHEMA_VERSION = "1.0.0"
ALGORITHM_VERSION = "0.2.0"
SCREENING_SCHEMA_VERSION = "1.0.0"
SCREENING_ALGORITHM_VERSION = "0.1.4"

__all__ = [
    "ALGORITHM_VERSION",
    "SCHEMA_VERSION",
    "SCREENING_ALGORITHM_VERSION",
    "SCREENING_SCHEMA_VERSION",
]
