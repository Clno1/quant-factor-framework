"""SQLite persistence for internal paper-trading accounts and ledgers."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import shutil
import threading
from typing import Any

import pandas as pd

from src.config import CONFIG, PROJECT_ROOT
from src.papertrading.definition import now_iso
from src.storage import app_database
from src.utils.identifiers import canonical_uuid
from src.utils.io import ensure_dir
from src.utils.logger import get_logger


log = get_logger(__name__)
_OUT_DIR = (
    Path(CONFIG.webapp.output_dir)
    if Path(CONFIG.webapp.output_dir).is_absolute()
    else PROJECT_ROOT / CONFIG.webapp.output_dir
)
# The directory now holds only decision-replay artifacts and process lock files.
PAPER_ROOT: Path = _OUT_DIR / "papertrading"
_ACCOUNT_LOCKS: dict[str, threading.RLock] = {}
_ACCOUNT_LOCKS_GUARD = threading.Lock()
_RECORD_KIND = "paper_account"


def _database():
    return app_database(output_dir=PAPER_ROOT.parent)


def account_dir(account_id: str) -> Path:
    """Return the directory reserved for non-OLTP account artifacts."""
    account_id = canonical_uuid(account_id, label="account_id")
    directory = PAPER_ROOT / account_id
    ensure_dir(directory)
    return directory


@contextmanager
def account_run_lock(account_id: str):
    """Serialize one account across Web threads and worker processes."""
    account_id = canonical_uuid(account_id, label="account_id")
    with _ACCOUNT_LOCKS_GUARD:
        thread_lock = _ACCOUNT_LOCKS.setdefault(account_id, threading.RLock())
    with thread_lock:
        lock_path = account_dir(account_id) / ".run.lock"
        stream = lock_path.open("a+b")
        try:
            try:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            except ImportError:
                pass
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except ImportError:
                pass
            stream.close()


def _universe_label(account: dict[str, Any]) -> str:
    universe = str(account.get("universe") or "")
    snapshot = account.get("watchlist_snapshot") or {}
    if universe.startswith("watchlist:") and snapshot:
        return str(snapshot.get("name") or universe)
    return universe


def _summary(account: dict[str, Any]) -> dict[str, Any]:
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


def create_account(account: dict[str, Any]) -> dict[str, Any]:
    account_id = canonical_uuid(account.get("id"), label="account_id")
    account["id"] = account_id
    if _database().get_record(_RECORD_KIND, account_id) is not None:
        raise ValueError(f"Paper account already exists: {account_id}")
    _database().put_record(
        _RECORD_KIND,
        account_id,
        account,
        _summary(account),
        create_only=True,
    )
    log.info("Paper account created: id=%s name=%r", account_id, account.get("name"))
    return account


def load_account(account_id: str) -> dict[str, Any] | None:
    account_id = canonical_uuid(account_id, label="account_id")
    return _database().get_record(_RECORD_KIND, account_id)


def update_account(account_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    account_id = canonical_uuid(account_id, label="account_id")
    account = load_account(account_id)
    if account is None:
        raise FileNotFoundError(f"Paper account not found: {account_id}")
    account.update(patch)
    account["updated_at"] = now_iso()
    _database().put_record(
        _RECORD_KIND,
        account_id,
        account,
        _summary(account),
    )
    return account


def list_accounts() -> list[dict[str, Any]]:
    return _database().list_summaries(_RECORD_KIND)


def delete_account(account_id: str) -> bool:
    account_id = canonical_uuid(account_id, label="account_id")
    if _database().get_record(_RECORD_KIND, account_id) is None:
        return False
    with account_run_lock(account_id):
        deleted = _database().delete_record(_RECORD_KIND, account_id)
    directory = PAPER_ROOT / account_id
    if directory.exists():
        shutil.rmtree(directory)
    if deleted:
        log.info("Paper account deleted: id=%s", account_id)
    return deleted


def load_table(account_id: str, name: str) -> pd.DataFrame:
    account_id = canonical_uuid(account_id, label="account_id")
    frame = _database().get_frame(_RECORD_KIND, account_id, name)
    return frame if frame is not None else pd.DataFrame()


def save_table(account_id: str, name: str, frame: pd.DataFrame) -> None:
    account_id = canonical_uuid(account_id, label="account_id")
    _database().put_frame(_RECORD_KIND, account_id, name, frame)


def append_table(
    account_id: str,
    name: str,
    rows: list[dict[str, Any]],
) -> pd.DataFrame:
    existing = load_table(account_id, name)
    if not rows:
        return existing
    incoming = pd.DataFrame(rows)
    output = (
        pd.concat([existing, incoming], ignore_index=True)
        if not existing.empty
        else incoming
    )
    save_table(account_id, name, output)
    return output


def load_account_artifacts(account_id: str) -> dict[str, pd.DataFrame]:
    return {
        name: load_table(account_id, name)
        for name in (
            "positions",
            "orders",
            "fills",
            "cash_events",
            "equity_curve",
            "target_weights",
            "target_history",
            "position_history",
            "runs",
        )
    }


__all__ = [
    "PAPER_ROOT",
    "account_dir",
    "account_run_lock",
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
