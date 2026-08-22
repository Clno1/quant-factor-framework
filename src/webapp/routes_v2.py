"""
V2 路由：因子库 / 策略库 / 回测 三件套。

页面：
  GET  /factors                          重定向到统一研究总览
  GET  /strategies                       策略列表
  GET  /strategies/new                   新建策略页
  GET  /strategies/{sid}                 策略详情
  GET  /backtests                        回测任务列表
  GET  /backtests/new                    新建回测页（支持 ?strategy_id= 预选）
  GET  /backtests/{tid}                  回测任务详情（含 running 状态轮询）

JSON API：
  GET    /api/factors_catalog            因子库统一视图（YAML+代码合并）
  GET    /api/strategies                 策略列表（含定义）
  POST   /api/strategies                 创建策略（JSON body）
  GET    /api/strategies/{sid}           单策略定义
  DELETE /api/strategies/{sid}           删除策略
  GET    /api/backtests                  回测任务摘要列表
  POST   /api/backtests                  创建回测任务并异步执行
  GET    /api/backtests/{tid}            单任务全量（含 metrics / diagnostics / 产物）
  GET    /api/backtests/{tid}/status     轻量状态轮询端点
  DELETE /api/backtests/{tid}            删除回测任务
"""
from __future__ import annotations

from datetime import datetime
import math
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import pandas as pd
from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.backtest import store as bt_store
from src.backtest.runner import get_runner
from src.config import CONFIG
from src.data.access import (
    MarketDataNotReadyError,
    enqueue_market_data_request,
    watchlist_universe_frame,
)
from src.data.foundation import MarketDataCatalog
from src.data.universe_ids import watchlist_snapshot_data_universe
from src.factors import assert_valid_factor_ids, get_factor_catalog, list_factor_ids
from src.factors.library import FactorLibraryError
from src.execution import resolve_execution_config
from src.papertrading import (
    PaperTradingValidationError,
    create_account as create_paper_account,
    delete_account as delete_paper_account,
    list_accounts as list_paper_accounts,
    load_account as load_paper_account,
    load_account_artifacts,
    run_account_once,
)
from src.papertrading.definition import create_account_payload
from src.papertrading.target import generate_target_weights
from src.research_universes.registry import research_universe_registry
from src.strategies import (
    StrategyComponent,
    StrategyDefinition,
    StrategyValidationError,
    create_strategy,
    delete_strategy,
    list_strategies,
    load_strategy,
)
from src.utils.date_utils import resolve_date_range
from src.utils.identifiers import (
    InvalidResourceId,
    canonical_ticker,
    canonical_uuid,
)
from src.utils.market_calendar import latest_publishable_xnys_session
from src.webapp.results_store import list_universes
from src.visualization.plots_plotly import fig_to_json
import plotly.graph_objects as go


_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

router_v2 = APIRouter()


def _queue_watchlist_market_data(
    snapshot: dict[str, Any],
    *,
    consumer_kind: str,
    consumer_id: str,
):
    start_iso, end_iso, _ = resolve_date_range(
        CONFIG.date_range.start,
        CONFIG.date_range.end,
    )
    return enqueue_market_data_request(
        data_universe=watchlist_snapshot_data_universe(snapshot),
        universe_frame=watchlist_universe_frame(snapshot),
        start=start_iso,
        end=end_iso,
        initial_start=(
            pd.Timestamp(start_iso) - pd.Timedelta(days=400)
        ).strftime("%Y-%m-%d"),
        consumer_kind=consumer_kind,
        consumer_id=consumer_id,
        force=True,
    )


# ---------------------------------------------------------------
# 工具
# ---------------------------------------------------------------

_PLOT_BG = "#0E1117"
_PLOT_PANEL = "#1A1F2E"
_PLOT_GRID = "#262B3A"
_PLOT_TEXT = "#E8EAED"

def _sanitize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
    return obj


