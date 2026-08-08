"""
波动率因子（Low-Volatility Anomaly）。

定义：N 日日收益率的滚动标准差
    factor_t = std(returns_{t-N+1 .. t})

经验方向：**负向**（高波动股票长期收益反而较差），direction = -1
图片研报中：
    VOL_20D / VOL_60D 的 IC 均值显示为正 0.04+，IC_IR ≈ 0.10
    方向是否在不同样本期稳定，交给因子置信评估报告检验；正式回测不能
    使用完整测试期收益反向选择方向。
"""
from __future__ import annotations

import pandas as pd

from src.factors.base import FactorBase, register


class Volatility(FactorBase):
    """可参数化的滚动波动率因子。"""

    direction: int = -1
    inputs = ("returns",)

    def __init__(self, window: int, name: str | None = None):
        if window <= 1:
            raise ValueError("window must be > 1")
        self.window = int(window)
        if name:
            self.name = name

    def compute_from_wide(self, wide: dict[str, pd.DataFrame]) -> pd.DataFrame:
        rets = wide.get("returns")
        if rets is None or rets.empty:
            return pd.DataFrame()
        # min_periods 设成 window 的 80%，让前期可以有部分数据
        return rets.rolling(window=self.window, min_periods=int(self.window * 0.8)).std()


@register
class Volatility20D(Volatility):
    name = "VOL_20D"
    description = "20-Day Realized Volatility"

    def __init__(self):
        super().__init__(window=20)


@register
class Volatility60D(Volatility):
    name = "VOL_60D"
    description = "60-Day Realized Volatility"

    def __init__(self):
        super().__init__(window=60)


__all__ = ["Volatility", "Volatility20D", "Volatility60D"]
