"""
结果存储层：统一把 pipeline 计算产物持久化到 outputs/，
Web 服务仅消费这些缓存，不做任何重计算。

新目录结构（v2，按股票池分区）：
  outputs/
    universes/<UNIVERSE>/
      factors/<FACTOR_NAME>/
        meta.json
        ic.parquet
        ic_summary.json
        group_nav.parquet
        ls_nav.parquet
        ls_returns.parquet
        group_metrics.parquet
        backtest_config.json

为兼容旧版（outputs/factors/...），如果新路径不存在会回退读旧路径。
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

DEFAULT_UNIVERSE = "SP500"


# ---------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------

def _universe_root(universe: str) -> Path:
    return _OUT_DIR / "universes" / universe / "factors"


def _legacy_root() -> Path:
    """兼容旧路径 outputs/factors/。"""
    return _OUT_DIR / "factors"


def factor_dir(name: str, universe: str = DEFAULT_UNIVERSE) -> Path:
    p = _universe_root(universe) / name
    ensure_dir(p)
    return p


# ---------------------------------------------------------------
# 写入
# ---------------------------------------------------------------

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
    universe: str = DEFAULT_UNIVERSE,
) -> Path:
    d = factor_dir(name, universe=universe)
    save_json(meta, d / "meta.json")
    write_parquet(ic.to_frame("IC"), d / "ic.parquet")
    save_json(ic_summary, d / "ic_summary.json")
    write_parquet(group_nav, d / "group_nav.parquet")
    write_parquet(ls_nav.to_frame("LongShort"), d / "ls_nav.parquet")
    write_parquet(ls_returns.to_frame("LongShort"), d / "ls_returns.parquet")
    write_parquet(group_metrics, d / "group_metrics.parquet")
    save_json(backtest_config, d / "backtest_config.json")
    return d


def save_factor_values(
    name: str,
    values: pd.DataFrame,
    *,
    universe: str = DEFAULT_UNIVERSE,
) -> Path:
    """
    单独落盘因子原始值矩阵 date×ticker。策略回测（composer）会按 universe 读取这份数据。

    注意：
      - 这里落的是"预处理前"的 raw 因子值还是"预处理后"的 clean 值，由调用方决定。
        当前 pipeline 调用时传入 `clean`（MAD 去极值 + 横截面 Z-score 后），
        策略合成会再做一次 Z-score 加权，逻辑幂等。
    """
    d = factor_dir(name, universe=universe)
    path = d / "factor_values.parquet"
    write_parquet(values, path)
    return path


def load_factor_values(
    name: str, universe: str = DEFAULT_UNIVERSE,
) -> pd.DataFrame | None:
    """读取因子值矩阵。文件不存在返回 None，由调用方决定报错方式。"""
    d = _universe_root(universe) / name
    p = d / "factor_values.parquet"
    if p.exists():
        return read_parquet(p)
    # 老产物路径无此文件，直接返回 None（提示用户重跑 pipeline）
    return None


# ---------------------------------------------------------------
# 读取
# ---------------------------------------------------------------

def list_universes() -> list[str]:
    """返回所有有产物的股票池名（按字母序）。"""
    root = _OUT_DIR / "universes"
    universes: list[str] = []
    if root.exists():
        for p in root.iterdir():
            if p.is_dir() and (p / "factors").exists():
                # 至少有一个因子目录才算有效
                if any((p / "factors").iterdir()):
                    universes.append(p.name)
    # 兼容旧路径
    if (_legacy_root()).exists() and any((_legacy_root()).iterdir()):
        if DEFAULT_UNIVERSE not in universes:
            universes.append(DEFAULT_UNIVERSE)
    return sorted(universes) or [DEFAULT_UNIVERSE]


def list_factors(universe: str = DEFAULT_UNIVERSE) -> list[str]:
    root = _universe_root(universe)
    if not root.exists():
        # 兼容旧路径：universe=SP500 时 fallback 到 outputs/factors/
        if universe == DEFAULT_UNIVERSE and _legacy_root().exists():
            root = _legacy_root()
        else:
            return []
    return sorted([
        p.name for p in root.iterdir()
        if p.is_dir() and (p / "meta.json").exists()
    ])


def _load_factor_dir(d: Path, name: str) -> dict[str, Any] | None:
    if not (d / "meta.json").exists():
        return None
    return {
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


def load_factor(name: str, universe: str = DEFAULT_UNIVERSE) -> dict[str, Any] | None:
    d = _universe_root(universe) / name
    out = _load_factor_dir(d, name)
    if out is not None:
        return out
    # fallback to legacy
    if universe == DEFAULT_UNIVERSE:
        return _load_factor_dir(_legacy_root() / name, name)
    return None


__all__ = [
    "DEFAULT_UNIVERSE",
    "save_factor_artifacts", "list_factors", "load_factor",
    "factor_dir", "list_universes",
    "save_factor_values", "load_factor_values",
]
