"""
结果存储层：统一把 pipeline 计算产物持久化到 outputs/，
Web 服务仅消费这些缓存，不做任何重计算。

目录结构：
  outputs/
    factors/<name>/
      meta.json          # 因子元信息
      ic.parquet         # IC 时序 (date -> ic)
      ic_summary.json    # IC 汇总指标
      group_nav.parquet  # 分组累计净值
      ls_nav.parquet     # Long-Short 净值
      ls_returns.parquet # Long-Short 日收益
      group_metrics.parquet
      backtest_config.json
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from src.config import CONFIG, PROJECT_ROOT
from src.utils.io import ensure_dir, load_json, read_parquet, save_json, write_parquet

_OUT_DIR = (
    Path(CONFIG.webapp.output_dir)
    if Path(CONFIG.webapp.output_dir).is_absolute()
    else PROJECT_ROOT / CONFIG.webapp.output_dir
)


def factor_dir(name: str) -> Path:
    p = _OUT_DIR / "factors" / name
    ensure_dir(p)
    return p


def save_factor_artifacts(
    name: str,
    *,
    meta: dict,
    ic: pd.Series,
    ic_summary: dict,
    group_nav: pd.DataFrame,
    ls_nav: pd.Series,
    ls_returns: pd.Series,
    group_metrics: pd.DataFrame,
    backtest_config: dict,
) -> Path:
    d = factor_dir(name)
    save_json(meta, d / "meta.json")
    write_parquet(ic.to_frame("IC"), d / "ic.parquet")
    save_json(ic_summary, d / "ic_summary.json")
    write_parquet(group_nav, d / "group_nav.parquet")
    write_parquet(ls_nav.to_frame("LongShort"), d / "ls_nav.parquet")
    write_parquet(ls_returns.to_frame("LongShort"), d / "ls_returns.parquet")
    write_parquet(group_metrics, d / "group_metrics.parquet")
    save_json(backtest_config, d / "backtest_config.json")
    return d


def list_factors() -> list[str]:
    root = _OUT_DIR / "factors"
    if not root.exists():
        return []
    return sorted([p.name for p in root.iterdir() if p.is_dir() and (p / "meta.json").exists()])


def load_factor(name: str) -> dict[str, Any] | None:
    d = _OUT_DIR / "factors" / name
    if not (d / "meta.json").exists():
        return None
    out: dict[str, Any] = {
        "name": name,
        "meta": load_json(d / "meta.json"),
        "ic_summary": load_json(d / "ic_summary.json"),
        "backtest_config": load_json(d / "backtest_config.json"),
        "ic": read_parquet(d / "ic.parquet")["IC"] if (d / "ic.parquet").exists() else pd.Series(dtype=float),
        "group_nav": read_parquet(d / "group_nav.parquet") if (d / "group_nav.parquet").exists() else pd.DataFrame(),
        "ls_nav": read_parquet(d / "ls_nav.parquet")["LongShort"] if (d / "ls_nav.parquet").exists() else pd.Series(dtype=float),
        "ls_returns": read_parquet(d / "ls_returns.parquet")["LongShort"] if (d / "ls_returns.parquet").exists() else pd.Series(dtype=float),
        "group_metrics": read_parquet(d / "group_metrics.parquet") if (d / "group_metrics.parquet").exists() else pd.DataFrame(),
    }
    return out


__all__ = ["save_factor_artifacts", "list_factors", "load_factor", "factor_dir"]
