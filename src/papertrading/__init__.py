"""Internal paper trading module.

This package implements a local FMP-driven simulation account. It is not a
broker paper account; orders, fills, positions, and PnL are simulated and
persisted transactionally in the application SQLite database.
"""

from src.papertrading.definition import PaperTradingValidationError
from src.papertrading.runner import run_account_once
from src.papertrading.store import (
    create_account,
    delete_account,
    list_accounts,
    load_account,
    load_account_artifacts,
    update_account,
)

__all__ = [
    "PaperTradingValidationError",
    "create_account",
    "delete_account",
    "list_accounts",
    "load_account",
    "load_account_artifacts",
    "run_account_once",
    "update_account",
]
