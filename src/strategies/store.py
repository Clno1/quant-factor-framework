"""SQLite repository for factor-composition strategy definitions."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from src.config import CONFIG, PROJECT_ROOT
from src.storage import app_database
from src.strategies.definition import StrategyDefinition, StrategyValidationError
from src.utils.identifiers import canonical_uuid
from src.utils.logger import get_logger


log = get_logger(__name__)
_OUT_DIR = (
    Path(CONFIG.webapp.output_dir)
    if Path(CONFIG.webapp.output_dir).is_absolute()
    else PROJECT_ROOT / CONFIG.webapp.output_dir
)
# Kept as a public namespace anchor and to isolate monkeypatched test databases.
STRATEGY_ROOT: Path = _OUT_DIR / "strategies"
_RECORD_KIND = "strategy"


def _database():
    return app_database(output_dir=STRATEGY_ROOT.parent)


def _summary(strategy: StrategyDefinition) -> dict[str, Any]:
    return {
        "id": strategy.id,
        "name": strategy.name,
        "description": strategy.description,
        "n_components": len(strategy.components),
        "created_at": strategy.created_at,
    }


def create_strategy(strategy: StrategyDefinition) -> StrategyDefinition:
    """Validate and atomically insert one strategy definition."""
    strategy.validate()
    strategy.id = canonical_uuid(strategy.id, label="strategy_id")
    try:
        _database().put_record(
            _RECORD_KIND,
            strategy.id,
            strategy.to_dict(),
            _summary(strategy),
            create_only=True,
        )
    except sqlite3.IntegrityError as exc:
        raise StrategyValidationError(f"策略 ID 已存在: {strategy.id}") from exc
    log.info(
        "Strategy created: id=%s name=%r components=%d",
        strategy.id,
        strategy.name,
        len(strategy.components),
    )
    return strategy


def list_strategies() -> list[dict[str, Any]]:
    """Return strategy summaries in reverse update order."""
    return _database().list_summaries(_RECORD_KIND)


def load_strategy(sid: str) -> StrategyDefinition | None:
    sid = canonical_uuid(sid, label="strategy_id")
    payload = _database().get_record(_RECORD_KIND, sid)
    return StrategyDefinition.from_dict(payload) if payload is not None else None


def delete_strategy(sid: str) -> bool:
    sid = canonical_uuid(sid, label="strategy_id")
    deleted = _database().delete_record(_RECORD_KIND, sid)
    if deleted:
        log.info("Strategy deleted: id=%s", sid)
    return deleted


__all__ = [
    "STRATEGY_ROOT",
    "create_strategy",
    "list_strategies",
    "load_strategy",
    "delete_strategy",
]
