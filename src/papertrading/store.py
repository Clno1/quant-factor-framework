"""Persistence for internal paper trading accounts."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import CONFIG, PROJECT_ROOT
from src.papertrading.definition import now_iso
from src.utils.io import atomic_save_json, ensure_dir, load_json, read_parquet, write_parquet
from src.utils.logger import get_logger

log = get_logger(__name__)


_OUT_DIR = (
    Path(CONFIG.webapp.output_dir)
    if Path(CONFIG.webapp.output_dir).is_absolute()
    else PROJECT_ROOT / CONFIG.webapp.output_dir
)
PAPER_ROOT: Path = _OUT_DIR / "papertrading"
_INDEX_PATH: Path = PAPER_ROOT / "_index.json"


def account_dir(account_id: str) -> Path:
    d = PAPER_ROOT / account_id
    ensure_dir(d)
    return d


def _account_json_path(account_id: str) -> Path:
    return PAPER_ROOT / account_id / "account.json"


def _load_index() -> list[dict[str, Any]]:
    if not _INDEX_PATH.exists():
        return []
    try:
        data = load_json(_INDEX_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("papertrading _index.json corrupted, rebuilding. error=%s", e)
        return _rebuild_index()
    if isinstance(data, dict):
        data = data.get("accounts", [])
    return list(data or [])


def _save_index(entries: list[dict[str, Any]]) -> None:
    ensure_dir(PAPER_ROOT)
    atomic_save_json(entries, _INDEX_PATH)


def _universe_label(account: dict[str, Any]) -> str:
    universe = str(account.get("universe") or "")
    snap = account.get("watchlist_snapshot") or {}
    if universe.startswith("watchlist:") and snap:
        return str(snap.get("name") or universe)
    return universe


def _account_summary(account: dict[str, Any]) -> dict[str, Any]:
    strategy = account.get("strategy_snapshot") or {}
    return {
        "id": account.get("id"),
        "name": account.get("name") or "",
        "strategy_id": account.get("strategy_id"),
        "strategy_name": strategy.get("name") or "",
        "universe": account.get("universe") or "",
        "universe_label": _universe_label(account),
        "status": account.get("status") or "",
        "cash": account.get("cash"),
        "initial_cash": account.get("initial_cash"),
        "last_equity": account.get("last_equity"),
        "last_run_at": account.get("last_run_at"),
        "last_decision_date": account.get("last_decision_date"),
        "last_mark_date": account.get("last_mark_date"),
        "created_at": account.get("created_at"),
        "last_error": account.get("last_error"),
    }


def _rebuild_index() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not PAPER_ROOT.exists():
        return entries
    for d in PAPER_ROOT.iterdir():
        if not d.is_dir():
            continue
        p = d / "account.json"
        if not p.exists():
            continue
        try:
            account = load_json(p)
            entries.append(_account_summary(account))
        except Exception as e:  # noqa: BLE001
            log.warning("Skip broken paper account dir %s: %s", d, e)
    entries.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    _save_index(entries)
    return entries


def _upsert_index(account: dict[str, Any]) -> None:
    aid = account.get("id")
    if not aid:
        return
    entries = [e for e in _load_index() if e.get("id") != aid]
    entries.insert(0, _account_summary(account))
    _save_index(entries)


def create_account(account: dict[str, Any]) -> dict[str, Any]:
    aid = str(account.get("id") or "")
    if not aid:
        raise ValueError("account.id is required")
    d = account_dir(aid)
    if (d / "account.json").exists():
        raise ValueError(f"Paper account already exists: {aid}")
    atomic_save_json(account, d / "account.json")
    _upsert_index(account)
    log.info("Paper account created: id=%s name=%r", aid, account.get("name"))
    return account


def load_account(account_id: str) -> dict[str, Any] | None:
    p = _account_json_path(account_id)
    if not p.exists():
        return None
    return load_json(p)


def update_account(account_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    account = load_account(account_id)
    if account is None:
        raise FileNotFoundError(f"Paper account not found: {account_id}")
    account.update(patch)
    account["updated_at"] = now_iso()
    atomic_save_json(account, _account_json_path(account_id))
    _upsert_index(account)
    return account


def list_accounts() -> list[dict[str, Any]]:
    entries = _load_index()
    if not entries and PAPER_ROOT.exists() and any(PAPER_ROOT.iterdir()):
        entries = _rebuild_index()
    return entries


def delete_account(account_id: str) -> bool:
    d = PAPER_ROOT / account_id
    if not d.exists():
        return False
    shutil.rmtree(d, ignore_errors=True)
    entries = [e for e in _load_index() if e.get("id") != account_id]
    _save_index(entries)
    log.info("Paper account deleted: id=%s", account_id)
    return True


def _read_df(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return read_parquet(path)
    except Exception as e:  # noqa: BLE001
        log.warning("Failed reading %s: %s", path, e)
        return pd.DataFrame()


def load_table(account_id: str, name: str) -> pd.DataFrame:
    return _read_df(account_dir(account_id) / f"{name}.parquet")


def save_table(account_id: str, name: str, df: pd.DataFrame) -> None:
    write_parquet(df, account_dir(account_id) / f"{name}.parquet")


def append_table(account_id: str, name: str, rows: list[dict[str, Any]]) -> pd.DataFrame:
    existing = load_table(account_id, name)
    if rows:
        new_df = pd.DataFrame(rows)
        out = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
    else:
        out = existing
    if not out.empty:
        save_table(account_id, name, out)
    return out


def load_account_artifacts(account_id: str) -> dict[str, pd.DataFrame]:
    return {
        "positions": load_table(account_id, "positions"),
        "orders": load_table(account_id, "orders"),
        "fills": load_table(account_id, "fills"),
        "equity_curve": load_table(account_id, "equity_curve"),
        "target_weights": load_table(account_id, "target_weights"),
        "runs": load_table(account_id, "runs"),
    }


__all__ = [
    "PAPER_ROOT",
    "account_dir",
    "create_account",
    "delete_account",
    "list_accounts",
    "load_account",
    "load_account_artifacts",
    "load_table",
    "save_table",
    "append_table",
    "update_account",
]
