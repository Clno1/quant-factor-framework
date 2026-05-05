"""
因子基类 + 注册器。

所有因子继承 FactorBase，统一接口：
    compute(price_df: pd.DataFrame) -> pd.DataFrame
输入：adj_close 宽表 (date x ticker)
输出：因子值宽表 (date x ticker)，同形
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Type

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

    @abstractmethod
    def compute(self, price_df: pd.DataFrame) -> pd.DataFrame:
        """
        输入：adj_close 宽表 (date x ticker)
        输出：因子值宽表 (date x ticker)，缺失处用 NaN
        """

    # -------- 通用辅助 --------

    def __repr__(self) -> str:
        return f"<Factor {self.name} direction={self.direction}>"

    def to_meta(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "direction": self.direction,
            "class": type(self).__name__,
        }


__all__ = ["FactorBase", "FACTOR_REGISTRY", "register"]
