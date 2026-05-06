"""
因子基类 + 注册器。

所有因子继承 FactorBase。统一入口：
    compute_from_wide(wide: dict[str, pd.DataFrame]) -> pd.DataFrame

其中 wide 包含：
    - "adj_close" : 复权收盘价宽表 (date x ticker)
    - "close"     : 原始收盘价
    - "volume"    : 成交量
    - "returns"   : 日收益率（基于 adj_close）
    - "sector"    : ticker -> sector

兼容性：旧 API `compute(price_df)` 仍然可用（默认 fallback 用 adj_close）。
新因子可以覆写 `compute_from_wide`，按需取多个输入字段。
"""
from __future__ import annotations

from abc import ABC
from typing import Any, Type

import pandas as pd

# 全局注册表：{factor_name: factor_cls}
FACTOR_REGISTRY: dict[str, Type["FactorBase"]] = {}


def register(cls: Type["FactorBase"]) -> Type["FactorBase"]:
    """因子类装饰器：注册到全局表。"""
    if not issubclass(cls, FactorBase):
        raise TypeError(f"{cls.__name__} must subclass FactorBase")
    if not getattr(cls, "name", None):
        raise ValueError(f"{cls.__name__} missing class attribute `name`")
    FACTOR_REGISTRY[cls.name] = cls
    return cls


class FactorBase(ABC):
    """因子抽象基类。"""

    name: str = ""            # 因子唯一标识（例如 'MOM_6M'）
    description: str = ""     # 人类可读描述
    direction: int = 0        # +1 正向 / -1 负向 / 0 由 IC 自动判断
    inputs: tuple[str, ...] = ("adj_close",)   # 需要的宽表字段（用于校验）

    # ------------------------------------------------------------
    # 新接口：所有因子推荐覆写此方法
    # ------------------------------------------------------------
    def compute_from_wide(self, wide: dict[str, pd.DataFrame]) -> pd.DataFrame:
        """
        默认实现：取 adj_close 调用 compute()，向后兼容旧因子。
        子类可以选择覆写 compute_from_wide 以使用 returns / volume 等。
        """
        primary = self.inputs[0] if self.inputs else "adj_close"
        df = wide.get(primary)
        if df is None:
            raise KeyError(
                f"Factor {self.name} requires wide['{primary}'] but it's missing."
            )
        return self.compute(df)

    # ------------------------------------------------------------
    # 旧接口：保留以兼容 Momentum 等
    # ------------------------------------------------------------
    def compute(self, price_df: pd.DataFrame) -> pd.DataFrame:  # noqa: D401
        """
        老 API（仅接收单一 DataFrame）。新因子建议改用 compute_from_wide。
        """
        raise NotImplementedError(
            f"{type(self).__name__}: must implement either compute() or compute_from_wide()"
        )

    # -------- 通用辅助 --------

    def __repr__(self) -> str:
        return f"<Factor {self.name} direction={self.direction}>"

    def to_meta(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "direction": self.direction,
            "inputs": list(self.inputs),
            "class": type(self).__name__,
        }


__all__ = ["FactorBase", "FACTOR_REGISTRY", "register"]
