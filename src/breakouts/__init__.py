"""Qullamaggie-style momentum breakout scanning and intraday timing."""

from importlib import import_module

__all__ = [
    "BreakoutFilters",
    "INTRADAY_INTERVALS",
    "build_intraday_snapshot",
    "evaluate_daily_setup",
    "load_intraday_1min",
    "load_market_regime",
    "refresh_daily_frame",
    "scan_breakouts",
]


def __getattr__(name):
    # EP's offline reader must not load the price scanner or its data dependencies.
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = "intraday" if name in {
        "INTRADAY_INTERVALS", "build_intraday_snapshot", "load_intraday_1min"
    } else "scanner"
    value = getattr(import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
