"""Isolated group analytics domain.

The package intentionally has no eager imports.  Existing factor, backtest and
paper-trading domains must not depend on this package; application entrypoints
compose it only when the feature flag is enabled.
"""

SCHEMA_VERSION = "1.1.0"
ALGORITHM_VERSION = "group-analytics-1.1.0"

__all__ = ["SCHEMA_VERSION", "ALGORITHM_VERSION"]
