"""SQLite repository for user-defined stock pools."""
from __future__ import annotations

from datetime import datetime
import sqlite3
from pathlib import Path
from typing import Any

from src.config import CONFIG, PROJECT_ROOT
from src.storage import app_database
from src.utils.identifiers import canonical_uuid
from src.utils.logger import get_logger
from src.watchlists.definition import WatchlistDefinition


log = get_logger(__name__)
_OUT_DIR = (
    Path(CONFIG.webapp.output_dir)
    if Path(CONFIG.webapp.output_dir).is_absolute()
    else PROJECT_ROOT / CONFIG.webapp.output_dir
)
WATCHLIST_ROOT: Path = _OUT_DIR / "watchlists"
_RECORD_KIND = "watchlist"


def _database():
    return app_database(output_dir=WATCHLIST_ROOT.parent)


def _summary(watchlist: WatchlistDefinition) -> dict[str, Any]:
    return {
        "id": watchlist.id,
        "name": watchlist.name,
        "item_count": len(watchlist.items),
        "updated_at": watchlist.updated_at or watchlist.created_at,
    }


def list_watchlists() -> list[dict[str, Any]]:
    return _database().list_summaries(_RECORD_KIND)


def load_watchlist(wid: str) -> WatchlistDefinition | None:
    wid = canonical_uuid(wid, label="watchlist_id")
    payload = _database().get_record(_RECORD_KIND, wid)
    return WatchlistDefinition.from_dict(payload) if payload is not None else None


def create_watchlist(watchlist: WatchlistDefinition) -> WatchlistDefinition:
    watchlist.validate()
    watchlist.id = canonical_uuid(watchlist.id, label="watchlist_id")
    now = datetime.now().isoformat(timespec="seconds")
    if not watchlist.created_at:
        watchlist.created_at = now
    watchlist.updated_at = now
    try:
        _database().put_record(
            _RECORD_KIND,
            watchlist.id,
            watchlist.to_dict(),
            _summary(watchlist),
            create_only=True,
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"Watchlist ID 已存在: {watchlist.id}") from exc
    log.info(
        "Watchlist created: id=%s name=%s items=%d",
        watchlist.id,
        watchlist.name,
        len(watchlist.items),
    )
    return watchlist


def update_watchlist(watchlist: WatchlistDefinition) -> WatchlistDefinition:
    watchlist.id = canonical_uuid(watchlist.id, label="watchlist_id")
    existing = load_watchlist(watchlist.id)
    if existing is None:
        raise FileNotFoundError(f"Watchlist {watchlist.id} 不存在")
    watchlist.validate()
    watchlist.created_at = existing.created_at or watchlist.created_at
    watchlist.updated_at = datetime.now().isoformat(timespec="seconds")
    _database().put_record(
        _RECORD_KIND,
        watchlist.id,
        watchlist.to_dict(),
        _summary(watchlist),
    )
    log.info(
        "Watchlist updated: id=%s name=%s items=%d",
        watchlist.id,
        watchlist.name,
        len(watchlist.items),
    )
    return watchlist


def delete_watchlist(wid: str) -> bool:
    wid = canonical_uuid(wid, label="watchlist_id")
    deleted = _database().delete_record(_RECORD_KIND, wid)
    if deleted:
        log.info("Watchlist deleted: id=%s", wid)
    return deleted


__all__ = [
    "WATCHLIST_ROOT",
    "create_watchlist",
    "delete_watchlist",
    "list_watchlists",
    "load_watchlist",
    "update_watchlist",
]
