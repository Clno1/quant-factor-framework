"""回测引擎。"""
from src.backtest.metrics import (
    performance_summary,
    annualized_return,
    annualized_volatility,
    sharpe_ratio,
    max_drawdown,
)
from src.backtest.quintile import QuintileResult
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
from src.backtest.integrity import (
    install_backtest_integrity_adapter,
    quintile_backtest_integrity,
)
from src.backtest.quintile_v2 import quintile_backtest_v2

# Package-level research entry points and the async runner use the strict
# semantic adapter. Low-level legacy quintile remains importable from
# src.backtest.quintile for compatibility tests during migration.
quintile_backtest = quintile_backtest_integrity
install_backtest_integrity_adapter()

__all__ = [
    "QuintileResult",
    "quintile_backtest",
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
