"""
策略持久化层。

目录结构（与 universe 无关，策略本身不绑定股票池）：
  outputs/strategies/
    _index.json                              列表索引：[{id, name, created_at, n_components}]
    <UUID>/definition.json                   单个策略定义

所有写入走原子 rename（避免读写并发撕裂）。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from src.config import CONFIG, PROJECT_ROOT
from src.strategies.definition import StrategyDefinition, StrategyValidationError
from src.utils.io import atomic_save_json, ensure_dir, load_json
from src.utils.logger import get_logger

log = get_logger(__name__)


_OUT_DIR = (
    Path(CONFIG.webapp.output_dir)
    if Path(CONFIG.webapp.output_dir).is_absolute()
    else PROJECT_ROOT / CONFIG.webapp.output_dir
)
STRATEGY_ROOT: Path = _OUT_DIR / "strategies"
_INDEX_PATH: Path = STRATEGY_ROOT / "_index.json"


# ---------------------------------------------------------------
# 索引
# ---------------------------------------------------------------

def _load_index() -> list[dict[str, Any]]:
    if not _INDEX_PATH.exists():
        return []
    try:
        data = load_json(_INDEX_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("strategies _index.json corrupted, will rebuild. error=%s", e)
        return _rebuild_index()
    if isinstance(data, dict):
        data = data.get("strategies", [])
    return list(data or [])


def _save_index(entries: list[dict[str, Any]]) -> None:
    ensure_dir(STRATEGY_ROOT)
    atomic_save_json(entries, _INDEX_PATH)


def _strategy_summary(s: StrategyDefinition) -> dict[str, Any]:
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description,
        "n_components": len(s.components),
        "created_at": s.created_at,
    }


def _rebuild_index() -> list[dict[str, Any]]:
    """扫描 STRATEGY_ROOT 所有 UUID 目录，重建索引。"""
    entries: list[dict[str, Any]] = []
    if not STRATEGY_ROOT.exists():
        return entries
    for d in STRATEGY_ROOT.iterdir():
        if not d.is_dir():
            continue
        def_path = d / "definition.json"
        if not def_path.exists():
            continue
        try:
            payload = load_json(def_path)
            s = StrategyDefinition.from_dict(payload)
            entries.append(_strategy_summary(s))
        except Exception as e:  # noqa: BLE001
            log.warning("Skip broken strategy dir %s: %s", d, e)
    entries.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    _save_index(entries)
    return entries


# ---------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------

def _strategy_dir(sid: str) -> Path:
    return STRATEGY_ROOT / sid


def create_strategy(strategy: StrategyDefinition) -> StrategyDefinition:
    """
    持久化一个新策略。调用前建议先 validate()。
    返回值即传入的 strategy（含 id、created_at）。
    """
    strategy.validate()
    d = _strategy_dir(strategy.id)
    if d.exists():
        raise StrategyValidationError(f"策略 ID 已存在: {strategy.id}")
    ensure_dir(d)
    atomic_save_json(strategy.to_dict(), d / "definition.json")

    # 追加到索引头部
    index = _load_index()
    index = [e for e in index if e.get("id") != strategy.id]
    index.insert(0, _strategy_summary(strategy))
    _save_index(index)
    log.info("Strategy created: id=%s name=%r components=%d",
             strategy.id, strategy.name, len(strategy.components))
    return strategy


def list_strategies() -> list[dict[str, Any]]:
    """返回策略摘要列表（按创建时间倒序）。"""
    entries = _load_index()
    if not entries and STRATEGY_ROOT.exists() and any(STRATEGY_ROOT.iterdir()):
        entries = _rebuild_index()
    return entries


def load_strategy(sid: str) -> StrategyDefinition | None:
    p = _strategy_dir(sid) / "definition.json"
    if not p.exists():
        return None
    return StrategyDefinition.from_dict(load_json(p))


def delete_strategy(sid: str) -> bool:
    d = _strategy_dir(sid)
    if not d.exists():
        return False
    shutil.rmtree(d, ignore_errors=True)
    index = [e for e in _load_index() if e.get("id") != sid]
    _save_index(index)
    log.info("Strategy deleted: id=%s", sid)
    return True


__all__ = [
    "STRATEGY_ROOT",
    "create_strategy",
    "list_strategies",
    "load_strategy",
    "delete_strategy",
]
