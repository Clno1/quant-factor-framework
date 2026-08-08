"""Persistence for backtest jobs and their analytical artifacts.

Mutable task state lives in SQLite. Large immutable result tables stay under
``outputs/backtests/<task_id>/`` as Parquet and decision-replay artifacts.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from src.config import CONFIG, PROJECT_ROOT
from src.storage import app_database
from src.strategies.definition import StrategyDefinition
from src.utils.identifiers import canonical_uuid
from src.utils.io import ensure_dir, read_parquet
from src.utils.logger import get_logger


log = get_logger(__name__)
_OUT_DIR = (
    Path(CONFIG.webapp.output_dir)
    if Path(CONFIG.webapp.output_dir).is_absolute()
    else PROJECT_ROOT / CONFIG.webapp.output_dir
)
BACKTEST_ROOT: Path = _OUT_DIR / "backtests"

STATUS_PENDING = "pending"
STATUS_WAITING_FOR_DATA = "waiting_for_data"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
TERMINAL_STATUSES = (STATUS_SUCCESS, STATUS_FAILED)
_RECORD_KIND = "backtest"


def _database():
    return app_database(output_dir=BACKTEST_ROOT.parent)


def task_dir(task_id: str) -> Path:
    """Return and create the directory reserved for large task artifacts."""
    task_id = canonical_uuid(task_id, label="task_id")
    directory = BACKTEST_ROOT / task_id
    ensure_dir(directory)
    return directory


def _task_summary(task: dict[str, Any]) -> dict[str, Any]:
    metrics = task.get("metrics") or {}
    snapshot = task.get("watchlist_snapshot") or {}
    universe = str(task.get("universe") or "")
    universe_label = (
        str(snapshot.get("name") or universe)
        if universe.startswith("watchlist:") and snapshot
        else universe
    )
    return {
        "id": task.get("id"),
        "name": task.get("name") or "",
        "strategy_id": task.get("strategy_id"),
        "strategy_name": (task.get("strategy_snapshot") or {}).get("name") or "",
        "universe": universe,
        "universe_label": universe_label,
        "date_start": (task.get("date_range") or {}).get("resolved_start"),
        "date_end": (task.get("date_range") or {}).get("resolved_end"),
        "status": task.get("status"),
        "created_at": task.get("created_at"),
        "duration_sec": task.get("duration_sec"),
        "data_request_id": task.get("data_request_id"),
        "AnnReturn": metrics.get("AnnReturn"),
        "Sharpe": metrics.get("Sharpe"),
        "MaxDD": metrics.get("MaxDD"),
    }


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
    rebalance_mode: str | None = None,
    name: str | None = None,
    watchlist_snapshot: dict[str, Any] | None = None,
    execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one pending task with frozen strategy and universe inputs."""
    task_id = str(uuid4())
    now = datetime.now().isoformat(timespec="seconds")
    task = {
        "id": task_id,
        "name": (name or "").strip() or f"{strategy.name} @ {universe}",
        "strategy_id": strategy.id,
        "strategy_snapshot": strategy.to_dict(),
        "universe": universe,
        "watchlist_snapshot": watchlist_snapshot,
        "date_range": {
            "start": start,
            "end": end,
            "resolved_start": resolved_start,
            "resolved_end": resolved_end,
        },
        "n_groups": n_groups,
        "rebalance_mode": rebalance_mode,
        "rebalance_days": rebalance_days,
        "top_group": top_group,
        "execution": execution,
        "status": STATUS_PENDING,
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "duration_sec": None,
        "error": None,
        "metrics": None,
        "diagnostics": None,
        "data_contract": None,
        "data_request_id": None,
    }
    _database().put_record(
        _RECORD_KIND,
        task_id,
        task,
        _task_summary(task),
        create_only=True,
    )
    task_dir(task_id)
    log.info(
        "Backtest task created: id=%s universe=%s strategy=%s",
        task_id,
        universe,
        strategy.name,
    )
    return task


def load_task(task_id: str) -> dict[str, Any] | None:
    task_id = canonical_uuid(task_id, label="task_id")
    return _database().get_record(_RECORD_KIND, task_id)


def update_task(task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a patch into one SQLite task record."""
    task_id = canonical_uuid(task_id, label="task_id")
    task = load_task(task_id)
    if task is None:
        raise FileNotFoundError(f"Backtest task not found: {task_id}")
    task.update(patch)
    _database().put_record(
        _RECORD_KIND,
        task_id,
        task,
        _task_summary(task),
    )
    return task


def list_tasks() -> list[dict[str, Any]]:
    return _database().list_summaries(_RECORD_KIND)


def delete_task(task_id: str) -> bool:
    task_id = canonical_uuid(task_id, label="task_id")
    directory = BACKTEST_ROOT / task_id
    artifact_exists = directory.exists()
    deleted = _database().delete_record(_RECORD_KIND, task_id)
    if artifact_exists:
        shutil.rmtree(directory)
    if deleted or artifact_exists:
        log.info("Backtest task deleted: id=%s", task_id)
        return True
    return False


def load_task_artifacts(task_id: str) -> dict[str, Any]:
    """Load Parquet artifacts and task metrics for the detail page."""
    task_id = canonical_uuid(task_id, label="task_id")
    directory = BACKTEST_ROOT / task_id
    names = {
        "returns": "returns.parquet",
        "nav": "nav.parquet",
        "holdings": "holdings.parquet",
        "benchmark_returns": "benchmark_returns.parquet",
        "excess_returns": "excess_returns.parquet",
        "holdings_detail": "holdings_detail.parquet",
        "trades": "trades.parquet",
        "costs": "costs.parquet",
    }
    artifacts = {
        name: read_parquet(directory / filename)
        for name, filename in names.items()
        if (directory / filename).exists()
    }
    task = load_task(task_id)
    if task is not None and isinstance(task.get("metrics"), dict):
        artifacts["metrics"] = dict(task["metrics"])
    return artifacts


def startup_recovery() -> int:
    """Mark tasks whose worker thread died with the previous Web process."""
    fixed = 0
    for task in _database().list_records(_RECORD_KIND):
        if task.get("status") not in (STATUS_PENDING, STATUS_RUNNING):
            continue
        previous_error = task.get("error") or ""
        update_task(
            str(task["id"]),
            {
                "status": STATUS_FAILED,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "error": (
                    "任务被服务重启中断（startup_recovery）"
                    + (f"\n上一次错误：{previous_error}" if previous_error else "")
                ),
            },
        )
        fixed += 1
        log.warning("startup_recovery marked task %s as failed", task.get("id"))
    return fixed


__all__ = [
    "BACKTEST_ROOT",
    "STATUS_PENDING",
    "STATUS_WAITING_FOR_DATA",
    "STATUS_RUNNING",
    "STATUS_SUCCESS",
    "STATUS_FAILED",
    "TERMINAL_STATUSES",
    "task_dir",
    "create_task",
    "load_task",
    "update_task",
    "list_tasks",
    "delete_task",
    "load_task_artifacts",
    "startup_recovery",
]
