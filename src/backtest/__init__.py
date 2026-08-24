"""回测引擎。"""
from src.backtest.metrics import (
    performance_summary,
    annualized_return,
    annualized_volatility,
    sharpe_ratio,
    max_drawdown,
)
from src.backtest.quintile import QuintileResult, quintile_backtest as legacy_quintile_backtest
from src.backtest.double_sort import DoubleSortResult, double_sort_backtest
from src.backtest.composer import (
    CompositionResult,
    FactorDataMissingError,
    compose_factor,
)
from src.backtest.adhoc import (
    AdhocResult,
    adhoc_compose,
)
from src.backtest.quintile_v2 import quintile_backtest_v2

# Legacy/synthetic callers must opt into the explicit legacy name. The ambiguous
# package-level ``quintile_backtest`` entry point is intentionally gone.

__all__ = [
    "QuintileResult",
    "legacy_quintile_backtest",
    "quintile_backtest_v2",
    "DoubleSortResult",
    "double_sort_backtest",
    "performance_summary",
    "annualized_return",
    "annualized_volatility",
    "sharpe_ratio",
    "max_drawdown",
    "CompositionResult",
    "FactorDataMissingError",
    "compose_factor",
    "AdhocResult",
    "adhoc_compose",
]
