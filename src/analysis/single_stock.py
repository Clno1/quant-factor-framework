"""
单只股票的因子分析。

两类输出：
  1. 因子值时序（8 个因子的历史值，date x factor 宽表）
  2. 当前快照：最新一日的因子值 + 在参考池（SP500）中的分位排名

设计：
  - 池内股票（在 wide 表里）：直接从已构建的宽表里取数据，零网络调用
  - 池外股票：实时调 FMP 拉 OHLCV，临时构造单股宽表，再算因子
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from src.config import CONFIG
from src.data.cleaner import load_wide_tables
from src.data.fmp import get_historical_ohlcv
from src.data.universe import get_universe
from src.factors import FACTOR_REGISTRY, get_factor
from src.utils.logger import get_logger

log = get_logger(__name__)


# ============================================================
# 数据获取
# ============================================================

def _build_single_stock_wide(ticker: str) -> dict[str, pd.DataFrame] | None:
    """实时拉一只股票的 OHLCV，构造能喂给因子的 wide 字典。"""
    start = CONFIG.date_range.start
    end = CONFIG.date_range.end
    df = get_historical_ohlcv(ticker, start, end, dividend_adjusted=True)
    if df is None or df.empty:
        return None
    # 构造单列宽表
    wide = {}
    for col in ["close", "adj_close", "volume"]:
        wide[col] = df[[col]].rename(columns={col: ticker})
    wide["returns"] = wide["adj_close"].pct_change()
    return wide


def _try_load_from_pool(ticker: str, universe: str) -> dict[str, pd.DataFrame] | None:
    """从已计算好的 universe wide 表里取该 ticker 的列。"""
    try:
        wide = load_wide_tables(universe=universe)
    except FileNotFoundError:
        return None
    adj = wide.get("adj_close", pd.DataFrame())
    if ticker not in adj.columns:
        return None
    sub = {}
    for k in ["close", "adj_close", "volume"]:
        df = wide.get(k, pd.DataFrame())
        if ticker in df.columns:
            sub[k] = df[[ticker]]
    sub["returns"] = sub["adj_close"].pct_change()
    return sub


# ============================================================
# 单股因子时序
# ============================================================

@dataclass
class SingleStockResult:
    ticker: str
    source: str                          # "pool" / "live"
    pool_universe: Optional[str]         # 若来自池，告知是哪个池
    factor_ts: pd.DataFrame              # date x factor 宽表
    snapshot: dict                       # 最新一日因子值 + 分位
    meta: dict                           # name / sector / 数据起止
    error: Optional[str] = None


def compute_single_stock_factors(
    ticker: str,
    *,
    reference_universe: str = "SP500",
    enabled_factors: list[str] | None = None,
) -> SingleStockResult:
    """
    计算指定股票的 8 因子时序与分位快照。

    Parameters
    ----------
    ticker : 股票代码（自动转大写）
    reference_universe : 参考股票池（用于算分位排名）
    enabled_factors : 要算的因子列表（None 用配置里 enabled 全部）
    """
    ticker = ticker.upper().strip()
    enabled = enabled_factors or list(CONFIG.factors.enabled)

    # 1. 取数据：先尝试从参考池里直接取（最快）
    sub_wide = _try_load_from_pool(ticker, reference_universe)
    source = "pool"
    if sub_wide is None:
        # 池外：实时拉
        log.info("Ticker %s not in pool [%s], fetching live from FMP ...",
                 ticker, reference_universe)
        sub_wide = _build_single_stock_wide(ticker)
        source = "live"

    if sub_wide is None:
        return SingleStockResult(
            ticker=ticker, source="none", pool_universe=None,
            factor_ts=pd.DataFrame(), snapshot={}, meta={},
            error=f"无法获取 {ticker} 的数据（FMP 也没有返回）。请检查代码是否正确。",
        )

    # 2. 算 8 个因子
    factor_series: dict[str, pd.Series] = {}
    for fname in enabled:
        if fname not in FACTOR_REGISTRY:
            continue
        try:
            f = get_factor(fname)
            df = f.compute_from_wide(sub_wide)
            if not df.empty and ticker in df.columns:
                factor_series[fname] = df[ticker]
        except Exception as e:  # noqa: BLE001
            log.warning("Factor %s failed for %s: %s", fname, ticker, e)

    factor_ts = pd.DataFrame(factor_series).sort_index()
    factor_ts.index.name = "date"

    # 3. 取最新一日快照
    snapshot = _make_snapshot(
        ticker, factor_ts, reference_universe=reference_universe
    )

    # 4. 元信息
    meta = _resolve_meta(ticker, reference_universe)
    meta["data_start"] = str(factor_ts.index.min().date()) if not factor_ts.empty else None
    meta["data_end"]   = str(factor_ts.index.max().date()) if not factor_ts.empty else None
    meta["n_days"]     = len(factor_ts)

    return SingleStockResult(
        ticker=ticker,
        source=source,
        pool_universe=reference_universe if source == "pool" else None,
        factor_ts=factor_ts,
        snapshot=snapshot,
        meta=meta,
    )


# ============================================================
# 分位排名快照
# ============================================================

def _make_snapshot(
    ticker: str,
    factor_ts: pd.DataFrame,
    *,
    reference_universe: str,
) -> dict:
    """
    最新一日的因子值 + 在参考池中的分位排名。

    返回结构：
        {
          "date": "2025-12-31",
          "factors": {
            "MOM_6M":  {"value": -0.18, "rank": 412, "pool_size": 500, "quintile": "Q1", "percentile": 17.6},
            ...
          }
        }
    """
    if factor_ts.empty:
        return {}
    latest_date = factor_ts.index.max()
    latest_row = factor_ts.loc[latest_date]

    # 加载参考池的当日因子值（如果有）
    pool_factor_snapshots: dict[str, pd.Series] = {}
    try:
        pool_wide = load_wide_tables(universe=reference_universe)
        for fname in factor_ts.columns:
            try:
                f = get_factor(fname)
                pool_factor_df = f.compute_from_wide(pool_wide)
                if latest_date in pool_factor_df.index:
                    pool_factor_snapshots[fname] = pool_factor_df.loc[latest_date].dropna()
            except Exception:  # noqa: BLE001
                pass
    except FileNotFoundError:
        log.warning("Reference pool [%s] wide table not built yet, no rank info.",
                    reference_universe)

    out = {"date": str(latest_date.date()), "factors": {}}
    for fname, value in latest_row.items():
        item: dict = {"value": None, "rank": None, "pool_size": None,
                      "quintile": None, "percentile": None}
        if pd.notna(value):
            item["value"] = float(value)
        if fname in pool_factor_snapshots and pd.notna(value):
            pool_vals = pool_factor_snapshots[fname]
            n = len(pool_vals)
            if n > 0:
                # 对池中所有股票的因子值排名（升序，rank=1 表示最小）
                # 我们关心 ticker 在池中的位置：count(pool < value) + 1
                rank = int((pool_vals < value).sum()) + 1
                pct = 100.0 * rank / n
                # 五分位
                q = min(5, max(1, int(np.ceil(pct / 20))))
                item.update({
                    "rank": rank,
                    "pool_size": n,
                    "quintile": f"Q{q}",
                    "percentile": round(pct, 1),
                })
        out["factors"][fname] = item
    return out


# ============================================================
# 元信息（name / sector）
# ============================================================

def _resolve_meta(ticker: str, reference_universe: str) -> dict:
    """从池或 FMP profile 获取股票名 + 行业。"""
    try:
        uni_df = get_universe(name=reference_universe)
        row = uni_df.loc[uni_df["ticker"] == ticker]
        if not row.empty:
            r = row.iloc[0]
            return {
                "ticker": ticker,
                "name":   r.get("name"),
                "sector": r.get("sector"),
                "sub_industry": r.get("sub_industry"),
                "in_pool": True,
            }
    except Exception:  # noqa: BLE001
        pass
    return {
        "ticker": ticker,
        "name":   None,
        "sector": None,
        "sub_industry": None,
        "in_pool": False,
    }


__all__ = ["compute_single_stock_factors", "SingleStockResult"]
