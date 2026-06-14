"""
回测任务异步执行器。

- 单例 `BacktestRunner`，内部持有 `ThreadPoolExecutor`（默认 2 个 worker）
- `submit(task_id)`：提交已在 store 中 created 的任务到后台执行
- 后台线程负责：状态流转 → 合成 → 五分位回测 → 落盘产物 → 写 metrics → 更新索引
- 任务级日志写 `outputs/backtests/<id>/log.txt`（独立 FileHandler，任务结束后 detach 避免泄漏）
- 所有异常都会捕获并写入 task.json.error，不会让线程静默死亡
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.backtest.adhoc import adhoc_compose
from src.backtest.composer import compose_factor
from src.backtest.metrics import performance_summary, relative_performance_summary
from src.backtest.quintile import quintile_backtest
from src.backtest import store as bt_store
from src.config import CONFIG
from src.data import apply_point_in_time_mask, load_wide_tables
from src.strategies.definition import StrategyComponent, StrategyDefinition
from src.utils.io import atomic_save_json, save_json, write_parquet
from src.utils.logger import get_logger

log = get_logger(__name__)


def _resolve_execution_config(task_exec: dict | None) -> dict[str, Any]:
    """Resolve task-level execution overrides against global defaults."""
    task_exec = task_exec or {}
    return {
        "timing": str(
            task_exec.get("timing")
            or getattr(CONFIG.backtest.execution, "timing", "close")
        ).lower(),
        "slippage_bps": float(
            task_exec.get("slippage_bps") if task_exec.get("slippage_bps") is not None
            else getattr(CONFIG.backtest.execution, "slippage_bps", 0.0)
        ),
        "commission_bps": float(
            task_exec.get("commission_bps") if task_exec.get("commission_bps") is not None
            else getattr(CONFIG.backtest.execution, "commission_bps", 0.0)
        ),
    }


def _validate_open_coverage(
    *,
    task_id: str,
    universe: str,
    open_prices: pd.DataFrame | None,
    composite: pd.DataFrame,
) -> float:
    """Require enough open prices for next_open execution."""
    if open_prices is None or open_prices.empty:
        raise FileNotFoundError(
            f"[task={task_id}] universe={universe} requires open prices for next_open."
        )
    aligned = open_prices.reindex(index=composite.index, columns=composite.columns)
    coverage = float(aligned.notna().mean().mean()) if aligned.size else 0.0
    min_coverage = float(
        getattr(CONFIG.backtest.execution, "min_open_coverage", 0.95)
    )
    if coverage < min_coverage:
        raise ValueError(
            f"[task={task_id}] universe={universe} open price coverage is "
            f"{coverage:.2%}, below required {min_coverage:.2%}. "
            "Rebuild raw OHLCV/open.parquet before running next_open backtests."
        )
    return coverage


# ---------------------------------------------------------------
# 单例 Runner
# ---------------------------------------------------------------

class BacktestRunner:
    """异步回测执行器。全局单例，通过 get_runner() 获取。"""

    _instance: "BacktestRunner | None" = None
    _lock = threading.Lock()

    def __init__(self, max_workers: int = 2):
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="backtest"
        )
        self._futures: dict[str, Future] = {}
        self._futures_lock = threading.Lock()
        log.info("BacktestRunner initialized (max_workers=%d)", max_workers)

    @classmethod
    def get(cls) -> "BacktestRunner":
        with cls._lock:
            if cls._instance is None:
                try:
                    mw = int(CONFIG.backtest.get("thread_pool_workers", 2))  # type: ignore[attr-defined]
                except Exception:
                    mw = 2
                cls._instance = cls(max_workers=max(1, mw))
            return cls._instance

    def submit(self, task_id: str) -> None:
        """提交一个已经 created 的任务到后台执行。"""
        with self._futures_lock:
            fut = self._futures.get(task_id)
            if fut is not None and not fut.done():
                log.warning("Task %s already running, skip re-submit.", task_id)
                return
            fut = self._pool.submit(_run_task_safely, task_id)
            self._futures[task_id] = fut

    def shutdown(self, wait: bool = False) -> None:
        self._pool.shutdown(wait=wait)


def get_runner() -> BacktestRunner:
    return BacktestRunner.get()


# ---------------------------------------------------------------
# 单任务执行（线程体）
# ---------------------------------------------------------------

def _run_task_safely(task_id: str) -> None:
    """顶层 wrapper：保证任何异常都被写入 task.json.error，避免线程静默。"""
    task_log_path = bt_store.task_dir(task_id) / "log.txt"
    handler = _attach_file_logger(task_log_path)
    try:
        log.info("[task=%s] start", task_id)
        _run_task(task_id)
        log.info("[task=%s] done", task_id)
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        log.error("[task=%s] failed: %s\n%s", task_id, e, tb)
        try:
            bt_store.update_task(task_id, {
                "status": bt_store.STATUS_FAILED,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "error": f"{type(e).__name__}: {e}\n\n{tb}",
            })
        except Exception as e2:  # noqa: BLE001
            log.error("[task=%s] failed to persist error: %s", task_id, e2)
    finally:
        _detach_file_logger(handler)


def _attach_file_logger(path: Path) -> logging.Handler:
    """挂一个 FileHandler 到项目根 logger，仅这次任务期间有效。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(path, mode="a", encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger().addHandler(fh)
    return fh


def _detach_file_logger(handler: logging.Handler) -> None:
    try:
        logging.getLogger().removeHandler(handler)
        handler.close()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------
# 核心执行流程
# ---------------------------------------------------------------

def _run_task(task_id: str) -> None:
    task = bt_store.load_task(task_id)
    if task is None:
        raise FileNotFoundError(f"Task {task_id} not found")

    # 1) 置 running
    t0 = time.time()
    task = bt_store.update_task(task_id, {
        "status": bt_store.STATUS_RUNNING,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "error": None,
    })

    # 2) 还原策略定义（从冻结的 snapshot 读）
    snapshot = task.get("strategy_snapshot") or {}
    strategy = StrategyDefinition.from_dict(snapshot)
    strategy.validate()  # 防御：冻结快照里的因子仍需存在

    universe: str = task["universe"]
    date_range = task.get("date_range") or {}
    r_start = date_range.get("resolved_start")
    r_end = date_range.get("resolved_end")
    n_groups = int(task.get("n_groups") or CONFIG.backtest.n_groups)
    rebalance_days = int(task.get("rebalance_days") or CONFIG.backtest.rebalance_days)
    rebalance_mode = str(
        task.get("rebalance_mode")
        or getattr(CONFIG.backtest, "rebalance_mode", "every_n_days")
    )
    top_group = int(task.get("top_group") or n_groups)
    exec_cfg = _resolve_execution_config(task.get("execution") or {})
    require_open = exec_cfg["timing"] == "next_open"

    # 3) 合成因子（根据 universe 类型走不同路径）
    is_watchlist = isinstance(universe, str) and universe.startswith("watchlist:")
    if is_watchlist:
        wl_snap = task.get("watchlist_snapshot") or {}
        wl_items = wl_snap.get("items") or []
        wl_tickers = [it["ticker"] for it in wl_items if it.get("ticker")]
        if not wl_tickers:
            raise ValueError(
                f"任务 {task_id} 的 watchlist 快照为空或缺失（universe={universe}），"
                "无法执行。可能是创建任务时未正确冻结 watchlist。"
            )
        log.info("[task=%s] adhoc compose on watchlist=%s (%d tickers)",
                 task_id, wl_snap.get("name"), len(wl_tickers))
        adhoc_result = adhoc_compose(
            components=[StrategyComponent(**c) if isinstance(c, dict) else c
                        for c in strategy.components],
            tickers=wl_tickers,
            start=r_start, end=r_end,
        )
        composite = adhoc_result.composite
        returns = adhoc_result.returns
        open_prices = adhoc_result.open_prices
        prices = adhoc_result.prices
        volumes = adhoc_result.volumes
        open_coverage = (
            _validate_open_coverage(
                task_id=task_id,
                universe=universe,
                open_prices=open_prices,
                composite=composite,
            )
            if require_open else None
        )
        compose_normalized_weights = adhoc_result.normalized_weights
        compose_date_range = adhoc_result.date_range
        extra_diag = {
            "watchlist_name": wl_snap.get("name"),
            "watchlist_id": wl_snap.get("id"),
            "tickers_requested": len(wl_tickers),
            "tickers_used": len(adhoc_result.tickers_used),
            "tickers_missing": adhoc_result.tickers_missing,
            "open_coverage": open_coverage,
        }
    else:
        log.info("[task=%s] composing factor on %s (%s ~ %s)",
                 task_id, universe, r_start, r_end)
        comp_result = compose_factor(
            components=[StrategyComponent(**c) if isinstance(c, dict) else c
                        for c in strategy.components],
            universe=universe,
            start=r_start, end=r_end,
        )
        composite = comp_result.composite
        pit_required = bool(getattr(CONFIG.backtest, "require_point_in_time_universe", False))
        composite, pit_diag = apply_point_in_time_mask(
            composite,
            universe=universe,
            required=pit_required,
        )
        # 4) 加载对应 universe 的 returns + open 宽表
        wide = load_wide_tables(universe=universe, require_open=require_open)
        returns = wide["returns"]
        open_prices = wide.get("open")
        prices = wide.get("adj_close")
        volumes = wide.get("volume")
        if require_open and (open_prices is None or open_prices.empty):
            raise FileNotFoundError(
                f"[task={task_id}] universe={universe} requires open.parquet for "
                "execution.timing=next_open. Run "
                f"`python scripts/run_mvp.py --update --only-universe {universe}`."
            )
        open_coverage = (
            _validate_open_coverage(
                task_id=task_id,
                universe=universe,
                open_prices=open_prices,
                composite=composite,
            )
            if require_open else None
        )
        compose_normalized_weights = comp_result.normalized_weights
        compose_date_range = (
            composite.index.min().strftime("%Y-%m-%d"),
            composite.index.max().strftime("%Y-%m-%d"),
        )
        extra_diag = {
            "point_in_time_universe": pit_diag,
            "open_coverage": open_coverage,
        }

    # 5) 五分位回测（小池自适应降 n_groups）
    n_tickers = composite.shape[1]
    effective_n_groups = n_groups
    if n_tickers < n_groups * 2:
        effective_n_groups = max(2, min(n_groups, n_tickers // 2))
        log.info("[task=%s] reduced n_groups: %d -> %d (small universe)",
                 task_id, n_groups, effective_n_groups)
    effective_top = min(top_group, effective_n_groups)

    log.info("[task=%s] running quintile backtest: n_groups=%d, rebalance=%dd, top=Q%d, "
             "execution=%s slippage=%.1fbps commission=%.1fbps",
             task_id, effective_n_groups, rebalance_days, effective_top,
             exec_cfg["timing"], exec_cfg["slippage_bps"], exec_cfg["commission_bps"])

    result = quintile_backtest(
        composite, returns,
        factor_direction=+1,   # 合成后默认正向（权重已处理方向）
        n_groups=effective_n_groups,
        rebalance_days=rebalance_days,
        rebalance_mode=rebalance_mode,
        open_df=open_prices,
        price_df=prices,
        volume_df=volumes,
        execution=exec_cfg,
    )

    top_col = f"Q{effective_top}"
    if top_col not in result.group_daily_returns.columns:
        raise RuntimeError(
            f"Top 组 {top_col} 不在回测结果中（可能因为股票数过少分组失败）"
        )
    top_returns: pd.Series = result.group_daily_returns[top_col].dropna()
    if top_returns.empty:
        raise RuntimeError("Top 组收益为空，可能因子全 NaN 或分组全空")
    top_nav: pd.Series = (1.0 + top_returns.fillna(0.0)).cumprod()

    # 6) Top 组逐票持仓 / 交易 / 成本明细
    holdings_detail = result.holdings_detail
    trades_detail = result.trades_detail
    costs_detail = result.costs_detail
    top_holdings_detail = holdings_detail.loc[
        holdings_detail["group"] == top_col
    ].copy() if not holdings_detail.empty else pd.DataFrame()
    top_trades_detail = trades_detail.loc[
        trades_detail["group"] == top_col
    ].copy() if not trades_detail.empty else pd.DataFrame()
    top_costs_detail = costs_detail.loc[
        costs_detail["group"] == top_col
    ].copy() if not costs_detail.empty else pd.DataFrame()

    holdings_rows: list[dict[str, Any]] = []
    if not top_holdings_detail.empty:
        for dt, sub in top_holdings_detail.groupby("date", sort=True):
            holdings_rows.append({
                "date": dt,
                "tickers": sub["ticker"].tolist(),
            })
    holdings_df = pd.DataFrame(holdings_rows)

    # 7) 指标
    metrics = performance_summary(top_returns)
    metrics.update(relative_performance_summary(top_returns, result.benchmark_returns))
    log.info("[task=%s] metrics: %s", task_id, metrics)

    # 8) 落盘产物
    d = bt_store.task_dir(task_id)
    write_parquet(top_returns.to_frame("returns"), d / "returns.parquet")
    write_parquet(top_nav.to_frame("nav"), d / "nav.parquet")
    if not result.benchmark_returns.empty:
        write_parquet(result.benchmark_returns.to_frame("returns"), d / "benchmark_returns.parquet")
    if not result.excess_returns.empty:
        write_parquet(result.excess_returns.to_frame("returns"), d / "excess_returns.parquet")
    if not holdings_df.empty:
        # tickers 列是 list，parquet 支持（转成 object）
        write_parquet(holdings_df, d / "holdings.parquet")
    if not top_holdings_detail.empty:
        write_parquet(top_holdings_detail, d / "holdings_detail.parquet")
    if not top_trades_detail.empty:
        write_parquet(top_trades_detail, d / "trades.parquet")
    if not top_costs_detail.empty:
        write_parquet(top_costs_detail, d / "costs.parquet")
    save_json(metrics, d / "metrics.json")

    # 9) 终态
    duration = time.time() - t0
    diagnostics = {
        "composite_shape": list(composite.shape),
        "composite_date_start": compose_date_range[0],
        "composite_date_end": compose_date_range[1],
        "normalized_weights": compose_normalized_weights,
        "effective_n_groups": effective_n_groups,
        "effective_top_group": effective_top,
        "rebalance_mode": rebalance_mode,
        "n_tickers": n_tickers,
        "n_trading_days": int(top_returns.shape[0]),
        "latest_holding_date": holdings_rows[-1]["date"] if holdings_rows else None,
        "latest_holding_tickers": holdings_rows[-1]["tickers"] if holdings_rows else [],
        "n_trade_rows": int(top_trades_detail.shape[0]),
        "total_traded_weight": (
            float(top_trades_detail["trade_abs_weight"].sum())
            if not top_trades_detail.empty else 0.0
        ),
        "total_cost": (
            float(top_costs_detail["cost"].sum())
            if not top_costs_detail.empty else 0.0
        ),
        "execution_used": result.config.get("execution") or exec_cfg,
        "cost_bps_per_year": result.execution_cost_bps_per_year.get(top_col),
    }
    diagnostics.update(extra_diag)
    patch = {
        "status": bt_store.STATUS_SUCCESS,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "duration_sec": round(duration, 3),
        "error": None,
        "metrics": metrics,
        "diagnostics": diagnostics,
    }
    bt_store.update_task(task_id, patch)
    log.info("[task=%s] done in %.2fs, Sharpe=%.3f AnnRet=%.3f MaxDD=%.3f",
             task_id, duration,
             metrics.get("Sharpe") or float("nan"),
             metrics.get("AnnReturn") or float("nan"),
             metrics.get("MaxDD") or float("nan"))


__all__ = [
    "BacktestRunner",
    "get_runner",
]
