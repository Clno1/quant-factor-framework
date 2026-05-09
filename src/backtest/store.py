"""
回测任务持久化层。

目录结构：
  outputs/backtests/
    _index.json                              列表索引
    <TASK_UUID>/
      task.json                              任务元信息 + 状态（pending/running/success/failed）+ strategy_snapshot
      returns.parquet                        Top 组日收益
      nav.parquet                            策略净值曲线
      metrics.json                           性能指标
      holdings.parquet                       每个调仓日的 Top 组持仓
      log.txt                                任务独立日志

所有 task.json 写入走原子 rename。
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from src.config import CONFIG, PROJECT_ROOT
from src.strategies.definition import StrategyDefinition
from src.utils.io import atomic_save_json, ensure_dir, load_json, read_parquet
from src.utils.logger import get_logger

log = get_logger(__name__)

_OUT_DIR = (
    Path(CONFIG.webapp.output_dir)
    if Path(CONFIG.webapp.output_dir).is_absolute()
    else PROJECT_ROOT / CONFIG.webapp.output_dir
)
BACKTEST_ROOT: Path = _OUT_DIR / "backtests"
_INDEX_PATH: Path = BACKTEST_ROOT / "_index.json"


STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
TERMINAL_STATUSES = (STATUS_SUCCESS, STATUS_FAILED)


# ---------------------------------------------------------------
# 路径
# ---------------------------------------------------------------

def task_dir(task_id: str) -> Path:
    d = BACKTEST_ROOT / task_id
    ensure_dir(d)
    return d


def _task_json_path(task_id: str) -> Path:
    return BACKTEST_ROOT / task_id / "task.json"


# ---------------------------------------------------------------
# 索引
# ---------------------------------------------------------------

def _load_index() -> list[dict[str, Any]]:
    if not _INDEX_PATH.exists():
        return []
    try:
        data = load_json(_INDEX_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("backtests _index.json corrupted, rebuilding. error=%s", e)
        return _rebuild_index()
    if isinstance(data, dict):
        data = data.get("tasks", [])
    return list(data or [])


def _save_index(entries: list[dict[str, Any]]) -> None:
    ensure_dir(BACKTEST_ROOT)
    atomic_save_json(entries, _INDEX_PATH)


def _task_summary(task: dict[str, Any]) -> dict[str, Any]:
    """从完整 task dict 抽摘要给列表页用。"""
    metrics = task.get("metrics") or {}
    wl_snap = task.get("watchlist_snapshot") or {}
    universe = task.get("universe") or ""
    # 为 watchlist:<uuid> 类型的 universe 提供友好的显示名
    universe_label = universe
    if universe.startswith("watchlist:") and wl_snap:
        universe_label = wl_snap.get("name") or universe
    return {
        "id": task.get("id"),
        "name": task.get("name") or "",
        "strategy_id": task.get("strategy_id"),
        "strategy_name": (task.get("strategy_snapshot") or {}).get("name") or "",
        "universe": universe,
        "universe_label": universe_label,
        "date_start": (task.get("date_range") or {}).get("resolved_start"),
        "date_end":   (task.get("date_range") or {}).get("resolved_end"),
        "status": task.get("status"),
        "created_at": task.get("created_at"),
        "duration_sec": task.get("duration_sec"),
        "AnnReturn": metrics.get("AnnReturn"),
        "Sharpe":    metrics.get("Sharpe"),
        "MaxDD":     metrics.get("MaxDD"),
    }


def _rebuild_index() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not BACKTEST_ROOT.exists():
        return entries
    for d in BACKTEST_ROOT.iterdir():
        if not d.is_dir():
            continue
        p = d / "task.json"
        if not p.exists():
            continue
        try:
            task = load_json(p)
            entries.append(_task_summary(task))
        except Exception as e:  # noqa: BLE001
            log.warning("Skip broken backtest dir %s: %s", d, e)
    entries.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    _save_index(entries)
    return entries


def _upsert_index(task: dict[str, Any]) -> None:
    tid = task.get("id")
    if not tid:
        return
    entries = _load_index()
    entries = [e for e in entries if e.get("id") != tid]
    entries.insert(0, _task_summary(task))
    _save_index(entries)


# ---------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------

def create_task(
    *,
    strategy: StrategyDefinition,
    universe: str,
    start: str,
    end: str,
    resolved_start: str,
    resolved_end: str,
    n_groups: int,
    rebalance_days: int,
    top_group: int,
    name: str | None = None,
    watchlist_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    创建一个 pending 的回测任务，返回 task dict（已写盘）。
    resolved_start/end 是 resolve_date_range 解析后的具体 ISO 日期（用于展示）。

    universe 取值：
      - "SP500" / "MAG7"：预设股票池（走 factor_values.parquet）
      - "watchlist:<uuid>"：自定义股票组（走 adhoc 实时计算）。此时应传入
        watchlist_snapshot（WatchlistDefinition.to_dict()）冻结 ticker 清单，
        后续即使 watchlist 被编辑/删除也不影响本任务可追溯。
    """
    task_id = str(uuid4())
    now = datetime.now().isoformat(timespec="seconds")
    task = {
        "id": task_id,
        "name": (name or "").strip() or f"{strategy.name} @ {universe}",
        "strategy_id": strategy.id,
        "strategy_snapshot": strategy.to_dict(),  # 冻结策略定义
        "universe": universe,
        "watchlist_snapshot": watchlist_snapshot,  # None 表示走预设股票池
        "date_range": {
            "start": start,
            "end": end,
            "resolved_start": resolved_start,
            "resolved_end": resolved_end,
        },
        "n_groups": n_groups,
        "rebalance_days": rebalance_days,
        "top_group": top_group,
        "status": STATUS_PENDING,
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "duration_sec": None,
        "error": None,
        "metrics": None,
        "diagnostics": None,
    }
    d = task_dir(task_id)
    atomic_save_json(task, d / "task.json")
    _upsert_index(task)
    log.info("Backtest task created: id=%s universe=%s strategy=%s",
             task_id, universe, strategy.name)
    return task