def _format_pct(x, digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    return f"{x * 100:.{digits}f}%"


def _format_num(x, digits: int = 3) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    return f"{x:.{digits}f}"


def _format_money(x, digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    try:
        return f"${float(x):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


_LEGACY_ADV_ERROR = re.compile(
    r"decision_date=(?P<decision_date>\d{4}-\d{2}-\d{2}).*?"
    r"ticker=(?P<ticker>[A-Z0-9.\-]+).*?"
    r"requested=(?P<requested>[+\-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?).*?"
    r"allowed=(?P<allowed>[+\-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?).*?"
    r"adv=(?P<adv>[+\-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?)",
    re.DOTALL,
)


def _backtest_failure_view(task: dict[str, Any]) -> dict[str, Any] | None:
    """Turn persisted engine failures into a concise operator-facing diagnosis."""
    if task.get("status") != bt_store.STATUS_FAILED:
        return None
    technical_error = str(task.get("error") or "未知错误")
    details = task.get("error_details") or {}
    if details.get("code") == "ADV_CAPACITY_EXCEEDED":
        order = dict(details.get("worst_order") or {})
        portfolio_value = float(details.get("portfolio_value") or 0.0)
        max_portfolio_value = float(details.get("max_portfolio_value") or 0.0)
        breach_count = int(details.get("breach_count") or 0)
        capacity_scope = "全区间最严格订单"
        recommendation_is_full_period = True
    else:
        match = _LEGACY_ADV_ERROR.search(technical_error)
        if match is None:
            first_line = technical_error.splitlines()[0] if technical_error else "未知错误"
            return {
                "kind": "generic",
                "title": "回测执行失败",
                "message": first_line,
                "technical_error": technical_error,
            }
        requested = float(match.group("requested"))
        allowed = float(match.group("allowed"))
        adv = float(match.group("adv"))
        portfolio_value = float(
            (task.get("execution") or {}).get("portfolio_value") or 0.0
        )
        max_portfolio_value = (
            portfolio_value * allowed / requested
            if requested > 0 and portfolio_value > 0
            else 0.0
        )
        volume_limit = float(
            (((task.get("execution") or {}).get("slippage") or {}).get(
                "volume_limit", 0.025
            ))
            or 0.0
        )
        order = {
            "decision_date": match.group("decision_date"),
            "execution_date": None,
            "group": None,
            "ticker": match.group("ticker"),
            "requested_quantity": requested,
            "allowed_quantity": allowed,
            "adv": adv,
            "participation_rate": requested / adv if adv > 0 else None,
            "volume_limit": volume_limit,
        }
        breach_count = None
        capacity_scope = "首个失败订单推算"
        recommendation_is_full_period = False

    suggested_portfolio_value = None
    if max_portfolio_value > 0:
        step = 100.0 if max_portfolio_value >= 1000 else 10.0
        suggested_portfolio_value = max(
            step,
            math.floor(max_portfolio_value * 0.90 / step) * step,
        )
    retry_url = None
    universe = str(task.get("universe") or "")
    strategy_id = str(task.get("strategy_id") or "")
    if suggested_portfolio_value and strategy_id:
        params = {
            "strategy_id": strategy_id,
            "portfolio_value": f"{suggested_portfolio_value:.2f}",
        }
        if universe.startswith("watchlist:"):
            params["watchlist_id"] = universe.split(":", 1)[1]
        retry_url = f"/backtests/new?{urlencode(params)}"

    return {
        "kind": "capacity",
        "title": "组合规模超过历史流动性容量",
        "message": (
            "系统拒绝假装整笔成交。请降低名义组合资金、扩大股票池，"
            "或另行建设能跨交易日挂单的事件驱动部分成交模型。"
        ),
        "portfolio_value": portfolio_value,
        "max_portfolio_value": max_portfolio_value,
        "suggested_portfolio_value": suggested_portfolio_value,
        "recommendation_is_full_period": recommendation_is_full_period,
        "capacity_scope": capacity_scope,
        "breach_count": breach_count,
        "order": order,
        "retry_url": retry_url,
        "technical_error": technical_error,
    }


def _backtest_group_view(task: dict[str, Any]) -> dict[str, Any]:
    """Resolve requested versus effective groups, including legacy small-pool tasks."""
    requested_n_groups = int(task.get("n_groups") or CONFIG.backtest.n_groups)
    requested_top_group = int(task.get("top_group") or requested_n_groups)
    diagnostics = task.get("diagnostics") or {}
    plan = diagnostics.get("execution_plan") or {}
    if plan:
        return {
            "requested_n_groups": int(
                plan.get("requested_n_groups") or requested_n_groups
            ),
            "effective_n_groups": int(
                plan.get("effective_n_groups") or requested_n_groups
            ),
            "requested_top_group": int(
                plan.get("requested_top_group") or requested_top_group
            ),
            "effective_top_group": int(
                plan.get("effective_top_group") or requested_top_group
            ),
            "n_tickers": int(plan.get("n_tickers") or 0),
            "small_universe_adjusted": bool(
                plan.get("small_universe_adjusted", False)
            ),
        }

    effective_n_groups = diagnostics.get("effective_n_groups")
    effective_top_group = diagnostics.get("effective_top_group")
    snapshot_items = (task.get("watchlist_snapshot") or {}).get("items") or []
    tickers = {
        str(item.get("ticker") or "").strip().upper()
        for item in snapshot_items
        if str(item.get("ticker") or "").strip()
    }
    n_tickers = len(tickers)
    if effective_n_groups is None:
        effective_n_groups = requested_n_groups
        if n_tickers and n_tickers < requested_n_groups * 2:
            effective_n_groups = max(
                2,
                min(requested_n_groups, n_tickers // 2),
            )
    effective_n_groups = int(effective_n_groups)
    effective_top_group = int(
        effective_top_group
        if effective_top_group is not None
        else min(requested_top_group, effective_n_groups)
    )
    return {
        "requested_n_groups": requested_n_groups,
        "effective_n_groups": effective_n_groups,
        "requested_top_group": requested_top_group,
        "effective_top_group": effective_top_group,
        "n_tickers": n_tickers,
        "small_universe_adjusted": effective_n_groups != requested_n_groups,
    }


def _backtest_timing_view(
    task: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return refresh-stable wall-clock and active-stage durations."""

    def parse(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    def seconds_between(start: datetime | None, end: datetime | None) -> float | None:
        if start is None or end is None:
            return None
        if start.tzinfo is None and end.tzinfo is not None:
            end = end.replace(tzinfo=None)
        elif start.tzinfo is not None and end.tzinfo is None:
            end = end.replace(tzinfo=start.tzinfo)
        return round(max(0.0, (end - start).total_seconds()), 1)

    created_at = parse(task.get("created_at"))
    status_changed_at = parse(task.get("status_changed_at"))
    started_at = parse(task.get("started_at"))
    finished_at = parse(task.get("finished_at"))
    reference = now
    if reference is None:
        anchor = started_at or created_at
        reference = datetime.now(anchor.tzinfo) if anchor and anchor.tzinfo else datetime.now()
    status = str(task.get("status") or "")
    active = status in {
        bt_store.STATUS_PENDING,
        bt_store.STATUS_WAITING_FOR_DATA,
        bt_store.STATUS_RUNNING,
    }
    end = reference if active else finished_at
    total_elapsed = seconds_between(created_at, end)

    stage_names = {
        bt_store.STATUS_PENDING: "queue",
        bt_store.STATUS_WAITING_FOR_DATA: "waiting_for_data",
        bt_store.STATUS_RUNNING: "running",
    }
    stage = stage_names.get(status, "finished")
    if status == bt_store.STATUS_RUNNING:
        stage_elapsed = seconds_between(started_at, reference)
    elif status in {bt_store.STATUS_PENDING, bt_store.STATUS_WAITING_FOR_DATA}:
        stage_elapsed = seconds_between(
            status_changed_at or created_at,
            reference,
        )
    elif task.get("duration_sec") is not None:
        stage_elapsed = round(max(0.0, float(task["duration_sec"])), 1)
    else:
        stage_elapsed = seconds_between(started_at, finished_at)

    return {
        "active": active,
        "stage": stage,
        "total_elapsed_sec": total_elapsed,
        "stage_elapsed_sec": stage_elapsed,
    }


def _enabled_universes() -> list[str]:
    """Return registered presets plus readable historical output partitions."""
    cfg_list = [
        entry.universe_id
        for entry in research_universe_registry().full_research_entries()
    ]
    existing = list_universes()
    seen: dict[str, None] = {}
    for u in cfg_list + existing:
        seen.setdefault(u, None)
    return list(seen.keys())


def _factor_catalog_payload() -> list[dict]:
    """因子库卡片用的数据（补充权重分类颜色等）。"""
    cat = get_factor_catalog()
    rows = []
    for fid in list_factor_ids():
        e = cat[fid]
        rows.append({
            "id": e.id,
            "display_name": e.display_name,
            "category": e.category,
            "formula": e.formula,
            "description": e.description,
            "direction": e.direction,
            "risk_note": e.risk_note,
            "inputs": e.inputs,
        })
    return rows


def _strategy_component_rows(strategy: StrategyDefinition) -> list[dict[str, Any]]:
    catalog = get_factor_catalog()
    rows: list[dict[str, Any]] = []
    for c in strategy.components:
        entry = catalog.get(c.factor_id)
        direction = int(entry.direction) if entry else 0
        rows.append({
            "factor_id": c.factor_id,
            "display_name": entry.display_name if entry else c.factor_id,
            "category": entry.category if entry else "—",
            "direction": direction,
            "weight": c.weight,
            "direction_mismatch": (
                direction in {-1, 1}
                and float(c.weight) != 0.0
                and (1 if float(c.weight) > 0 else -1) != direction
            ),
        })
    return rows


def _strategy_research_rows(
    strategy: StrategyDefinition,
    *,
    assessments: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if assessments is None:
        from src.webapp.research_routes import factor_assessment_map

        assessments = factor_assessment_map()
    components = _strategy_component_rows(strategy)
    for component in components:
        assessment = assessments.get(component["factor_id"], {})
        component["cross_verdict"] = assessment.get("verdict") or "INSUFFICIENT"
        component["sp500_verdict"] = (
            (assessment.get("sp500") or {}).get("verdict")
            or (assessment.get("sp500") or {}).get("status")
            or "MISSING"
        )
        component["nasdaq100_verdict"] = (
            (assessment.get("nasdaq100") or {}).get("verdict")
            or (assessment.get("nasdaq100") or {}).get("status")
            or "MISSING"
        )
        component["research_target_session"] = assessment.get("target_session")
    return components


def _strategy_research_map(
    summaries: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    from src.webapp.research_routes import factor_assessment_map

    assessments = factor_assessment_map()
    output: dict[str, list[dict[str, Any]]] = {}
    for summary in summaries:
        strategy = load_strategy(str(summary["id"]))
        if strategy is not None:
            output[strategy.id] = _strategy_research_rows(
                strategy,
                assessments=assessments,
            )
    return output


def _strategy_research_snapshot(
    strategy: StrategyDefinition,
) -> dict[str, Any]:
    from src.webapp.research_routes import factor_research_snapshot

    snapshot = factor_research_snapshot(
        [component.factor_id for component in strategy.components]
    )
    assessments = {
        str(item["factor_id"]): item
        for item in snapshot.get("factors", [])
    }
    snapshot["components"] = _strategy_research_rows(
        strategy,
        assessments=assessments,
    )
    return snapshot


def _risk_config_snapshot() -> dict[str, Any]:
    return {
        "require_point_in_time_universe": bool(
            getattr(CONFIG.backtest, "require_point_in_time_universe", True)
        ),
        "tradability": dict(getattr(CONFIG.backtest, "tradability", {})),
    }


def _target_universe_snapshot(
    universe: str,
    watchlist_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    if watchlist_snapshot is not None:
        return {
            **watchlist_snapshot,
            "requested_universe": universe,
            "universe_type": "TARGET",
        }
    try:
        registered = research_universe_registry().get(universe)
        return {
            **registered.to_dict(),
            "requested_universe": universe,
            "universe_type": "RESEARCH_PRESET_AS_TARGET",
        }
    except Exception:  # noqa: BLE001
        return {
            "universe_id": universe,
            "requested_universe": universe,
            "universe_type": "NAMED_TARGET",
        }


def _default_universe() -> str:
    enabled = _enabled_universes()
    configured = research_universe_registry().primary().universe_id
    if configured in enabled:
        return configured
    return enabled[0] if enabled else "SP500"


def _normalize_ranking_universe(raw: str | None) -> str:
    value = str(raw or "").strip()
    if not value:
        return _default_universe()
    if value.lower().startswith("watchlist:"):
        try:
            watchlist_id = canonical_uuid(
                value.split(":", 1)[1].strip(),
                label="watchlist_id",
            )
        except InvalidResourceId as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return f"watchlist:{watchlist_id}"
    return value.upper()


def _resolve_watchlist_snapshot(universe: str) -> tuple[dict[str, Any] | None, str]:
    if not universe.lower().startswith("watchlist:"):
        return None, universe
    try:
        wid = canonical_uuid(
            universe.split(":", 1)[1].strip(),
            label="watchlist_id",
        )
    except InvalidResourceId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    from src.watchlists import load_watchlist
    wl = load_watchlist(wid)
    if wl is None:
        raise HTTPException(status_code=404, detail=f"股票池不存在: {wid}")
    wl.validate()
    return wl.to_dict(), wl.name


def _build_strategy_ranking(
    *,
    strategy: StrategyDefinition,
    universe: str,
    asof: str | None,
) -> dict[str, Any]:
    watchlist_snapshot, universe_label = _resolve_watchlist_snapshot(universe)
    if not universe.lower().startswith("watchlist:"):
        enabled = _enabled_universes()
        if universe not in enabled:
            raise HTTPException(
                status_code=400,
                detail=f"未知股票池 {universe}，可选: {enabled}",
            )

    try:
        target = generate_target_weights(
            strategy=strategy,
            universe=universe,
            watchlist_snapshot=watchlist_snapshot,
            asof=asof or None,
            n_groups=int(CONFIG.backtest.n_groups),
            top_group=int(CONFIG.backtest.n_groups),
        )
    except MarketDataNotReadyError as exc:
        request_id = exc.request_id
        if watchlist_snapshot is not None:
            request = _queue_watchlist_market_data(
                watchlist_snapshot,
                consumer_kind="strategy_ranking",
                consumer_id=strategy.id,
            )
            request_id = request.request_id
        raise HTTPException(
            status_code=409,
            detail=(
                f"行情尚未发布，已进入补数队列"
                f"{f'（请求 {request_id}）' if request_id else ''}。"
            ),
        ) from exc
    df = target.target_weights.copy()
    if df.empty:
        rows: list[dict[str, Any]] = []
    else:
        df = df.dropna(subset=["score"]).copy()
        df = df.sort_values("score", ascending=False).reset_index(drop=True)
        df.insert(0, "rank", range(1, len(df) + 1))
        df["is_target"] = df["target_weight"].fillna(0) > 0
        df["score"] = df["score"].astype(float)
        df["decision_price"] = pd.to_numeric(df["decision_price"], errors="coerce")
        df["target_weight"] = pd.to_numeric(df["target_weight"], errors="coerce").fillna(0.0)
        rows = _sanitize(df.to_dict(orient="records"))

    target_count = sum(1 for r in rows if r.get("is_target"))
    stock_link_universe = None if universe.lower().startswith("watchlist:") else universe
    if watchlist_snapshot is not None:
        from src.watchlists import record_ranking_activity

        record_ranking_activity(
            str(watchlist_snapshot["id"]),
            strategy_id=strategy.id,
            decision_date=target.decision_date,
            data_contract=target.data_contract,
        )
    return {
        "strategy": strategy.to_dict(),
        "universe": universe,
        "universe_label": universe_label,
        "target_universe_snapshot": _target_universe_snapshot(
            universe,
            watchlist_snapshot,
        ),
        "stock_link_universe": stock_link_universe,
        "asof": asof or "",
        "decision_date": target.decision_date,
        "rows": rows,
        "row_count": len(rows),
        "target_count": target_count,
        "effective_n_groups": target.effective_n_groups,
        "top_group": target.top_group,
        "normalized_weights": target.normalized_weights,
        "tickers_used": len(target.tickers_used),
        "tickers_missing": target.tickers_missing,
        "warnings": target.warnings,
        "data_contract": target.data_contract,
        "standardization_scope": {
            "method": "cross_sectional_zscore_then_weighted_sum",
            "requested_universe": universe,
            "data_universe": target.data_contract.get("data_universe"),
            "decision_date": target.decision_date,
            "eligible_ticker_count": len(rows),
        },
        "score_max": max((float(r["score"]) for r in rows if r.get("score") is not None), default=None),
        "score_min": min((float(r["score"]) for r in rows if r.get("score") is not None), default=None),
    }


# ---------------------------------------------------------------
# 因子库页
# ---------------------------------------------------------------

@router_v2.get("/factors", response_class=HTMLResponse)
def factors_page(request: Request):
    del request
    return RedirectResponse(url="/research", status_code=307)


@router_v2.get("/api/factors_catalog")
def api_factors_catalog():
    try:
        return JSONResponse(_sanitize({"factors": _factor_catalog_payload()}))
    except FactorLibraryError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------
# 策略库页
# ---------------------------------------------------------------

@router_v2.get("/strategies", response_class=HTMLResponse)
def strategies_list_page(request: Request):
    items = list_strategies()
    return templates.TemplateResponse(request, "strategy_list.html", {
        "title": "策略库",
        "items": items,
        "universes": _enabled_universes(),
    })


@router_v2.get("/strategies/new", response_class=HTMLResponse)
def strategies_new_page(request: Request):
    factors = _factor_catalog_payload()
    return templates.TemplateResponse(request, "strategy_new.html", {
        "title": "新建策略",
        "factors": factors,
        "universes": _enabled_universes(),
    })


@router_v2.get("/strategies/{sid}", response_class=HTMLResponse)
def strategy_detail_page(request: Request, sid: UUID):
    sid = str(sid)
    s = load_strategy(sid)
    if s is None:
        raise HTTPException(status_code=404, detail=f"Strategy not found: {sid}")
    components = _strategy_research_rows(s)
    total_abs = sum(abs(c.weight) for c in s.components) or 1.0
    weights_norm = [{"factor_id": c.factor_id, "weight": c.weight / total_abs}
                    for c in s.components]

    # 权重条形图
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=[_format_pct(w["weight"], 1) for w in weights_norm],
        y=[c["display_name"] for c in components],
        orientation="h",
        marker=dict(color="#42A5F5"),
        text=[f"{w['weight']*100:.1f}%" for w in weights_norm],
        textposition="outside",
    ))
    fig.update_layout(
        title=dict(text="归一化权重分布", font=dict(color=_PLOT_TEXT)),
        paper_bgcolor=_PLOT_BG, plot_bgcolor=_PLOT_PANEL,
        font=dict(color=_PLOT_TEXT),
        margin=dict(l=120, r=60, t=60, b=40),
        height=max(260, 50 * len(components) + 80),
        xaxis=dict(showticklabels=False, gridcolor=_PLOT_GRID),
        yaxis=dict(gridcolor=_PLOT_GRID, autorange="reversed"),
    )

    return templates.TemplateResponse(request, "strategy_detail.html", {
        "title": f"策略 · {s.name}",
        "strategy": s.to_dict(),
        "components": components,
        "weights_fig_json": fig_to_json(fig),
        "universes": _enabled_universes(),
    })


@router_v2.get("/rankings", response_class=HTMLResponse)
def rankings_entry_page(request: Request):
    del request
    return RedirectResponse(url="/strategies", status_code=307)


@router_v2.get("/strategies/{sid}/ranking", response_class=HTMLResponse)
def strategy_ranking_page(
    request: Request,
    sid: UUID,
    universe: str | None = Query(None),
    asof: str | None = Query(None),
):
    del request, universe, asof
    return RedirectResponse(url=f"/strategies/{sid}", status_code=307)


# ---------- API：策略 ----------

@router_v2.get("/api/strategies")
def api_list_strategies():
    return JSONResponse(_sanitize({"strategies": list_strategies()}))


@router_v2.get("/api/strategies/{sid}")
def api_get_strategy(sid: UUID):
    sid = str(sid)
    s = load_strategy(sid)
    if s is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    payload = s.to_dict()
    payload["research_evidence"] = _strategy_research_rows(s)
    return JSONResponse(_sanitize(payload))


@router_v2.get("/api/strategies/{sid}/ranking")
def api_get_strategy_ranking(
    sid: UUID,
    universe: str | None = Query(None),
    asof: str | None = Query(None),
):
    sid = str(sid)
    strategy = load_strategy(sid)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    selected_universe = _normalize_ranking_universe(universe)
    try:
        ranking = _build_strategy_ranking(
            strategy=strategy,
            universe=selected_universe,
            asof=(asof or "").strip() or None,
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(_sanitize(ranking))


@router_v2.post("/api/strategies")
def api_create_strategy(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    description = (payload.get("description") or "").strip()
    raw_components = payload.get("components") or []
    try:
        components = [
            StrategyComponent(
                factor_id=str(c["factor_id"]).strip(),
                weight=float(c["weight"]),
            )
            for c in raw_components
        ]
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"成分格式错误: {e}")

    try:
        s = StrategyDefinition.new(name=name, description=description, components=components)
        create_strategy(s)
    except (StrategyValidationError, FactorLibraryError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(_sanitize(s.to_dict()), status_code=201)


@router_v2.delete("/api/strategies/{sid}")
def api_delete_strategy(sid: UUID):
    sid = str(sid)
    ok = delete_strategy(sid)
    if not ok:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return JSONResponse({"deleted": sid})


# ---------------------------------------------------------------
# 回测任务
# ---------------------------------------------------------------

@router_v2.get("/backtests", response_class=HTMLResponse)
def backtests_list_page(request: Request):
    items = bt_store.list_tasks()
    # 格式化指标给模板
    formatted = []
    for t in items:
        formatted.append({
            **t,
            "AnnReturn_fmt": _format_pct(t.get("AnnReturn")),
            "Sharpe_fmt":    _format_num(t.get("Sharpe")),
            "MaxDD_fmt":     _format_pct(t.get("MaxDD")),
        })
    return templates.TemplateResponse(request, "backtest_list.html", {
        "title": "回测",
        "items": formatted,
        "universes": _enabled_universes(),
    })


@router_v2.get("/backtests/new", response_class=HTMLResponse)
def backtests_new_page(
    request: Request,
    strategy_id: str | None = Query(None),
    watchlist_id: str | None = Query(None),
    portfolio_value: float | None = Query(None, gt=0, le=1_000_000_000_000),
):
    from src.watchlists import list_watchlists
    strategies = list_strategies()
    universes = _enabled_universes()
    watchlists = list_watchlists()
    default_start = str(CONFIG.date_range.start)
    default_end = str(CONFIG.date_range.end)
    default_exec = {
        "timing": str(getattr(CONFIG.backtest.execution, "timing", "next_open")),
        "portfolio_value": (
            float(portfolio_value)
            if portfolio_value is not None
            else float(getattr(CONFIG.backtest.execution, "portfolio_value", 100000))
        ),
        "fee_model": str(getattr(CONFIG.backtest.execution, "fee_model", "ibkr_us_pro_fixed")),
        "slippage_model": str(getattr(CONFIG.backtest.execution, "slippage_model", "volume_share")),
        "slippage_bps": float(getattr(CONFIG.backtest.execution, "slippage_bps", 5)),
        "commission_bps": float(getattr(CONFIG.backtest.execution, "commission_bps", 2)),
        "volume_limit": float(getattr(CONFIG.backtest.execution.slippage, "volume_limit", 0.025)),
        "adv_window": int(getattr(CONFIG.backtest.execution.slippage, "adv_window", 20)),
    }
    return templates.TemplateResponse(request, "backtest_new.html", {
        "title": "新建回测",
        "strategies": strategies,
        "strategy_research": _strategy_research_map(strategies),
        "universes": universes,
        "watchlists": watchlists,
        "preselect_strategy_id": strategy_id,
        "preselect_watchlist_id": watchlist_id,
        "default_start": default_start,
        "default_end": default_end,
        "default_exec": default_exec,
    })


@router_v2.get("/backtests/{tid}", response_class=HTMLResponse)
def backtest_detail_page(request: Request, tid: UUID):
    tid = str(tid)
    task = bt_store.load_task(tid)
    if task is None:
        raise HTTPException(status_code=404, detail="Backtest not found")

    # 构造 NAV 图与逐票执行明细（仅成功态）
    nav_fig_json = None
    arts: dict[str, Any] = {}
    if task.get("status") == bt_store.STATUS_SUCCESS:
        arts = bt_store.load_task_artifacts(tid)
        nav_df = arts.get("nav")
        if nav_df is not None and not nav_df.empty:
            nav_series = nav_df.iloc[:, 0]
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=nav_series.index, y=nav_series.values,
                mode="lines", name="策略净值",
                line=dict(color="#42A5F5", width=2),
            ))
            bench_df = arts.get("benchmark_returns")
            if bench_df is not None and not bench_df.empty:
                bench_nav = (1.0 + bench_df.iloc[:, 0].fillna(0)).cumprod()
                fig.add_trace(go.Scatter(
                    x=bench_nav.index, y=bench_nav.values,
                    mode="lines", name="等权基准",
                    line=dict(color="#9CA3AF", width=1.5),
                ))
            fig.add_hline(y=1.0, line_dash="dot", line_color="#888")
            fig.update_layout(
                title=dict(text="策略净值曲线（Top 组）",
                           font=dict(color=_PLOT_TEXT, size=15)),
                paper_bgcolor=_PLOT_BG, plot_bgcolor=_PLOT_PANEL,
                font=dict(color=_PLOT_TEXT),
                margin=dict(l=40, r=40, t=60, b=40),
                height=420, hovermode="x unified",
            )
            fig.update_xaxes(gridcolor=_PLOT_GRID)
            fig.update_yaxes(gridcolor=_PLOT_GRID, title_text="净值（初值=1.0）")
            nav_fig_json = fig_to_json(fig)

    # 成分因子展开
    catalog = get_factor_catalog()
    snapshot = task.get("strategy_snapshot") or {}
    components_rows = []
    for c in snapshot.get("components", []):
        fid = c.get("factor_id")
        entry = catalog.get(fid) if fid else None
        direction = int(entry.direction) if entry else 0
        weight = float(c.get("weight", 0.0))
        components_rows.append({
            "factor_id": fid,
            "display_name": entry.display_name if entry else fid,
            "category": entry.category if entry else "—",
            "direction": direction,
            "weight": weight,
            "direction_mismatch": (
                direction in {-1, 1}
                and weight != 0.0
                and (1 if weight > 0 else -1) != direction
            ),
        })

    # 格式化指标
    metrics_raw = task.get("metrics") or {}
    metrics_fmt = {
        "AnnReturn": _format_pct(metrics_raw.get("AnnReturn")),
        "AnnVol":    _format_pct(metrics_raw.get("AnnVol")),
        "Sharpe":    _format_num(metrics_raw.get("Sharpe")),
        "MaxDD":     _format_pct(metrics_raw.get("MaxDD")),
        "Calmar":    _format_num(metrics_raw.get("Calmar")),
        "WinRate":   _format_pct(metrics_raw.get("WinRate")),
        "BenchmarkAnnReturn": _format_pct(metrics_raw.get("BenchmarkAnnReturn")),
        "ExcessAnnReturn": _format_pct(metrics_raw.get("ExcessAnnReturn")),
        "TrackingError": _format_pct(metrics_raw.get("TrackingError")),
        "InformationRatio": _format_num(metrics_raw.get("InformationRatio")),
        "Beta": _format_num(metrics_raw.get("Beta")),
        "N_days":    metrics_raw.get("N_days", "—"),
    }

    return templates.TemplateResponse(request, "backtest_detail.html", {
        "title": f"回测 · {task.get('name') or tid[:8]}",
        "task": task,
        "group_view": _backtest_group_view(task),
        "task_timing": _backtest_timing_view(task),
        "failure": _backtest_failure_view(task),
        "metrics_fmt": metrics_fmt,
        "components_rows": components_rows,
        "research_snapshot": task.get("research_evidence_snapshot") or {},
        "target_snapshot": task.get("target_universe_snapshot") or {},
        "data_contract": task.get("data_contract") or {},
        "risk_config": task.get("risk_config") or {},
        "holdings_detail": _records_for_table(
            arts.get("holdings_detail", pd.DataFrame()),
            limit=200,
        ),
        "trades_detail": _records_for_table(
            arts.get("trades", pd.DataFrame()),
            limit=200,
        ),
        "costs_detail": _records_for_table(
            arts.get("costs", pd.DataFrame()),
            limit=100,
        ),
        "nav_fig_json": nav_fig_json,
        "is_running": task.get("status") in (
            bt_store.STATUS_PENDING,
            bt_store.STATUS_WAITING_FOR_DATA,
            bt_store.STATUS_RUNNING,
        ),
        "universes": _enabled_universes(),
    })


# ---------- API：回测 ----------

@router_v2.get("/api/backtests")
def api_list_backtests():
    return JSONResponse(_sanitize({"tasks": bt_store.list_tasks()}))


@router_v2.get("/api/backtests/{tid}")
def api_get_backtest(tid: UUID):
    tid = str(tid)
    task = bt_store.load_task(tid)
    if task is None:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return JSONResponse(_sanitize(task))


@router_v2.get("/api/backtests/{tid}/status")
def api_backtest_status(tid: UUID):
    """轻量状态轮询端点，仅返回状态、耗时、简要错误。"""
    tid = str(tid)
    task = bt_store.load_task(tid)
    if task is None:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return JSONResponse(_sanitize({
        "id": task.get("id"),
        "status": task.get("status"),
        "created_at": task.get("created_at"),
        "status_changed_at": task.get("status_changed_at"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "duration_sec": task.get("duration_sec"),
        "timing": _backtest_timing_view(task),
        "error": task.get("error"),
        "metrics": task.get("metrics"),
        "data_request_id": task.get("data_request_id"),
        "waiting_for_data": (
            (task.get("diagnostics") or {}).get("waiting_for_data")
        ),
    }))


@router_v2.post("/api/backtests")
def api_create_backtest(payload: dict = Body(...)):
    strategy_id = payload.get("strategy_id")
    universe_raw = (payload.get("universe") or "").strip()
    start = (payload.get("start") or str(CONFIG.date_range.start)).strip()
    end = (payload.get("end") or str(CONFIG.date_range.end)).strip()
    name = (payload.get("name") or "").strip() or None

    if not strategy_id:
        raise HTTPException(status_code=400, detail="strategy_id 必填")
    if not universe_raw:
        raise HTTPException(status_code=400, detail="universe 必填")

    try:
        strategy_id = canonical_uuid(strategy_id, label="strategy_id")
    except InvalidResourceId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    strategy = load_strategy(strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail=f"策略不存在: {strategy_id}")

    # 校验：策略里的因子仍然注册
    try:
        assert_valid_factor_ids([c.factor_id for c in strategy.components])
    except FactorLibraryError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # universe 两种形态：
    #   1) 注册的研究股票池："SP500" / "NASDAQ100" / "MAG7"
    #   2) 用户目标股票池："watchlist:<uuid>"
    watchlist_snapshot: dict | None = None
    universe_for_task: str
    if universe_raw.lower().startswith("watchlist:"):
        try:
            wid = canonical_uuid(
                universe_raw.split(":", 1)[1].strip(),
                label="watchlist_id",
            )
        except InvalidResourceId as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        from src.watchlists import load_watchlist
        wl = load_watchlist(wid)
        if wl is None:
            raise HTTPException(status_code=404, detail=f"Watchlist 不存在: {wid}")
        try:
            wl.validate()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Watchlist 非法: {e}")
        watchlist_snapshot = wl.to_dict()
        universe_for_task = f"watchlist:{wl.id}"
    else:
        uni_upper = universe_raw.upper()
        enabled = _enabled_universes()
        if uni_upper not in enabled:
            raise HTTPException(
                status_code=400,
                detail=f"未知股票池 {uni_upper}，可选: {enabled}",
            )
        universe_for_task = uni_upper

    # 解析日期
    try:
        resolved_start, resolved_end, _ = resolve_date_range(start, end)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"日期范围非法: {e}")

    n_groups = int(CONFIG.backtest.n_groups)
    rebalance_days = int(CONFIG.backtest.rebalance_days)
    rebalance_mode = str(getattr(CONFIG.backtest, "rebalance_mode", "every_n_days"))

    # 解析 execution（用户在新建回测页可覆盖默认值）
    execution: dict | None = None
    raw_exec = payload.get("execution") or {}
    if raw_exec:
        timing = str(raw_exec.get("timing") or "").lower().strip()
        if timing and timing != "next_open":
            raise HTTPException(
                status_code=400,
                detail="execution.timing 只允许 next_open；同日收盘成交已禁用",
            )
        try:
            portfolio_value = (
                float(raw_exec["portfolio_value"])
                if "portfolio_value" in raw_exec
                and raw_exec["portfolio_value"] is not None
                else None
            )
            slip = (
                float(raw_exec["slippage_bps"])
                if "slippage_bps" in raw_exec and raw_exec["slippage_bps"] is not None
                else None
            )
            comm = (
                float(raw_exec["commission_bps"])
                if "commission_bps" in raw_exec and raw_exec["commission_bps"] is not None
                else None
            )
        except (TypeError, ValueError) as e:
            raise HTTPException(
                status_code=400,
                detail=f"execution 数字解析失败：{e}",
            )
        if portfolio_value is not None and (
            not math.isfinite(portfolio_value)
            or portfolio_value <= 0
            or portfolio_value > 1_000_000_000_000
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "portfolio_value 超出合理范围 "
                    f"(0, 1,000,000,000,000]: {portfolio_value}"
                ),
            )
        if slip is not None and (
            not math.isfinite(slip) or slip < 0 or slip > 1000
        ):
            raise HTTPException(
                status_code=400, detail=f"slippage_bps 超出合理范围 [0, 1000]: {slip}",
            )
        if comm is not None and (
            not math.isfinite(comm) or comm < 0 or comm > 1000
        ):
            raise HTTPException(
                status_code=400, detail=f"commission_bps 超出合理范围 [0, 1000]: {comm}",
            )
        fee_model = str(raw_exec.get("fee_model") or "").lower().strip()
        slippage_model = str(raw_exec.get("slippage_model") or "").lower().strip()
        if fee_model and fee_model not in {
            "simple_bps", "ibkr_us_pro_fixed", "ibkr_us_pro_tiered", "ibkr_us_lite",
        }:
            raise HTTPException(
                status_code=400, detail=f"fee_model 非法：{fee_model}",
            )
        if slippage_model and slippage_model not in {
            "none", "constant_bps", "simple_bps", "volume_share",
        }:
            raise HTTPException(
                status_code=400, detail=f"slippage_model 非法：{slippage_model}",
            )
        execution = {
            "timing": timing or None,
            "portfolio_value": portfolio_value,
            "fee_model": fee_model or None,
            "slippage_model": slippage_model or None,
            "slippage_bps": slip,
            "commission_bps": comm,
        }
        # 把 None 字段去掉，让 runner 用 CONFIG 默认兜底
        execution = {k: v for k, v in execution.items() if v is not None}
        if not execution:
            execution = None

    try:
        execution = resolve_execution_config(execution or {})
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"execution 配置非法：{exc}",
        ) from exc

    task = bt_store.create_task(
        strategy=strategy,
        universe=universe_for_task,
        start=start, end=end,
        resolved_start=resolved_start, resolved_end=resolved_end,
        n_groups=n_groups,
        rebalance_mode=rebalance_mode,
        rebalance_days=rebalance_days,
        top_group=n_groups,   # 固定取 Top = 最高分组（Q{n_groups}）
        name=name,
        watchlist_snapshot=watchlist_snapshot,
        execution=execution,
        research_evidence_snapshot=_strategy_research_snapshot(strategy),
        target_universe_snapshot=_target_universe_snapshot(
            universe_for_task,
            watchlist_snapshot,
        ),
        risk_config=_risk_config_snapshot(),
    )
    get_runner().submit(task["id"])
    return JSONResponse(_sanitize(task), status_code=201)


@router_v2.delete("/api/backtests/{tid}")
def api_delete_backtest(tid: UUID):
    tid = str(tid)
    task = bt_store.load_task(tid)
    if task is None:
        raise HTTPException(status_code=404, detail="Backtest not found")
    if task.get("status") in (
        bt_store.STATUS_PENDING,
        bt_store.STATUS_RUNNING,
    ):
        raise HTTPException(
            status_code=409,
            detail="运行中的回测不能删除，请等待任务结束",
        )
    ok = bt_store.delete_task(tid)
    if not ok:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return JSONResponse({"deleted": tid})


# ===============================================================
# 模拟盘（内部 FMP 驱动模拟）
# ===============================================================

def _format_paper_account_summary(a: dict[str, Any]) -> dict[str, Any]:
    initial = a.get("initial_cash")
    equity = a.get("last_equity")
    pnl = None
    ret = None
    try:
        if initial is not None and equity is not None and float(initial) > 0:
            pnl = float(equity) - float(initial)
            ret = float(equity) / float(initial) - 1.0
    except (TypeError, ValueError):
        pass
    return {
        **a,
        "cash_fmt": _format_money(a.get("cash")),
        "initial_cash_fmt": _format_money(initial),
        "last_equity_fmt": _format_money(equity),
        "pnl": pnl,
        "pnl_fmt": _format_money(pnl),
        "return_fmt": _format_pct(ret),
    }


def _records_for_table(df: pd.DataFrame, limit: int = 50) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    view = df.copy()
    return _sanitize(view.tail(limit).iloc[::-1].to_dict(orient="records"))


@router_v2.get("/paper", response_class=HTMLResponse)
def paper_accounts_page(request: Request):
    items = [_format_paper_account_summary(a) for a in list_paper_accounts()]
    return templates.TemplateResponse(request, "paper_list.html", {
        "title": "模拟盘",
        "items": items,
        "universes": _enabled_universes(),
    })


@router_v2.get("/paper/new", response_class=HTMLResponse)
def paper_new_page(
    request: Request,
    strategy_id: str | None = Query(None),
    watchlist_id: str | None = Query(None),
):
    from src.watchlists import list_watchlists
    strategies = list_strategies()
    default_exec = {
        "timing": "next_open",
        "fee_model": str(getattr(CONFIG.backtest.execution, "fee_model", "ibkr_us_pro_fixed")),
        "slippage_model": str(getattr(CONFIG.backtest.execution, "slippage_model", "volume_share")),
        "slippage_bps": float(getattr(CONFIG.backtest.execution, "slippage_bps", 5)),
        "commission_bps": float(getattr(CONFIG.backtest.execution, "commission_bps", 2)),
        "min_order_value": 25.0,
    }
    return templates.TemplateResponse(request, "paper_new.html", {
        "title": "新建模拟盘",
        "strategies": strategies,
        "strategy_research": _strategy_research_map(strategies),
        "universes": _enabled_universes(),
        "watchlists": list_watchlists(),
        "preselect_strategy_id": strategy_id,
        "preselect_watchlist_id": watchlist_id,
        "default_initial_cash": 100000,
        "default_exec": default_exec,
        "default_n_groups": int(CONFIG.backtest.n_groups),
        "default_top_group": int(CONFIG.backtest.n_groups),
        "default_rebalance_mode": str(getattr(CONFIG.backtest, "rebalance_mode", "month_end")),
    })


@router_v2.get("/paper/{aid}", response_class=HTMLResponse)
def paper_detail_page(request: Request, aid: UUID):
    aid = str(aid)
    account = load_paper_account(aid)
    if account is None:
        raise HTTPException(status_code=404, detail="Paper account not found")
    arts = load_account_artifacts(aid)
    positions = arts.get("positions", pd.DataFrame())
    orders = arts.get("orders", pd.DataFrame())
    fills = arts.get("fills", pd.DataFrame())
    targets = arts.get("target_weights", pd.DataFrame())
    runs = arts.get("runs", pd.DataFrame())
    equity_curve = arts.get("equity_curve", pd.DataFrame())

    equity_fig_json = None
    if equity_curve is not None and not equity_curve.empty and "equity" in equity_curve.columns:
        curve = equity_curve.copy()
        curve["date"] = pd.to_datetime(curve["date"])
        curve = curve.sort_values("date")
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=curve["date"], y=curve["equity"],
            mode="lines+markers", name="账户权益",
            line=dict(color="#42A5F5", width=2),
        ))
        fig.update_layout(
            title=dict(text="模拟盘权益曲线", font=dict(color=_PLOT_TEXT, size=15)),
            paper_bgcolor=_PLOT_BG, plot_bgcolor=_PLOT_PANEL,
            font=dict(color=_PLOT_TEXT),
            margin=dict(l=40, r=40, t=60, b=40),
            height=380, hovermode="x unified",
        )
        fig.update_xaxes(gridcolor=_PLOT_GRID)
        fig.update_yaxes(gridcolor=_PLOT_GRID, title_text="权益（USD）")
        equity_fig_json = fig_to_json(fig)

    summary = _format_paper_account_summary(account)
    open_orders = []
    if orders is not None and not orders.empty and "status" in orders.columns:
        open_orders = _records_for_table(
            orders[orders["status"].astype(str) == "pending"],
            limit=50,
        )
    return templates.TemplateResponse(request, "paper_detail.html", {
        "title": f"模拟盘 · {account.get('name') or aid[:8]}",
        "account": account,
        "research_snapshot": account.get("research_evidence_snapshot") or {},
        "target_snapshot": account.get("target_universe_snapshot") or {},
        "data_contract": account.get("data_contract") or {},
        "risk_config": account.get("risk_config") or {},
        "summary": summary,
        "positions": _records_for_table(positions, limit=100),
        "open_orders": open_orders,
        "orders": _records_for_table(orders, limit=80),
        "fills": _records_for_table(fills, limit=80),
        "targets": _records_for_table(targets, limit=50),
        "runs": _records_for_table(runs, limit=20),
        "equity_fig_json": equity_fig_json,
        "universes": _enabled_universes(),
    })


@router_v2.get("/api/paper/accounts")
def api_list_paper_accounts():
    return JSONResponse(_sanitize({"accounts": list_paper_accounts()}))


@router_v2.get("/api/paper/accounts/{aid}")
def api_get_paper_account(aid: UUID):
    aid = str(aid)
    account = load_paper_account(aid)
    if account is None:
        raise HTTPException(status_code=404, detail="Paper account not found")
    return JSONResponse(_sanitize(account))


@router_v2.post("/api/paper/accounts")
def api_create_paper_account(payload: dict = Body(...)):
    strategy_id = str(payload.get("strategy_id") or "").strip()
    universe_raw = str(payload.get("universe") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not strategy_id:
        raise HTTPException(status_code=400, detail="strategy_id 必填")
    if not universe_raw:
        raise HTTPException(status_code=400, detail="universe 必填")
    try:
        strategy_id = canonical_uuid(strategy_id, label="strategy_id")
    except InvalidResourceId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    strategy = load_strategy(strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail=f"策略不存在: {strategy_id}")

    watchlist_snapshot: dict[str, Any] | None = None
    if universe_raw.lower().startswith("watchlist:"):
        try:
            wid = canonical_uuid(
                universe_raw.split(":", 1)[1].strip(),
                label="watchlist_id",
            )
        except InvalidResourceId as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        from src.watchlists import load_watchlist
        wl = load_watchlist(wid)
        if wl is None:
            raise HTTPException(status_code=404, detail=f"股票池不存在: {wid}")
        try:
            wl.validate()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"股票池非法: {e}")
        watchlist_snapshot = wl.to_dict()
        universe = f"watchlist:{wl.id}"
    else:
        universe = universe_raw.upper()
        enabled = _enabled_universes()
        if universe not in enabled:
            raise HTTPException(status_code=400, detail=f"未知股票池 {universe}，可选: {enabled}")

    try:
        account = create_account_payload(
            name=name or f"{strategy.name} 模拟盘",
            strategy=strategy,
            universe=universe,
            watchlist_snapshot=watchlist_snapshot,
            initial_cash=float(payload.get("initial_cash", 100000)),
            n_groups=int(payload.get("n_groups") or CONFIG.backtest.n_groups),
            top_group=int(payload.get("top_group") or CONFIG.backtest.n_groups),
            rebalance_mode=str(payload.get("rebalance_mode") or getattr(CONFIG.backtest, "rebalance_mode", "month_end")),
            execution=payload.get("execution") or {},
            research_evidence_snapshot=_strategy_research_snapshot(strategy),
            target_universe_snapshot=_target_universe_snapshot(
                universe,
                watchlist_snapshot,
            ),
            risk_config=_risk_config_snapshot(),
        )
        if watchlist_snapshot is not None:
            request = _queue_watchlist_market_data(
                watchlist_snapshot,
                consumer_kind="paper_account",
                consumer_id=str(account["id"]),
            )
            account["data_request_id"] = request.request_id
            account["diagnostics"] = {
                "waiting_for_data": {
                    "request_id": request.request_id,
                    "data_universe": request.data_universe,
                }
            }
        create_paper_account(account)
    except (PaperTradingValidationError, FactorLibraryError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(_sanitize(account), status_code=201)


@router_v2.post("/api/paper/accounts/{aid}/run")
def api_run_paper_account(aid: UUID, payload: dict | None = Body(None)):
    aid = str(aid)
    asof = None
    if payload:
        raw_asof = payload.get("asof")
        asof = str(raw_asof).strip() if raw_asof else None
    try:
        result = run_account_once(aid, asof=asof)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Paper account not found")
    except MarketDataNotReadyError as e:
        raise HTTPException(
            status_code=409,
            detail=f"模拟盘正在等待统一行情或因子发布：{e}",
        ) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(_sanitize(result))


@router_v2.delete("/api/paper/accounts/{aid}")
def api_delete_paper_account(aid: UUID):
    aid = str(aid)
    ok = delete_paper_account(aid)
    if not ok:
        raise HTTPException(status_code=404, detail="Paper account not found")
    return JSONResponse({"deleted": aid})


# ===============================================================
# 目标股票池（内部存储类型仍叫 Watchlist）
# ===============================================================

from src.watchlists import (  # noqa: E402
    WatchlistDefinition,
    WatchlistItem,
    create_watchlist,
    delete_watchlist,
    list_watchlists,
    load_ranking_activity,
    load_watchlist,
    update_watchlist,
)


def _target_universe_rows() -> list[dict[str, Any]]:
    """Build the Target Universe operational view without changing domain IDs."""
    catalog = MarketDataCatalog()
    expected = latest_publishable_xnys_session(
        delay_minutes=int(getattr(CONFIG.data.foundation, "close_delay_minutes", 120))
    ).date().isoformat()
    backtests = bt_store.list_tasks()
    accounts = list_paper_accounts()
    rows: list[dict[str, Any]] = []
    for summary in list_watchlists():
        wid = str(summary["id"])
        watchlist = load_watchlist(wid)
        if watchlist is None:
            continue
        snapshot = watchlist.to_dict()
        data_universe = watchlist_snapshot_data_universe(snapshot)
        version = catalog.latest_version(data_universe)
        complete_hashes = bool(
            version
            and version.checksum_sha256
            and version.universe_checksum_sha256
            and version.membership_checksum_sha256
            and version.manifest_checksum_sha256
        )
        if version is None:
            data_status = "MISSING"
        elif not complete_hashes:
            data_status = "INVALID"
        elif version.target_session.isoformat() != expected:
            data_status = "STALE"
        else:
            data_status = "PUBLISHED"
        key = f"watchlist:{wid}"
        activity = load_ranking_activity(wid) or {}
        rows.append(
            {
                **summary,
                "universe_type": "TARGET",
                "data_universe": data_universe,
                "data_status": data_status,
                "latest_session": (
                    version.target_session.isoformat() if version else None
                ),
                "dataset_version_id": version.version_id if version else None,
                "latest_ranking_at": activity.get("ranked_at"),
                "latest_ranking_date": activity.get("decision_date"),
                "backtest_count": sum(
                    1 for task in backtests if task.get("universe") == key
                ),
                "paper_count": sum(
                    1 for account in accounts if account.get("universe") == key
                ),
            }
        )
    return rows


@router_v2.get("/watchlists", response_class=HTMLResponse)
def watchlists_list_page(request: Request):
    items = _target_universe_rows()
    return templates.TemplateResponse(request, "watchlist_list.html", {
        "title": "目标股票池",
        "watchlists": items,
    })


@router_v2.get("/watchlists/new", response_class=HTMLResponse)
def watchlists_new_page(request: Request):
    return templates.TemplateResponse(request, "watchlist_edit.html", {
        "title": "新建目标股票池",
        "mode": "new",
        "watchlist": None,
    })


@router_v2.get("/watchlists/{wid}", response_class=HTMLResponse)
def watchlists_edit_page(request: Request, wid: UUID):
    wid = str(wid)
    wl = load_watchlist(wid)
    if wl is None:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    return templates.TemplateResponse(request, "watchlist_edit.html", {
        "title": f"编辑目标股票池 · {wl.name}",
        "mode": "edit",
        "watchlist": wl.to_dict(),
    })


# ---------------- JSON API ----------------

@router_v2.get("/api/watchlists")
def api_list_watchlists():
    return JSONResponse(_sanitize(_target_universe_rows()))


@router_v2.get("/api/watchlists/{wid}")
def api_get_watchlist(wid: UUID):
    wid = str(wid)
    wl = load_watchlist(wid)
    if wl is None:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    operational = next(
        (item for item in _target_universe_rows() if item["id"] == wid),
        {},
    )
    return JSONResponse(_sanitize({**wl.to_dict(), **operational}))


def _parse_payload_items(raw_items: Any) -> list[WatchlistItem]:
    """把 JSON 里的 items 数组解析为 WatchlistItem 列表（容错 + 去重）。"""
    if not isinstance(raw_items, list):
        raise HTTPException(status_code=400, detail="items 必须是数组")
    items: list[WatchlistItem] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="items 元素必须是对象")
        try:
            ticker = canonical_ticker(raw.get("ticker"))
        except InvalidResourceId as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if ticker in seen:
            raise HTTPException(status_code=400, detail=f"ticker 重复: {ticker}")
        seen.add(ticker)
        try:
            weight = float(raw.get("weight") if raw.get("weight") is not None else 0.0)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"{ticker} 的权重不是数字: {raw.get('weight')!r}",
            )
        if not math.isfinite(weight) or weight < 0:
            raise HTTPException(
                status_code=400,
                detail=f"{ticker} 的权重必须是有限非负数: {weight}",
            )
        items.append(WatchlistItem(
            ticker=ticker,
            weight=weight,
            name=str(raw.get("name") or ""),
        ))
    if not items:
        raise HTTPException(status_code=400, detail="items 为空")
    return items


@router_v2.post("/api/watchlists")
def api_create_watchlist(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    description = (payload.get("description") or "").strip()
    items = _parse_payload_items(payload.get("items"))

    # 权重策略：若 normalize=true 则归一化；若 equal_weight=true 则等权；否则原样保留
    normalize = bool(payload.get("normalize"))
    equal_weight = bool(payload.get("equal_weight"))

    wl = WatchlistDefinition.new(name=name, description=description, items=items)
    if equal_weight:
        wl.set_equal_weights()
    elif normalize:
        wl.normalize_weights()

    try:
        wl = create_watchlist(wl)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    request = _queue_watchlist_market_data(
        wl.to_dict(),
        consumer_kind="watchlist",
        consumer_id=wl.id,
    )
    response = wl.to_dict()
    response["data_request_id"] = request.request_id
    return JSONResponse(_sanitize(response), status_code=201)


@router_v2.put("/api/watchlists/{wid}")
def api_update_watchlist(wid: UUID, payload: dict = Body(...)):
    wid = str(wid)
    existing = load_watchlist(wid)
    if existing is None:
        raise HTTPException(status_code=404, detail="Watchlist not found")

    # 允许只改部分字段；items 必填（编辑页总是把完整列表发过来）
    name = (payload.get("name") or existing.name).strip()
    description = (payload.get("description") or existing.description).strip()
    items = _parse_payload_items(payload.get("items"))

    normalize = bool(payload.get("normalize"))
    equal_weight = bool(payload.get("equal_weight"))

    wl = WatchlistDefinition(
        id=existing.id,
        name=name,
        description=description,
        items=items,
        created_at=existing.created_at,
    )
    if equal_weight:
        wl.set_equal_weights()
    elif normalize:
        wl.normalize_weights()

    try:
        wl = update_watchlist(wl)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    request = _queue_watchlist_market_data(
        wl.to_dict(),
        consumer_kind="watchlist",
        consumer_id=wl.id,
    )
    response = wl.to_dict()
    response["data_request_id"] = request.request_id
    return JSONResponse(_sanitize(response))


@router_v2.delete("/api/watchlists/{wid}")
def api_delete_watchlist(wid: UUID):
    wid = str(wid)
    ok = delete_watchlist(wid)
    if not ok:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    return JSONResponse({"deleted": wid})


# ---------------- FMP 辅助端点 ----------------

@router_v2.get("/api/symbol_search")
def api_symbol_search(
    q: str = Query(..., min_length=1, max_length=64),
    limit: int = Query(20, ge=1, le=50),
):
    """调 FMP 搜索，用于 Watchlist 编辑页的实时下拉。"""
    from src.data.fmp import search_symbol
    try:
        results = search_symbol(q, limit=limit)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"FMP search 失败: {e}")
    return JSONResponse(results)


@router_v2.get("/api/ticker_verify")
def api_ticker_verify(ticker: str = Query(..., min_length=1, max_length=16)):
    """
    校验 ticker 是否真实存在（调 FMP profile/quote）。
    存在返回 {exists: true, ticker, name, exchange, currency}
    不存在返回 {exists: false, ticker}
    """
    from src.data.fmp import verify_ticker
    try:
        info = verify_ticker(ticker)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"FMP verify 失败: {e}")
    if info is None:
        return JSONResponse({"exists": False, "ticker": ticker.upper()})
    return JSONResponse({"exists": True, **info})


__all__ = ["router_v2", "templates"]
