"""Data helpers for the internal paper trading simulator."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import math
from typing import Any
from uuid import uuid4

from src.config import CONFIG
from src.execution import resolve_execution_config
from src.strategies.definition import StrategyDefinition
from src.utils.identifiers import (
    InvalidResourceId,
    canonical_uuid,
    safe_path_component,
)


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
    research_evidence_snapshot: dict[str, Any] | None = None,
    target_universe_snapshot: dict[str, Any] | None = None,
    risk_config: dict[str, Any] | None = None,
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
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise PaperTradingValidationError("初始资金必须大于 0")
    n_groups = int(n_groups)
    top_group = int(top_group)
    if not 1 <= n_groups <= 20:
        raise PaperTradingValidationError("n_groups 必须在 [1, 20] 内")
    if not 1 <= top_group <= n_groups:
        raise PaperTradingValidationError("top_group 必须在 [1, n_groups] 内")
    rebalance_mode = str(rebalance_mode or "").strip().lower()
    if rebalance_mode not in {
        "every_n_days", "month_end", "monthly", "week_end", "weekly",
    }:
        raise PaperTradingValidationError("rebalance_mode 非法")
    try:
        if str(universe).lower().startswith("watchlist:"):
            watchlist_id = canonical_uuid(
                str(universe).split(":", 1)[1],
                label="watchlist_id",
            )
            universe = f"watchlist:{watchlist_id}"
        else:
            universe = safe_path_component(
                str(universe).upper(),
                label="universe",
            )
    except InvalidResourceId as exc:
        raise PaperTradingValidationError(str(exc)) from exc

    strategy.validate()
    account_id = str(uuid4())
    now = now_iso()
    frozen_risk = {
        "require_point_in_time_universe": bool(
            getattr(CONFIG.backtest, "require_point_in_time_universe", True)
        ),
        "tradability": deepcopy(
            dict(getattr(CONFIG.backtest, "tradability", {}))
        ),
    }
    if risk_config is not None:
        supplied_risk = deepcopy(risk_config)
        frozen_risk.update(
            {
                key: value
                for key, value in supplied_risk.items()
                if key != "tradability"
            }
        )
        if "tradability" in supplied_risk:
            frozen_tradability = dict(frozen_risk["tradability"])
            frozen_tradability.update(supplied_risk["tradability"] or {})
            frozen_risk["tradability"] = frozen_tradability
    return {
        "id": account_id,
        "name": name,
        "strategy_id": strategy.id,
        "strategy_snapshot": strategy.to_dict(),
        "universe": universe,
        "watchlist_snapshot": deepcopy(watchlist_snapshot),
        "research_evidence_snapshot": deepcopy(research_evidence_snapshot),
        "target_universe_snapshot": deepcopy(target_universe_snapshot),
        "initial_cash": initial_cash,
        "cash": initial_cash,
        "last_equity": initial_cash,
        "status": STATUS_ACTIVE,
        "n_groups": n_groups,
        "top_group": top_group,
        "rebalance_mode": rebalance_mode,
        "execution": normalize_execution(deepcopy(execution)),
        "risk_config": frozen_risk,
        "created_at": now,
        "updated_at": now,
        "last_run_at": None,
        "last_decision_date": None,
        "last_mark_date": None,
        "last_error": None,
        "diagnostics": None,
        "data_contract": None,
        "data_request_id": None,
        "schema_version": 2,
    }


def account_strategy(account: dict[str, Any]) -> StrategyDefinition:
    snapshot = account.get("strategy_snapshot") or {}
    strategy = StrategyDefinition.from_dict(snapshot)
    strategy.validate()
    return strategy
