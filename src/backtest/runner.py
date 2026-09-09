"""
回测任务异步执行器。

- 单例 `BacktestRunner`，内部持有 `ThreadPoolExecutor`（默认 2 个 worker）
- `submit(task_id)`：提交已在 store 中 created 的任务到后台执行
- 后台线程负责：状态流转 → 合成 → 五分位回测 → 落盘产物 → 更新 SQLite
- 任务级日志写 `outputs/backtests/<id>/log.txt`（独立 FileHandler，任务结束后 detach 避免泄漏）
- 所有异常都会捕获并写入 SQLite task.error，不会让线程静默死亡
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
from src.backtest.quintile import (
    BacktestCapacityError,
    build_tradable_mask,
)
from src.backtest.quintile_v2 import quintile_backtest_v2
from src.backtest import store as bt_store
from src.config import CONFIG
from src.data.access import (
    MarketDataNotReadyError,
    current_named_contract,
    enqueue_market_data_request,
    load_published_bundle,
    watchlist_universe_frame,
)
from src.data.pit import build_membership_mask, point_in_time_required
from src.data.universe_ids import watchlist_snapshot_data_universe
from src.decision_replay import build_backtest_snapshot, save_snapshot
from src.execution import resolve_execution_config
from src.factors import get_factor
from src.factors.publication import (
    ResearchPublicationError,
    validate_factor_research_publication,
)
from src.storage import DATA_REQUEST_FAILED, app_database
from src.strategies.definition import StrategyComponent, StrategyDefinition
from src.utils.io import write_parquet
from src.utils.logger import get_logger

log = get_logger(__name__)


def _watchlist_tickers(snapshot: dict[str, Any]) -> list[str]:
    return [
        str(item.get("ticker") or "").strip().upper()
        for item in snapshot.get("items") or []
        if str(item.get("ticker") or "").strip()
    ]


def _prepare_task_data_contract(task_id: str) -> dict[str, Any]:
    """Resolve and persist immutable inputs before the task becomes running."""
    task = bt_store.load_task(task_id)
    if task is None:
        raise FileNotFoundError(f"Task {task_id} not found")
    strategy = StrategyDefinition.from_dict(task.get("strategy_snapshot") or {})
    strategy.validate()
    factor_ids = [component.factor_id for component in strategy.components]
    universe = str(task["universe"])
    date_range = task.get("date_range") or {}
    start = date_range.get("resolved_start")
    end = date_range.get("resolved_end")
    require_open = (
        _resolve_execution_config(task.get("execution") or {})["timing"]
        == "next_open"
    )
    existing = task.get("data_contract") or {}

    if universe.startswith("watchlist:"):
        snapshot = task.get("watchlist_snapshot") or {}
        tickers = _watchlist_tickers(snapshot)
        if not tickers:
            raise ValueError("Backtest watchlist snapshot contains no tickers")
        data_universe = watchlist_snapshot_data_universe(snapshot)
        try:
            bundle = load_published_bundle(
                requested_universe=universe,
                data_universe=data_universe,
                tickers=tickers,
                start=start,
                end=end,
                require_open=require_open,
                exact_universe=True,
                factor_ids=factor_ids,
                dataset_version_id=existing.get("dataset_version_id"),
            )
        except MarketDataNotReadyError as exc:
            initial_start = (
                pd.Timestamp(start) - pd.Timedelta(days=400)
            ).strftime("%Y-%m-%d")
            request = enqueue_market_data_request(
                data_universe=data_universe,
                universe_frame=watchlist_universe_frame(snapshot),
                start=str(start),
                end=str(end) if end else None,
                initial_start=initial_start,
                consumer_kind="backtest",
                consumer_id=task_id,
                force=True,
            )
            raise MarketDataNotReadyError(
                f"{exc}; queued data request {request.request_id}",
                data_universe=data_universe,
                coverage=exc.coverage,
                request_id=request.request_id,
            ) from exc
        contract = bundle.contract.to_dict()
    else:
        universe = universe.upper()
        try:
            if existing:
                bundle = load_published_bundle(
                    requested_universe=universe,
                    data_universe=universe,
                    start=start,
                    end=end,
                    require_open=require_open,
                    factor_ids=factor_ids,
                    require_factor_publication=True,
                    dataset_version_id=existing.get("dataset_version_id"),
                    factor_publication_id=existing.get("factor_publication_id"),
                )
                contract = bundle.contract.to_dict()
            else:
                contract = current_named_contract(
                    universe,
                    factor_ids=factor_ids,
                ).to_dict()
        except (ResearchPublicationError, MarketDataNotReadyError) as exc:
            raise MarketDataNotReadyError(
                str(exc),
                data_universe=universe,
            ) from exc

    return bt_store.update_task(
        task_id,
        {
            "data_contract": contract,
            "data_request_id": None,
            "error": None,
        },
    )


def _resolve_execution_config(task_exec: dict | None) -> dict[str, Any]:
    """Resolve task-level execution overrides against global defaults."""
    return resolve_execution_config(task_exec or {})


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
    required = composite.notna()
    required_count = int(required.sum().sum())
    coverage = (
        float((aligned.notna() & required).sum().sum()) / required_count
        if required_count
        else 0.0
    )
    min_coverage = float(
        getattr(CONFIG.backtest.execution, "min_open_coverage", 0.95)
    )
    if coverage < min_coverage:
        raise ValueError(
            f"[task={task_id}] universe={universe} open price coverage is "
            f"{coverage:.2%}, below required {min_coverage:.2%}. "
            "Publish a complete market-data version before running next_open backtests."
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
        self._stop_event = threading.Event()
        self._monitor = threading.Thread(
            target=self._monitor_waiting_tasks,
            name="backtest-data-monitor",
            daemon=True,
        )
        self._monitor.start()
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
        self._stop_event.set()
        self._pool.shutdown(wait=wait)

    def reconcile_waiting(self) -> int:
        """Submit waiting tasks once their data request or research is ready."""
        submitted = 0
        database = app_database(output_dir=bt_store.BACKTEST_ROOT.parent)
        for summary in bt_store.list_tasks():
            if summary.get("status") != bt_store.STATUS_WAITING_FOR_DATA:
                continue
            task_id = str(summary.get("id") or "")
            if not task_id:
                continue
            request_id = summary.get("data_request_id")
            if request_id:
                request = database.get_data_request(str(request_id))
                if request is None:
                    bt_store.update_task(
                        task_id,
                        {
                            "status": bt_store.STATUS_FAILED,
                            "finished_at": datetime.now().isoformat(timespec="seconds"),
                            "error": f"Data request disappeared: {request_id}",
                        },
                    )
                    continue
                if request.status == DATA_REQUEST_FAILED and request.attempts >= 3:
                    bt_store.update_task(
                        task_id,
                        {
                            "status": bt_store.STATUS_FAILED,
                            "finished_at": datetime.now().isoformat(timespec="seconds"),
                            "error": (
                                f"Data request failed after {request.attempts} "
                                f"attempts: {request.error}"
                            ),
                        },
                    )
                    continue
                if request.status != "success":
                    continue
            self.submit(task_id)
            submitted += 1
        return submitted

    def _monitor_waiting_tasks(self) -> None:
        try:
            interval = max(
                5,
                int(getattr(CONFIG.data.foundation, "request_poll_seconds", 15)),
            )
        except Exception:
            interval = 15
        while not self._stop_event.wait(interval):
            try:
                self.reconcile_waiting()
            except Exception:  # noqa: BLE001
                log.exception("Failed reconciling backtests waiting for data")


def get_runner() -> BacktestRunner:
    return BacktestRunner.get()


# ---------------------------------------------------------------
# 单任务执行（线程体）
# ---------------------------------------------------------------

def _run_task_safely(task_id: str) -> None:
    """顶层 wrapper：保证任何异常都写入任务记录，避免线程静默。"""
    task_log_path = bt_store.task_dir(task_id) / "log.txt"
    handler = _attach_file_logger(task_log_path)
    try:
        log.info("[task=%s] start", task_id)
        _prepare_task_data_contract(task_id)
        _run_task(task_id)
        log.info("[task=%s] done", task_id)
    except MarketDataNotReadyError as e:
        log.info("[task=%s] waiting for data: %s", task_id, e)
        bt_store.update_task(
            task_id,
            {
                "status": bt_store.STATUS_WAITING_FOR_DATA,
                "started_at": None,
                "finished_at": None,
                "error": None,
                "data_request_id": e.request_id,
                "diagnostics": {
                    "waiting_for_data": {
                        "data_universe": e.data_universe,
                        "request_id": e.request_id,
                        "coverage": (
                            e.coverage.to_dict()
                            if e.coverage is not None
                            else None
                        ),
                        "message": str(e),
                    }
                },
            },
        )
    except BacktestCapacityError as e:
        details = e.to_dict()
        log.warning("[task=%s] ADV capacity rejected: %s", task_id, e)
        try:
            current = bt_store.load_task(task_id) or {}
            diagnostics = dict(current.get("diagnostics") or {})
            diagnostics["capacity_error"] = details
            started_at = current.get("started_at")
            started = (
                datetime.fromisoformat(str(started_at))
                if started_at
                else None
            )
            finished_at = datetime.now(tz=started.tzinfo if started else None)
            duration_sec = None
            if started is not None:
                duration_sec = max(
                    0.0,
                    (finished_at - started).total_seconds(),
                )
            bt_store.update_task(task_id, {
                "status": bt_store.STATUS_FAILED,
                "finished_at": finished_at.isoformat(timespec="seconds"),
                "duration_sec": (
                    round(duration_sec, 3) if duration_sec is not None else None
                ),
                "error_code": e.code,
                "error": str(e),
                "error_details": details,
                "diagnostics": diagnostics,
            })
        except Exception as e2:  # noqa: BLE001
            log.error("[task=%s] failed to persist capacity error: %s", task_id, e2)
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        log.error("[task=%s] failed: %s\n%s", task_id, e, tb)
        try:
            current = bt_store.load_task(task_id) or {}
            started_at = current.get("started_at")
            started = (
                datetime.fromisoformat(str(started_at))
                if started_at
                else None
            )
            finished_at = datetime.now(tz=started.tzinfo if started else None)
            duration_sec = None
            if started is not None:
                duration_sec = max(
                    0.0,
                    (finished_at - started).total_seconds(),
                )
            bt_store.update_task(task_id, {
                "status": bt_store.STATUS_FAILED,
                "finished_at": finished_at.isoformat(timespec="seconds"),
                "duration_sec": (
                    round(duration_sec, 3) if duration_sec is not None else None
                ),
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
    risk_cfg = task.get("risk_config") or {}
    tradability_cfg = risk_cfg.get("tradability")
    require_point_in_time = bool(
        risk_cfg.get(
            "require_point_in_time_universe",
            getattr(CONFIG.backtest, "require_point_in_time_universe", False),
        )
    )
    require_open = exec_cfg["timing"] == "next_open"
    data_contract = task.get("data_contract") or {}

    # 3) Resolve exactly one published input bundle, then compose the strategy.
    is_watchlist = isinstance(universe, str) and universe.startswith("watchlist:")
    factor_ids = [component.factor_id for component in strategy.components]
    if is_watchlist:
        wl_snap = task.get("watchlist_snapshot") or {}
        wl_items = wl_snap.get("items") or []
        wl_tickers = [it["ticker"] for it in wl_items if it.get("ticker")]
        if not wl_tickers:
            raise ValueError(
                f"任务 {task_id} 的 watchlist 快照为空或缺失（universe={universe}），"
                "无法执行。可能是创建任务时未正确冻结 watchlist。"
            )
        log.info(
            "[task=%s] adhoc compose on watchlist=%s (%d tickers)",
            task_id,
            wl_snap.get("name"),
            len(wl_tickers),
        )
        data_universe = (
            data_contract.get("data_universe")
            or watchlist_snapshot_data_universe(wl_snap)
        )
        bundle = load_published_bundle(
            requested_universe=universe,
            data_universe=data_universe,
            tickers=wl_tickers,
            start=r_start,
            end=r_end,
            require_open=require_open,
            exact_universe=True,
            factor_ids=factor_ids,
            dataset_version_id=data_contract.get("dataset_version_id"),
        )
        adhoc_result = adhoc_compose(
            components=list(strategy.components),
            tickers=wl_tickers,
            start=r_start,
            end=r_end,
            wide=bundle.wide,
        )
        composite = adhoc_result.composite
        returns = adhoc_result.returns
        open_prices = adhoc_result.open_prices
        prices = adhoc_result.prices
        total_return_open_prices = adhoc_result.total_return_open_prices
        total_return_close_prices = adhoc_result.total_return_close_prices
        volumes = adhoc_result.volumes
        factor_raw = adhoc_result.factor_raw
        factor_clean = adhoc_result.factor_clean
        factor_inputs = adhoc_result.factor_inputs
        factor_contributions = adhoc_result.factor_contributions
        membership_mask, pit_result = build_membership_mask(
            composite.index,
            composite.columns,
            universe=data_universe,
            required=True,
            membership_override=bundle.membership,
            membership_source=f"duckdb:{bundle.version.version_id}:membership",
            membership_source_sha256=(
                bundle.version.membership_checksum_sha256
            ),
        )
        if membership_mask is None:
            raise ValueError(
                f"Published custom universe {data_universe} has no membership"
            )
        pit_diag = pit_result.to_dict()
        pit_diag["warning"] = (
            "This backtest uses the frozen watchlist membership for the full "
            "history; it is a fixed-basket experiment, not a reconstructed index."
        )
        open_coverage = (
            _validate_open_coverage(
                task_id=task_id,
                universe=universe,
                open_prices=open_prices,
                composite=composite,
            )
            if require_open
            else None
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
            "factor_warnings": adhoc_result.warnings,
            "point_in_time_universe": pit_diag,
            "data_contract": bundle.contract.to_dict(),
        }
    else:
        log.info(
            "[task=%s] composing factor on %s (%s ~ %s)",
            task_id,
            universe,
            r_start,
            r_end,
        )
        bundle = load_published_bundle(
            requested_universe=universe,
            data_universe=universe,
            start=r_start,
            end=r_end,
            require_open=require_open,
            factor_ids=factor_ids,
            require_factor_publication=True,
            dataset_version_id=data_contract.get("dataset_version_id"),
            factor_publication_id=data_contract.get("factor_publication_id"),
        )
        comp_result = compose_factor(
            components=list(strategy.components),
            universe=universe,
            start=r_start,
            end=r_end,
            expected_generations=bundle.contract.factor_generations,
        )
        composite = comp_result.composite
        pit_required = point_in_time_required(
            universe,
            strict=require_point_in_time,
        )
        membership_mask, pit_result = build_membership_mask(
            composite.index,
            composite.columns,
            universe=universe,
            required=pit_required,
            membership_override=bundle.membership,
            membership_source=f"duckdb:{bundle.version.version_id}:membership",
            membership_source_sha256=(
                bundle.version.membership_checksum_sha256
            ),
        )
        pit_diag = pit_result.to_dict()
        if membership_mask is None:
            membership_mask = pd.DataFrame(
                True,
                index=composite.index,
                columns=composite.columns,
            )
        else:
            composite = composite.where(membership_mask).dropna(how="all")
            if composite.empty:
                raise ValueError(
                    f"PIT membership mask for {universe} removed all observations."
                )

        wide = bundle.wide
        if bundle.prices is None:
            raise RuntimeError("Published bundle has no typed price semantics")
        returns = bundle.prices.total_returns
        open_prices = bundle.prices.execution_open
        prices = bundle.prices.execution_close
        total_return_open_prices = bundle.prices.total_return_open
        total_return_close_prices = bundle.prices.total_return_close
        volumes = wide.get("volume")
        factor_raw = dict(comp_result.factor_raw)
        factor_clean = comp_result.factor_clean
        factor_inputs = comp_result.factor_inputs
        factor_contributions = comp_result.factor_contributions
        for component in strategy.components:
            factor_id = component.factor_id
            if factor_id in factor_raw:
                continue
            raw = get_factor(factor_id).compute_from_wide(wide)
            factor_raw[factor_id] = raw.reindex(
                index=composite.index,
                columns=composite.columns,
            )
            log.info(
                "[task=%s] reconstructed formula-level values for %s "
                "because factor_raw_values.parquet was absent",
                task_id,
                factor_id,
            )
        # Refuse a factor publication replacement that raced composition.
        validate_factor_research_publication(
            universe,
            version=bundle.version,
            factor_ids=factor_ids,
            publication_id=bundle.contract.factor_publication_id,
        )
        if require_open and (open_prices is None or open_prices.empty):
            raise FileNotFoundError(
                f"[task={task_id}] universe={universe} requires published open "
                "prices for execution.timing=next_open."
            )
        open_coverage = (
            _validate_open_coverage(
                task_id=task_id,
                universe=universe,
                open_prices=open_prices,
                composite=composite,
            )
            if require_open
            else None
        )
        compose_normalized_weights = comp_result.normalized_weights
        compose_date_range = (
            composite.index.min().strftime("%Y-%m-%d"),
            composite.index.max().strftime("%Y-%m-%d"),
        )
        extra_diag = {
            "point_in_time_universe": pit_diag,
            "open_coverage": open_coverage,
            "factor_warnings": comp_result.warnings,
            "data_contract": bundle.contract.to_dict(),
        }

    # 5) 五分位回测（小池自适应降 n_groups）
    n_tickers = composite.shape[1]
    effective_n_groups = n_groups
    if n_tickers < n_groups * 2:
        effective_n_groups = max(2, min(n_groups, n_tickers // 2))
        log.info("[task=%s] reduced n_groups: %d -> %d (small universe)",
                 task_id, n_groups, effective_n_groups)
    effective_top = min(top_group, effective_n_groups)
    execution_plan = {
        "requested_n_groups": n_groups,
        "effective_n_groups": effective_n_groups,
        "requested_top_group": top_group,
        "effective_top_group": effective_top,
        "n_tickers": n_tickers,
        "small_universe_adjusted": effective_n_groups != n_groups,
    }
    current_diagnostics = dict(
        (bt_store.load_task(task_id) or {}).get("diagnostics") or {}
    )
    current_diagnostics["execution_plan"] = execution_plan
    bt_store.update_task(task_id, {"diagnostics": current_diagnostics})

    log.info("[task=%s] running quintile backtest: n_groups=%d, rebalance=%dd, top=Q%d, "
             "execution=%s fee_model=%s slippage_model=%s slippage=%.1fbps commission=%.1fbps",
             task_id, effective_n_groups, rebalance_days, effective_top,
             exec_cfg["timing"], exec_cfg.get("fee_model"), exec_cfg.get("slippage_model"),
             exec_cfg["slippage_bps"], exec_cfg["commission_bps"])

    decision_tradable_mask = build_tradable_mask(
        index=composite.index,
        columns=composite.columns,
        returns_df=returns,
        price_df=prices,
        open_df=open_prices,
        volume_df=volumes,
        timing=exec_cfg["timing"],
        tradability=tradability_cfg,
    )
    decision_tradable_mask &= membership_mask.reindex(
        index=composite.index,
        columns=composite.columns,
        fill_value=False,
    )
    result = quintile_backtest_v2(
        composite, returns,
        factor_direction=+1,   # 合成后默认正向（权重已处理方向）
        n_groups=effective_n_groups,
        rebalance_days=rebalance_days,
        rebalance_mode=rebalance_mode,
        execution_open_df=open_prices,
        execution_close_df=prices,
        total_return_open_df=total_return_open_prices,
        total_return_close_df=total_return_close_prices,
        volume_df=volumes,
        tradable_mask=decision_tradable_mask,
        membership_mask=membership_mask,
        membership_events=bundle.membership_events,
        benchmark_returns=bundle.benchmark_returns,
        execution=exec_cfg,
    )
    result.config["benchmark_data_contract"] = dict(
        bundle.contract.benchmark or {}
    )
    result.config["benchmark_ticker"] = (
        (bundle.contract.benchmark or {}).get("ticker")
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
    portfolio_daily = result.portfolio_daily
    position_daily = result.position_daily
    top_holdings_detail = holdings_detail.loc[
        holdings_detail["group"] == top_col
    ].copy() if not holdings_detail.empty else pd.DataFrame()
    top_trades_detail = trades_detail.loc[
        trades_detail["group"] == top_col
    ].copy() if not trades_detail.empty else pd.DataFrame()
    top_costs_detail = costs_detail.loc[
        costs_detail["group"] == top_col
    ].copy() if not costs_detail.empty else pd.DataFrame()
    top_portfolio_daily = portfolio_daily.loc[
        portfolio_daily["group"] == top_col
    ].copy() if not portfolio_daily.empty else pd.DataFrame()
    top_position_daily = position_daily.loc[
        position_daily["group"] == top_col
    ].copy() if not position_daily.empty else pd.DataFrame()

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
    if not top_portfolio_daily.empty:
        write_parquet(top_portfolio_daily, d / "portfolio_daily.parquet")
    if not top_position_daily.empty:
        write_parquet(top_position_daily, d / "position_daily.parquet")

    replay_snapshot = build_backtest_snapshot(
        source_id=task_id,
        strategy_snapshot=snapshot,
        universe=universe,
        composite=composite,
        factor_raw=factor_raw,
        factor_clean=factor_clean,
        factor_inputs=factor_inputs,
        factor_contributions=factor_contributions,
        close_prices=prices,
        market_returns=returns,
        volumes=volumes,
        membership_mask=membership_mask,
        result=result,
        n_groups=effective_n_groups,
        top_group=effective_top,
        normalized_weights=compose_normalized_weights,
        execution=result.config.get("execution") or exec_cfg,
        pit_diagnostics=pit_diag,
    )
    replay_path = save_snapshot(d, replay_snapshot)
    log.info(
        "[task=%s] decision replay saved: %s (%d days, %d tickers)",
        task_id,
        replay_path,
        replay_snapshot.manifest["trading_days"],
        replay_snapshot.manifest["tickers"],
    )

    # 9) 终态
    duration = time.time() - t0
    diagnostics = {
        "composite_shape": list(composite.shape),
        "composite_date_start": compose_date_range[0],
        "composite_date_end": compose_date_range[1],
        "normalized_weights": compose_normalized_weights,
        "effective_n_groups": effective_n_groups,
        "effective_top_group": effective_top,
        "execution_plan": execution_plan,
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
        "ending_nav": (
            float(top_portfolio_daily.iloc[-1]["end_nav"])
            if not top_portfolio_daily.empty else None
        ),
        "max_accounting_error": (
            float(top_portfolio_daily["accounting_error"].abs().max())
            if not top_portfolio_daily.empty else None
        ),
        "execution_used": result.config.get("execution") or exec_cfg,
        "cost_bps_per_year": result.execution_cost_bps_per_year.get(top_col),
        "decision_replay": {
            "available": True,
            "schema_version": replay_snapshot.manifest["schema_version"],
            "trading_days": replay_snapshot.manifest["trading_days"],
            "tickers": replay_snapshot.manifest["tickers"],
            "audit": replay_snapshot.manifest["audit"],
        },
    }
    diagnostics.update(extra_diag)
    patch = {
        "status": bt_store.STATUS_SUCCESS,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "duration_sec": round(duration, 3),
        "error": None,
        "metrics": metrics,
        "diagnostics": diagnostics,
        "data_contract": bundle.contract.to_dict(),
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
