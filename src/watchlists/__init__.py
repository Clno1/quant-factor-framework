"""
Watchlist（自定义股票池）模块。

Watchlist 是一批带权重的股票集合：
  - 回测场景：忽略权重，仅把 ticker 集合作为股票池输入（策略 → 五分位 → Top 组）
  - 模拟盘场景（未来）：按权重直接下单

支持：
  - CRUD（允许同名，按 UUID 唯一）
  - 编辑（改名、增减 ticker、改权重）
  - 回测通过 strategy_snapshot/watchlist_snapshot 冻结保护
"""
from src.watchlists.definition import (
    WatchlistDefinition,
    WatchlistItem,
    normalize_weights,
)
from src.watchlists.store import (
    WATCHLIST_ROOT,
    create_watchlist,
    delete_watchlist,
    list_watchlists,
    load_watchlist,
    update_watchlist,
)

__all__ = [
    "WatchlistDefinition",
    "WatchlistItem",
    "normalize_weights",
    "WATCHLIST_ROOT",
    "create_watchlist",
    "delete_watchlist",
    "list_watchlists",
    "load_watchlist",
    "update_watchlist",
]
