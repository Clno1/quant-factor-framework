"""
因子库包入口。

提供因子注册表 FACTOR_REGISTRY，新增因子 = 在对应模块用 @register 装饰器注册。
"""
from __future__ import annotations

from src.factors.base import FactorBase, FACTOR_REGISTRY, register

# 导入所有因子以触发 @register 注册
from src.factors.momentum import (  # noqa: F401
    Momentum1M, Momentum3M, Momentum6M, Momentum12M,
)
from src.factors.volatility import Volatility20D, Volatility60D  # noqa: F401
from src.factors.reversal import Reversal5D  # noqa: F401
from src.factors.turnover import Turnover20D  # noqa: F401

__all__ = [
    "FactorBase", "FACTOR_REGISTRY", "register", "get_factor",
    "get_factor_catalog", "list_factor_ids", "get_factor_entry",
    "assert_valid_factor_ids", "FactorEntry", "FactorLibraryError",
]


def get_factor(name: str) -> FactorBase:
    """按名字获取因子实例（大小写敏感，与 name 字段一致）。"""
    if name not in FACTOR_REGISTRY:
        available = ", ".join(sorted(FACTOR_REGISTRY.keys()))
        raise KeyError(f"Unknown factor: {name}. Available: [{available}]")
    cls = FACTOR_REGISTRY[name]
    return cls()


# 因子库统一视图（YAML + 代码合并）
from src.factors.library import (  # noqa: E402, F401
    FactorEntry,
    FactorLibraryError,
    assert_valid_factor_ids,
    get_factor_catalog,
    get_factor_entry,
    list_factor_ids,
)
