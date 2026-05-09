"""
因子库统一视图。

合并两处信息：
  1. src.factors.FACTOR_REGISTRY（代码侧的计算实现 + name/description/direction/inputs）
  2. configs/factor_library.yaml（人工维护的展示元信息：中文名/分类/公式/风险提示）

启动时做一致性校验：
  - yaml 里有 id 但代码未注册 → 抛 FactorLibraryError
  - 代码注册了但 yaml 缺失   → 警告日志 + 用代码字段 fallback

外部统一用 `get_factor_catalog()` 获取因子库全景。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

from src.config import PROJECT_ROOT
from src.factors.base import FACTOR_REGISTRY
from src.utils.logger import get_logger

log = get_logger(__name__)

FACTOR_LIBRARY_PATH = PROJECT_ROOT / "configs" / "factor_library.yaml"


class FactorLibraryError(Exception):
    """因子库元信息校验失败。"""


@dataclass
class FactorEntry:
    """因子库条目（合并 YAML + 代码后的统一视图）。"""
    id: str
    display_name: str
    category: str
    formula: str
    description: str
    direction: int              # +1 / -1 / 0（以代码为准）
    inputs: list[str] = field(default_factory=list)
    risk_note: str = ""
    registered: bool = True     # 是否已在 FACTOR_REGISTRY 注册（未注册 = 不可用）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------
# YAML 载入
# ---------------------------------------------------------------

def _load_yaml_entries(path: Path = FACTOR_LIBRARY_PATH) -> dict[str, dict[str, Any]]:
    """读取 factor_library.yaml，返回 {id: entry_dict}。文件缺失时返回 {}。"""
    if not path.exists():
        log.warning("factor_library.yaml not found at %s, will fallback to code-only metadata.", path)
        return {}
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    factors = raw.get("factors") or []
    by_id: dict[str, dict[str, Any]] = {}
    for item in factors:
        fid = item.get("id")
        if not fid:
            log.warning("factor_library.yaml entry missing id: %s", item)
            continue
        by_id[fid] = item
    return by_id


# ---------------------------------------------------------------
# 统一视图
# ---------------------------------------------------------------

def _build_catalog() -> dict[str, FactorEntry]:
    yaml_entries = _load_yaml_entries()
    out: dict[str, FactorEntry] = {}

    # 1) 所有代码注册的因子（真理源）
    for fid, cls in FACTOR_REGISTRY.items():
        inst = cls()  # 因子构造均无必需参数
        yml = yaml_entries.get(fid, {})
        entry = FactorEntry(
            id=fid,
            display_name=str(yml.get("display_name") or fid),
            category=str(yml.get("category") or "其他"),
            formula=str(yml.get("formula") or ""),
            description=str(yml.get("description") or inst.description or ""),
            direction=int(getattr(inst, "direction", 0) or 0),
            inputs=list(getattr(inst, "inputs", ()) or ()),
            risk_note=str(yml.get("risk_note") or ""),
            registered=True,
        )
        out[fid] = entry

    # 2) 校验：yaml 里有但代码没注册 → 报错
    unknown = set(yaml_entries.keys()) - set(FACTOR_REGISTRY.keys())
    if unknown:
        raise FactorLibraryError(
            f"factor_library.yaml references unregistered factor(s): {sorted(unknown)}. "
            f"Available in code: {sorted(FACTOR_REGISTRY.keys())}"
        )

    # 3) 警告：代码有但 yaml 缺失
    missing_meta = set(FACTOR_REGISTRY.keys()) - set(yaml_entries.keys())
    if missing_meta:
        log.warning(
            "Factor(s) missing YAML metadata (using code fallback): %s",
            sorted(missing_meta),
        )
    return out


# ---------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------

_CATALOG_CACHE: dict[str, FactorEntry] | None = None


def get_factor_catalog(refresh: bool = False) -> dict[str, FactorEntry]:
    """
    获取因子库统一视图（内存级缓存一次）。

    Parameters
    ----------
    refresh : bool
        True 时强制重建（用于热更新 factor_library.yaml 调试）。
    """
    global _CATALOG_CACHE
    if refresh or _CATALOG_CACHE is None:
        _CATALOG_CACHE = _build_catalog()
    return _CATALOG_CACHE


def list_factor_ids() -> list[str]:
    """按 YAML 中的出现顺序返回因子 ID 列表（便于稳定展示）。"""
    yml = _load_yaml_entries()
    ordered = [fid for fid in yml.keys() if fid in FACTOR_REGISTRY]
    # 补上 yaml 没写的注册因子（放末尾）
    rest = [fid for fid in FACTOR_REGISTRY.keys() if fid not in yml]
    return ordered + sorted(rest)


def get_factor_entry(factor_id: str) -> FactorEntry | None:
    return get_factor_catalog().get(factor_id)


def assert_valid_factor_ids(ids: list[str]) -> None:
    """用于策略创建时校验：所有因子都已注册。"""
    catalog = get_factor_catalog()
    unknown = [fid for fid in ids if fid not in catalog or not catalog[fid].registered]
    if unknown:
        raise FactorLibraryError(
            f"Unknown or unregistered factor id(s): {unknown}. "
            f"Valid: {sorted(catalog.keys())}"
        )


__all__ = [
    "FactorEntry",
    "FactorLibraryError",
    "get_factor_catalog",
    "list_factor_ids",
    "get_factor_entry",
    "assert_valid_factor_ids",
    "FACTOR_LIBRARY_PATH",
]
