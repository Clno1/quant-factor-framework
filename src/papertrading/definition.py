"""Data helpers for the internal paper trading simulator."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from src.execution import resolve_execution_config
from src.strategies.definition import StrategyDefinition


class PaperTradingValidationError(ValueError):
    """Paper account payload validation failed."""


STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
ACCOUNT_STATUSES = (STATUS_ACTIVE, STATUS_PAUSED)

ORDER_PENDING = "pending"
ORDER_FILLED = "filled"
ORDER_REJECTED = "rejected"
ORDER_CANCELLED = "cancelled"
ORDER_STATUSES = (ORDER_PENDING, ORDER_FILLED, ORDER_REJECTED, ORDER_CANCELLED)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_execution(execution: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(execution or {})
    timing = str(raw.get("timing") or "next_open").lower().strip()
    if timing != "next_open":
        raise PaperTradingValidationError("模拟盘第一版只支持 next_open 成交")
    try:
        min_order_value = float(raw.get("min_order_value", 25.0))
    except (TypeError, ValueError) as e:
        raise PaperTradingValidationError(f"成交参数必须是数字: {e}") from e
    try:
        cfg = resolve_execution_config(raw)
    except ValueError as e:
        raise PaperTradingValidationError(str(e)) from e
    fee_model = str(cfg.get("fee_model") or "").lower()
    slippage_model = str(cfg.get("slippage_model") or "").lower()
    if fee_model not in {
        "simple_bps", "ibkr_us_pro_fixed", "ibkr_us_pro_tiered", "ibkr_us_lite",
    }:
        raise PaperTradingValidationError(f"fee_model 非法：{fee_model}")
    if slippage_model not in {
        "none", "constant_bps", "simple_bps", "volume_share",
    }:
        raise PaperTradingValidationError(f"slippage_model 非法：{slippage_model}")
    slippage_bps = float(cfg.get("slippage_bps", 5.0))
    commission_bps = float(cfg.get("commission_bps", 2.0))
    if slippage_bps < 0 or slippage_bps > 1000:
        raise PaperTradingValidationError("slippage_bps 必须在 [0, 1000] 内")
    if commission_bps < 0 or commission_bps > 1000:
        raise PaperTradingValidationError("commission_bps 必须在 [0, 1000] 内")
    if min_order_value < 0:
        raise PaperTradingValidationError("min_order_value 不能为负")
    cfg["timing"] = "next_open"
    cfg["min_order_value"] = min_order_value
    return cfg


def create_account_payload(
    *,
    name: str,
    strategy: StrategyDefinition,
    universe: str,
    watchlist_snapshot: dict[str, Any] | None,
    initial_cash: float,
    n_groups: int,
    top_group: int,
    rebalance_mode: str,
    execution: dict[str, Any] | None,
) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise PaperTradingValidationError("模拟盘名称不能为空")
    if len(name) > 100:
        raise PaperTradingValidationError("模拟盘名称过长（>100 字符）")
    try:
        initial_cash = float(initial_cash)
    except (TypeError, ValueError) as e:
        raise PaperTradingValidationError(f"初始资金必须是数字: {e}") from e
    if initial_cash <= 0:
        raise PaperTradingValidationError("初始资金必须大于 0")
    n_groups = int(n_groups)
    top_group = int(top_group)
    if n_groups < 1:
        raise PaperTradingValidationError("n_groups 必须大于等于 1")
    if top_group < 1:
        raise PaperTradingValidationError("top_group 必须大于等于 1")

    strategy.validate()
    account_id = str(uuid4())
    now = now_iso()
    return {
        "id": account_id,
        "name": name,
        "strategy_id": strategy.id,
        "strategy_snapshot": strategy.to_dict(),
        "universe": universe,
        "watchlist_snapshot": watchlist_snapshot,
        "initial_cash": initial_cash,
        "cash": initial_cash,
        "last_equity": initial_cash,
        "status": STATUS_ACTIVE,
        "n_groups": n_groups,
        "top_group": top_group,
        "rebalance_mode": rebalance_mode,
        "execution": normalize_execution(execution),
        "created_at": now,
        "updated_at": now,
        "last_run_at": None,
        "last_decision_date": None,
        "last_mark_date": None,
        "last_error": None,
        "diagnostics": None,
        "schema_version": 1,
    }


def account_strategy(account: dict[str, Any]) -> StrategyDefinition:
    snapshot = account.get("strategy_snapshot") or {}
    strategy = StrategyDefinition.from_dict(snapshot)
    strategy.validate()
    return strategy
