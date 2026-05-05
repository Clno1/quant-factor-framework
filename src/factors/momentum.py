"""
动量因子。

经典动量定义（学术界常用，Jegadeesh & Titman, 1993）：
    factor_t = price_{t-skip} / price_{t-skip-lookback} - 1

- lookback: 回看窗口（交易日）
- skip    : 近端跳过天数，规避短期反转效应（通常 21 天 ≈ 1 个月）

本模块默认实现 6 个月动量（MOM_6M），并提供可参数化基类 `Momentum`
便于后续派生 MOM_1M / MOM_3M / MOM_12M。
"""
from __future__ import annotations

import pandas as pd

from src.factors.base import FactorBase, register


class Momentum(FactorBase):
    """可参数化的动量因子基类。"""

    direction: int = +1  # 动量通常为正向因子，IC 会自动核验

    def __init__(self, lookback: int, skip: int = 21, name: str | None = None):
        if lookback <= 0:
            raise ValueError("lookback must be positive")
        if skip < 0:
            raise ValueError("skip must be >= 0")
        self.lookback = int(lookback)
        self.skip = int(skip)
        # 允许子类静态指定 name，也允许实例化时覆盖
        if name:
            self.name = name

    def compute(self, price_df: pd.DataFrame) -> pd.DataFrame:
        if price_df.empty:
            return price_df.copy()
        near = price_df.shift(self.skip)
        far = price_df.shift(self.skip + self.lookback)
        factor = near / far - 1.0
        return factor


@register
class Momentum6M(Momentum):
    """6 个月动量：close_{t-21} / close_{t-21-126} - 1。"""

    name = "MOM_6M"
    description = "6-Month Momentum (skip 1M, lookback 6M)"

    def __init__(self):
        super().__init__(lookback=126, skip=21)


# ---------- 以下为预留扩展，MVP 不启用（在 configs/default.yaml 中启用即可） ----------

@register
class Momentum1M(Momentum):
    name = "MOM_1M"
    description = "1-Month Momentum (skip 0, lookback 21)"

    def __init__(self):
        super().__init__(lookback=21, skip=0)


@register
class Momentum3M(Momentum):
    name = "MOM_3M"
    description = "3-Month Momentum (skip 1M, lookback 3M)"

    def __init__(self):
        super().__init__(lookback=63, skip=21)


@register
class Momentum12M(Momentum):
    name = "MOM_12M"
    description = "12-Month Momentum (skip 1M, lookback 12M)"

    def __init__(self):
        super().__init__(lookback=252, skip=21)


__all__ = ["Momentum", "Momentum6M", "Momentum1M", "Momentum3M", "Momentum12M"]