def load_task(task_id: str) -> dict[str, Any] | None:
    p = _task_json_path(task_id)
    if not p.exists():
        return None
    return load_json(p)


def update_task(task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """合并更新并原子写回；同时更新索引。"""
    task = load_task(task_id)
    if task is None:
        raise FileNotFoundError(f"Backtest task not found: {task_id}")
    task.update(patch)
    atomic_save_json(task, _task_json_path(task_id))
    _upsert_index(task)
    return task


def list_tasks() -> list[dict[str, Any]]:
    entries = _load_index()
    if not entries and BACKTEST_ROOT.exists() and any(BACKTEST_ROOT.iterdir()):
        entries = _rebuild_index()
    return entries


def delete_task(task_id: str) -> bool:
    d = BACKTEST_ROOT / task_id
    if not d.exists():
        return False
    shutil.rmtree(d, ignore_errors=True)
    entries = [e for e in _load_index() if e.get("id") != task_id]
    _save_index(entries)
    log.info("Backtest task deleted: id=%s", task_id)
    return True


# ---------------------------------------------------------------
# 产物读取（给详情页用）
# ---------------------------------------------------------------

def load_task_artifacts(task_id: str) -> dict[str, Any]:
    """
    返回已落盘的产物，缺失的字段返回空。
    """
    d = BACKTEST_ROOT / task_id
    out: dict[str, Any] = {}
    p_returns = d / "returns.parquet"
    p_nav = d / "nav.parquet"
    p_holdings = d / "holdings.parquet"
    p_metrics = d / "metrics.json"
    if p_returns.exists():
        out["returns"] = read_parquet(p_returns)
    if p_nav.exists():
        out["nav"] = read_parquet(p_nav)
    if p_holdings.exists():
        out["holdings"] = read_parquet(p_holdings)
    if p_metrics.exists():
        out["metrics"] = load_json(p_metrics)
    return out


# ---------------------------------------------------------------
# 启动恢复：把上次进程死掉时残留的 running 任务标为 failed
# ---------------------------------------------------------------

def startup_recovery() -> int:
    """
    服务启动时调用：扫描所有 task.json，把 status in {pending, running} 的任务
    标记为 failed（因为负责它们的线程已随进程消亡）。
    返回被修正的任务数。
    """
    if not BACKTEST_ROOT.exists():
        return 0
    fixed = 0
    for d in BACKTEST_ROOT.iterdir():
        if not d.is_dir():
            continue
        p = d / "task.json"
        if not p.exists():
            continue
        try:
            task = load_json(p)
        except Exception:  # noqa: BLE001
            continue
        if task.get("status") in (STATUS_PENDING, STATUS_RUNNING):
            task["status"] = STATUS_FAILED
            task["finished_at"] = datetime.now().isoformat(timespec="seconds")
            prev_err = task.get("error") or ""
            task["error"] = (
                "任务被服务重启中断（startup_recovery）"
                + (f"\n上一次错误：{prev_err}" if prev_err else "")
            )
            atomic_save_json(task, p)
            fixed += 1
            log.warning("startup_recovery marked task %s as failed", task.get("id"))
    if fixed:
        _rebuild_index()
    return fixed


__all__ = [
    "BACKTEST_ROOT",
    "STATUS_PENDING", "STATUS_RUNNING", "STATUS_SUCCESS", "STATUS_FAILED",
    "TERMINAL_STATUSES",
    "task_dir",
    "create_task", "load_task", "update_task",
    "list_tasks", "delete_task",
    "load_task_artifacts",
    "startup_recovery",
]
