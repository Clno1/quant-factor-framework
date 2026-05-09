"""
V2 路由：因子库 / 策略库 / 回测 三件套。

页面：
  GET  /factors                          因子库（只读列表）
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

import math
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from src.backtest import store as bt_store
from src.backtest.runner import get_runner
from src.config import CONFIG
from src.factors import assert_valid_factor_ids, get_factor_catalog, list_factor_ids
from src.factors.library import FactorLibraryError
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
from src.webapp.results_store import list_universes
from src.visualization.plots_plotly import fig_to_json
import plotly.graph_objects as go


_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

router_v2 = APIRouter()


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


def _enabled_universes() -> list[str]:
    """配置 + 已有产物的并集。"""
    try:
        cfg_list = [str(u).upper() for u in (CONFIG.universes.enabled or [])]
    except Exception:
        cfg_list = []
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


# ---------------------------------------------------------------
# 因子库页
# ---------------------------------------------------------------

@router_v2.get("/factors", response_class=HTMLResponse)
def factors_page(request: Request):
    try:
        factors = _factor_catalog_payload()
    except FactorLibraryError as e:
        raise HTTPException(status_code=500, detail=str(e))
    # 按分类分组
    categories: dict[str, list[dict]] = {}
    for f in factors:
        categories.setdefault(f["category"], []).append(f)
    return templates.TemplateResponse(request, "factor_library.html", {
        "title": "因子库",
        "categories": categories,
        "factors": factors,
        "total": len(factors),
        "universes": _enabled_universes(),
    })


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
def strategy_detail_page(request: Request, sid: str):
    s = load_strategy(sid)
    if s is None:
        raise HTTPException(status_code=404, detail=f"Strategy not found: {sid}")
    catalog = get_factor_catalog()
    components = []
    for c in s.components:
        entry = catalog.get(c.factor_id)
        components.append({
            "factor_id": c.factor_id,
            "display_name": entry.display_name if entry else c.factor_id,
            "category": entry.category if entry else "—",
            "weight": c.weight,
        })
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


# ---------- API：策略 ----------

@router_v2.get("/api/strategies")
def api_list_strategies():
    return JSONResponse(_sanitize({"strategies": list_strategies()}))


@router_v2.get("/api/strategies/{sid}")
def api_get_strategy(sid: str):
    s = load_strategy(sid)
    if s is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return JSONResponse(_sanitize(s.to_dict()))


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
def api_delete_strategy(sid: str):
    ok = delete_strategy(sid)
    if not ok:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return JSONResponse({"deleted": sid})


# ---------------------------------------------------------------
# 回测 Tab
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
):
    from src.watchlists import list_watchlists
    strategies = list_strategies()
    universes = _enabled_universes()
    watchlists = list_watchlists()
    default_start = str(CONFIG.date_range.start)
    default_end = str(CONFIG.date_range.end)
    return templates.TemplateResponse(request, "backtest_new.html", {
        "title": "新建回测",
        "strategies": strategies,
        "universes": universes,
        "watchlists": watchlists,
        "preselect_strategy_id": strategy_id,
        "preselect_watchlist_id": watchlist_id,
        "default_start": default_start,
        "default_end": default_end,
    })


@router_v2.get("/backtests/{tid}", response_class=HTMLResponse)
def backtest_detail_page(request: Request, tid: str):
    task = bt_store.load_task(tid)
    if task is None:
        raise HTTPException(status_code=404, detail="Backtest not found")

    # 构造 NAV 图（仅成功态）
    nav_fig_json = None
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
        components_rows.append({
            "factor_id": fid,
            "display_name": entry.display_name if entry else fid,
            "category": entry.category if entry else "—",
            "weight": c.get("weight", 0.0),
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
        "N_days":    metrics_raw.get("N_days", "—"),
    }

    return templates.TemplateResponse(request, "backtest_detail.html", {
        "title": f"回测 · {task.get('name') or tid[:8]}",
        "task": task,
        "metrics_fmt": metrics_fmt,
        "components_rows": components_rows,
        "nav_fig_json": nav_fig_json,
        "is_running": task.get("status") in (bt_store.STATUS_PENDING, bt_store.STATUS_RUNNING),
        "universes": _enabled_universes(),
    })


# ---------- API：回测 ----------

@router_v2.get("/api/backtests")
def api_list_backtests():
    return JSONResponse(_sanitize({"tasks": bt_store.list_tasks()}))


@router_v2.get("/api/backtests/{tid}")
def api_get_backtest(tid: str):
    task = bt_store.load_task(tid)
    if task is None:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return JSONResponse(_sanitize(task))


@router_v2.get("/api/backtests/{tid}/status")
def api_backtest_status(tid: str):
    """轻量状态轮询端点，仅返回状态、耗时、简要错误。"""
    task = bt_store.load_task(tid)
    if task is None:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return JSONResponse(_sanitize({
        "id": task.get("id"),
        "status": task.get("status"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "duration_sec": task.get("duration_sec"),
        "error": task.get("error"),
        "metrics": task.get("metrics"),
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

    strategy = load_strategy(strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail=f"策略不存在: {strategy_id}")

    # 校验：策略里的因子仍然注册
    try:
        assert_valid_factor_ids([c.factor_id for c in strategy.components])
    except FactorLibraryError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # universe 两种形态：
    #   1) 预设股票池："SP500" / "MAG7"（大写）
    #   2) 自定义 watchlist："watchlist:<uuid>"
    watchlist_snapshot: dict | None = None
    universe_for_task: str
    if universe_raw.lower().startswith("watchlist:"):
        wid = universe_raw.split(":", 1)[1].strip()
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

    task = bt_store.create_task(
        strategy=strategy,
        universe=universe_for_task,
        start=start, end=end,
        resolved_start=resolved_start, resolved_end=resolved_end,
        n_groups=n_groups,
        rebalance_days=rebalance_days,
        top_group=n_groups,   # 固定取 Top = 最高分组（Q{n_groups}）
        name=name,
        watchlist_snapshot=watchlist_snapshot,
    )
    get_runner().submit(task["id"])
    return JSONResponse(_sanitize(task), status_code=201)


@router_v2.delete("/api/backtests/{tid}")
def api_delete_backtest(tid: str):
    ok = bt_store.delete_task(tid)
    if not ok:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return JSONResponse({"deleted": tid})


# ===============================================================
# Watchlist Tab（自定义股票组）
# ===============================================================

from src.watchlists import (  # noqa: E402
    WatchlistDefinition,
    WatchlistItem,
    create_watchlist,
    delete_watchlist,
    list_watchlists,
    load_watchlist,
    update_watchlist,
)


@router_v2.get("/watchlists", response_class=HTMLResponse)
def watchlists_list_page(request: Request):
    items = list_watchlists()
    return templates.TemplateResponse(request, "watchlist_list.html", {
        "title": "股票组",
        "watchlists": items,
    })


@router_v2.get("/watchlists/new", response_class=HTMLResponse)
def watchlists_new_page(request: Request):
    return templates.TemplateResponse(request, "watchlist_edit.html", {
        "title": "新建股票组",
        "mode": "new",
        "watchlist": None,
    })


@router_v2.get("/watchlists/{wid}", response_class=HTMLResponse)
def watchlists_edit_page(request: Request, wid: str):
    wl = load_watchlist(wid)
    if wl is None:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    return templates.TemplateResponse(request, "watchlist_edit.html", {
        "title": f"编辑股票组 · {wl.name}",
        "mode": "edit",
        "watchlist": wl.to_dict(),
    })


# ---------------- JSON API ----------------

@router_v2.get("/api/watchlists")
def api_list_watchlists():
    return JSONResponse(_sanitize(list_watchlists()))


@router_v2.get("/api/watchlists/{wid}")
def api_get_watchlist(wid: str):
    wl = load_watchlist(wid)
    if wl is None:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    return JSONResponse(_sanitize(wl.to_dict()))


def _parse_payload_items(raw_items: Any) -> list[WatchlistItem]:
    """把 JSON 里的 items 数组解析为 WatchlistItem 列表（容错 + 去重）。"""
    if not isinstance(raw_items, list):
        raise HTTPException(status_code=400, detail="items 必须是数组")
    items: list[WatchlistItem] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="items 元素必须是对象")
        ticker = str(raw.get("ticker") or "").strip().upper()
        if not ticker:
            raise HTTPException(status_code=400, detail="存在空 ticker")
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
        if weight < 0:
            raise HTTPException(
                status_code=400,
                detail=f"{ticker} 的权重不能为负: {weight}",
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
    return JSONResponse(_sanitize(wl.to_dict()), status_code=201)


@router_v2.put("/api/watchlists/{wid}")
def api_update_watchlist(wid: str, payload: dict = Body(...)):
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
    return JSONResponse(_sanitize(wl.to_dict()))


@router_v2.delete("/api/watchlists/{wid}")
def api_delete_watchlist(wid: str):
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
