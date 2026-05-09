"""
策略库：因子加权配方的纯定义模块（不含回测结果、不绑定股票池）。

- `StrategyDefinition` / `StrategyComponent`：数据类
- `StrategyStore`：CRUD（create / list / load / delete），_index.json 维护
"""
from src.strategies.definition import (  # noqa: F401
    StrategyComponent,
    StrategyDefinition,
    StrategyValidationError,
)
from src.strategies.store import (  # noqa: F401
    STRATEGY_ROOT,
    create_strategy,
    delete_strategy,
    list_strategies,
    load_strategy,
)

__all__ = [
    "StrategyComponent",
    "StrategyDefinition",
    "StrategyValidationError",
    "STRATEGY_ROOT",
    "create_strategy",
    "delete_strategy",
    "list_strategies",
    "load_strategy",
]
