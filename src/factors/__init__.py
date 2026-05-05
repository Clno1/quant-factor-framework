"""
因子库包入口。

提供因子注册表 FACTOR_REGISTRY，新增因子 = 在对应模块用 @register 装饰器注册。
"""
from __future__ import annotations

from src.factors.base import FactorBase, FACTOR_REGISTRY, register

# 导入具体因子以触发注册
from src.factors.momentum import Momentum6M  # noqa: F401

__all__ = ["FactorBase", "FACTOR_REGISTRY", "register", "get_factor"]


def get_factor(name: str) -> FactorBase:
    """按名字获取因子实例（大小写敏感，与 name 字段一致）。"""
    if name not in FACTOR_REGISTRY:
        available = ", ".join(sorted(FACTOR_REGISTRY.keys()))
        raise KeyError(f"Unknown factor: {name}. Available: [{available}]")
    cls = FACTOR_REGISTRY[name]
    return cls()
