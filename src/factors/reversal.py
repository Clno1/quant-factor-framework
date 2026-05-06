"""
短期反转因子（Short-Term Reversal）。

定义：过去 N 日累计收益（不取负，让 IC 自然显示负相关）
    factor_t = (1 + r_{t-N+1}) * ... * (1 + r_t) - 1
            = exp(sum(log(1 + r_{t-N+1..t}))) - 1

经验：短期内涨多的股票未来 N 日往往跑输（Jegadeesh, 1990）→ direction = -1
研报图中 REVERSAL 的 IC 均值约 -0.0213，IC_IR ≈ -0.06，t = -2.34
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.factors.base import FactorBase, register


class Reversal(FactorBase):
    """可参数化的短期反转因子（过去 N 日累计收益，方向负）。"""

    direction: int = -1
    inputs = ("returns",)

    def __init__(self, window: int, name: str | None = None):
        if window <= 0:
            raise ValueError("window must be > 0")
        self.window = int(window)
        if name:
            self.name = name

    def compute_from_wide(self, wide: dict[str, pd.DataFrame]) -> pd.DataFrame:
        rets = wide.get("returns")
        if rets is None or rets.empty:
            return pd.DataFrame()
        # 用 log return 滚动求和再 expm1，数值更稳定
        log_ret = np.log1p(rets)
        cum_log = log_ret.rolling(window=self.window, min_periods=self.window).sum()
        return np.expm1(cum_log)


@register
class Reversal5D(Reversal):
    name = "REVERSAL"
    description = "5-Day Short-Term Reversal (cumulative return)"

    def __init__(self):
        super().__init__(window=5)


__all__ = ["Reversal", "Reversal5D"]
