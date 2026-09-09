"""
换手率因子（Turnover）。

严格定义需要"流通股本"才能算 turnover = volume / shares_outstanding。
免费数据源（FMP/yfinance）日线 endpoint 不直接给历史流通股本，
所以这里用业界常见的近似——**20日相对换手率**：
    factor_t = volume_t / mean(volume_{t-20+1 .. t})

含义：今日成交量相对于过去 20 日均量的倍数。
经验方向：**负向**（换手率高 → 投机情绪重 → 未来收益较低），direction = -1
研报图中 TURNOVER 的 IC 均值约 -0.0428，IC_IR ≈ -0.16，t = -6.10
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.factors.base import FactorBase, register


class TurnoverRelative(FactorBase):
    """N 日相对换手率：当日成交量 / 过去 N 日均成交量。"""

    direction: int = -1
    inputs = ("volume",)

    def __init__(self, window: int, name: str | None = None):
        if window <= 1:
            raise ValueError("window must be > 1")
        self.window = int(window)
        if name:
            self.name = name

    def compute_from_wide(self, wide: dict[str, pd.DataFrame]) -> pd.DataFrame:
        vol = wide.get("volume")
        if vol is None or vol.empty:
            return pd.DataFrame()
        # 防 0：均量为 0 的位置返回 NaN，避免除零
        avg = vol.rolling(window=self.window, min_periods=self.window).mean()
        avg = avg.mask(avg.eq(0), np.nan)
        return vol / avg


@register
class Turnover20D(TurnoverRelative):
    name = "TURNOVER"
    description = "20-Day Relative Turnover (volume / 20D mean volume)"

    def __init__(self):
        super().__init__(window=20)


__all__ = ["TurnoverRelative", "Turnover20D"]
