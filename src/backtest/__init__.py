"""回测引擎。"""
from src.backtest.metrics import (
    performance_summary,
    annualized_return,
    annualized_volatility,
    sharpe_ratio,
    max_drawdown,
)
from src.backtest.quintile import QuintileResult, quintile_backtest
from src.backtest.composer import (
    CompositionResult,
    FactorDataMissingError,
    compose_factor,
)
from src.backtest.adhoc import (
    AdhocResult,
    adhoc_compose,
)

__all__ = [
    "QuintileResult",
    "quintile_backtest",
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
