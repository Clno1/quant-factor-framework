"""
Watchlist 存储层。

目录结构：
  outputs/
    watchlists/
      _index.json            # [{id, name, item_count, updated_at}]
      <UUID>/
        definition.json      # 完整 WatchlistDefinition

所有写入走原子 rename（避免读写并发撕裂）。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from src.config import CONFIG, PROJECT_ROOT
from src.utils.io import atomic_save_json, ensure_dir, load_json
from src.utils.logger import get_logger
from src.watchlists.definition import WatchlistDefinition

log = get_logger(__name__)


_OUT_DIR = (
    Path(CONFIG.webapp.output_dir)
    if Path(CONFIG.webapp.output_dir).is_absolute()
    else PROJECT_ROOT / CONFIG.webapp.output_dir
)
WATCHLIST_ROOT: Path = _OUT_DIR / "watchlists"
_INDEX_PATH: Path = WATCHLIST_ROOT / "_index.json"


def _definition_path(wid: str) -> Path:
    return WATCHLIST_ROOT / wid / "definition.json"


# ----------------------------------------------------------------
# 索引维护
# ----------------------------------------------------------------

def _load_index() -> list[dict[str, Any]]:
    if not _INDEX_PATH.exists():
        return []
    try:
        data = load_json(_INDEX_PATH)
        if isinstance(data, list):
            return data
    except Exception as e:  # noqa: BLE001
        log.warning("Watchlist index corrupted (%s), rebuilding.", e)
    return _rebuild_index_from_dirs()


def _rebuild_index_from_dirs() -> list[dict[str, Any]]:
    if not WATCHLIST_ROOT.exists():
        return []
    out: list[dict[str, Any]] = []
    for d in WATCHLIST_ROOT.iterdir():
        if not d.is_dir():
            continue
        p = d / "definition.json"
        if not p.exists():
            continue
        try:
            data = load_json(p)
            out.append({
                "id": data["id"],
                "name": data.get("name", ""),
                "item_count": len(data.get("items") or []),
                "updated_at": data.get("updated_at", data.get("created_at", "")),
            })
        except Exception as e:  # noqa: BLE001
            log.warning("Skip broken watchlist %s: %s", d.name, e)
    out.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    atomic_save_json(out, _INDEX_PATH)
    return out


def _write_index(index: list[dict[str, Any]]) -> None:
    index.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    atomic_save_json(index, _INDEX_PATH)


def _upsert_index(wl: WatchlistDefinition) -> None:
    index = _load_index()
    entry = {
        "id": wl.id,
        "name": wl.name,
        "item_count": len(wl.items),
        "updated_at": wl.updated_at or wl.created_at,
    }
    for i, row in enumerate(index):
        if row.get("id") == wl.id:
            index[i] = entry
            break
    else:
        index.append(entry)
    _write_index(index)


def _remove_from_index(wid: str) -> None:
    index = _load_index()
    index = [r for r in index if r.get("id") != wid]
    _write_index(index)


# ----------------------------------------------------------------
# 对外 CRUD
# ----------------------------------------------------------------

def list_watchlists() -> list[dict[str, Any]]:
    """返回列表页用的轻量索引。"""
    return _load_index()


def load_watchlist(wid: str) -> WatchlistDefinition | None:
    p = _definition_path(wid)
    if not p.exists():
        return None
    data = load_json(p)
    return WatchlistDefinition.from_dict(data)


def create_watchlist(wl: WatchlistDefinition) -> WatchlistDefinition:
    wl.validate()
    now = datetime.now().isoformat(timespec="seconds")
    if not wl.created_at:
        wl.created_at = now
    wl.updated_at = now
    p = _definition_path(wl.id)
    ensure_dir(p)
    atomic_save_json(wl.to_dict(), p)
    _upsert_index(wl)
    log.info("Watchlist created: id=%s name=%s items=%d",
             wl.id, wl.name, len(wl.items))
    return wl


def update_watchlist(wl: WatchlistDefinition) -> WatchlistDefinition:
    """就地更新。保留原 created_at，刷新 updated_at。"""
    existing = load_watchlist(wl.id)
    if existing is None:
        raise FileNotFoundError(f"Watchlist {wl.id} 不存在")
    wl.validate()
    wl.created_at = existing.created_at or wl.created_at
    wl.updated_at = datetime.now().isoformat(timespec="seconds")
    p = _definition_path(wl.id)
    atomic_save_json(wl.to_dict(), p)
    _upsert_index(wl)
    log.info("Watchlist updated: id=%s name=%s items=%d",
             wl.id, wl.name, len(wl.items))
    return wl


def delete_watchlist(wid: str) -> bool:
    d = WATCHLIST_ROOT / wid
    if not d.exists():
        return False
    for f in d.iterdir():
        f.unlink(missing_ok=True)
    d.rmdir()
    _remove_from_index(wid)
    log.info("Watchlist deleted: id=%s", wid)
    return True
