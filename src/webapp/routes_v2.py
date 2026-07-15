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
import threading
from typing import Any

import pandas as pd
from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from src.backtest import store as bt_store
from src.backtest.runner import get_runner
from src.breakouts import (
    BreakoutFilters,
    build_intraday_snapshot,
    evaluate_daily_setup,
    load_intraday_1min,
    load_market_regime,
    refresh_daily_frame,
    scan_breakouts,
)
from src.breakouts.scanner import load_daily_frame
from src.breakouts.scan_cache import load_scan_cache, save_scan_cache
from src.config import CONFIG, PROJECT_ROOT
from src.factors import assert_valid_factor_ids, get_factor_catalog, list_factor_ids
from src.factors.library import FactorLibraryError
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

_MOMENTUM_UNIVERSE_LABELS = {
    "US_ACTIVE": "美股活跃标的 · 股票 + ETF · NASDAQ / NYSE / AMEX",
    "SP500": "S&P 500",
    "MAG7": "科技龙头 · MAG7",
}
_MOMENTUM_SCAN_LOCK = threading.Lock()


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


def _strategy_component_rows(strategy: StrategyDefinition) -> list[dict[str, Any]]:
    catalog = get_factor_catalog()
    rows: list[dict[str, Any]] = []
    for c in strategy.components:
        entry = catalog.get(c.factor_id)
        rows.append({
            "factor_id": c.factor_id,
            "display_name": entry.display_name if entry else c.factor_id,
            "category": entry.category if entry else "—",
            "weight": c.weight,
        })
    return rows


def _default_universe() -> str:
    enabled = _enabled_universes()
    try:
        configured = str(CONFIG.universes.default).upper()
        if configured in enabled:
            return configured
    except Exception:  # noqa: BLE001
        pass
    return enabled[0] if enabled else "SP500"


def _normalize_ranking_universe(raw: str | None) -> str:
    value = str(raw or "").strip()
    if not value:
        return _default_universe()
    if value.lower().startswith("watchlist:"):
        return f"watchlist:{value.split(':', 1)[1].strip()}"
    return value.upper()


def _resolve_watchlist_snapshot(universe: str) -> tuple[dict[str, Any] | None, str]:
    if not universe.lower().startswith("watchlist:"):
        return None, universe
    wid = universe.split(":", 1)[1].strip()
    from src.watchlists import load_watchlist
    wl = load_watchlist(wid)
    if wl is None:
        raise HTTPException(status_code=404, detail=f"股票池不存在: {wid}")
    wl.validate()
    return wl.to_dict(), wl.name


def _ranking_universe_options() -> tuple[list[str], list[dict[str, Any]]]:
    from src.watchlists import list_watchlists
    return _enabled_universes(), list_watchlists()


def _momentum_universe_options() -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    from src.watchlists import list_watchlists

    values = ["US_ACTIVE"] + [value for value in _enabled_universes() if value != "US_ACTIVE"]
    options = [
        {"value": value, "label": _MOMENTUM_UNIVERSE_LABELS.get(value, value)}
        for value in dict.fromkeys(values)
    ]
    return options, list_watchlists()


def _normalize_momentum_universe(raw: str | None) -> str:
    return _normalize_ranking_universe(raw) if str(raw or "").strip() else "US_ACTIVE"


def _breakout_universe_context(raw: str | None) -> dict[str, Any]:
    """Resolve a preset universe or Watchlist into tickers and display metadata."""
    universe = _normalize_momentum_universe(raw)
    watchlist_snapshot, label = _resolve_watchlist_snapshot(universe)
    if watchlist_snapshot is not None:
        items = watchlist_snapshot.get("items") or []
        tickers = [str(item.get("ticker") or "").upper() for item in items]
        names = {
            str(item.get("ticker") or "").upper(): str(item.get("name") or "")
            for item in items
        }
        return {
            "universe": universe,
            "label": label,
            "tickers": [ticker for ticker in tickers if ticker],
            "names": names,
            "sectors": {},
            "current_dollar_volume": {},
            "refresh_daily": True,
        }

    if universe not in _enabled_universes() and universe != "US_ACTIVE":
        raise HTTPException(status_code=400, detail=f"未知股票池: {universe}")

    if universe == "US_ACTIVE":
        from src.data.universe import get_universe
        meta = get_universe(name=universe).copy()
        tickers = meta["ticker"].astype(str).str.upper().tolist()
    else:
        from src.data.cleaner import load_wide_tables
        try:
            wide = load_wide_tables(universe)
            tickers = [str(t).upper() for t in wide["close"].columns]
        except Exception:
            tickers = []
        meta = pd.DataFrame()
        universe_cache = PROJECT_ROOT / "data" / "raw" / "universe" / f"{universe.lower()}.parquet"
        if universe_cache.exists():
            try:
                meta = pd.read_parquet(universe_cache)
            except Exception:
                meta = pd.DataFrame()
        elif universe == "MAG7":
            from src.data.universe import get_universe
            meta = get_universe(name=universe).copy()
    if not meta.empty and "ticker" in meta.columns:
        meta["ticker"] = meta["ticker"].astype(str).str.upper()
        if not tickers:
            tickers = meta["ticker"].tolist()
        names = meta.set_index("ticker").get("name", pd.Series(dtype="object")).fillna("").to_dict()
        sectors = meta.set_index("ticker").get("sector", pd.Series(dtype="object")).fillna("").to_dict()
        if "current_dollar_volume" in meta.columns:
            current_dollar_volume = (
                pd.to_numeric(meta.set_index("ticker")["current_dollar_volume"], errors="coerce")
                .dropna()
                .to_dict()
            )
        else:
            current_dollar_volume = {}
    else:
        names, sectors, current_dollar_volume = {}, {}, {}
    return {
        "universe": universe,
        "label": _MOMENTUM_UNIVERSE_LABELS.get(universe, universe),
        "tickers": tickers,
        "names": names,
        "sectors": sectors,
        "current_dollar_volume": current_dollar_volume,
        "refresh_daily": False,
    }


def _build_breakout_scan(
    *,
    universe: str | None,
    asof: str | None,
    min_return_20d: float,
    min_adr_20d: float,
    min_dollar_volume_m: float,
    min_avg_dollar_volume_m: float,
    min_consolidation_days: int,
    max_distance_ma50: float,
    pivot_proximity: float,
    market_symbol: str,
    view: str,
) -> dict[str, Any]:
    context = _breakout_universe_context(universe)
    if context.get("refresh_daily"):
        target = (
            pd.Timestamp(asof).normalize()
            if asof
            else pd.offsets.BDay().rollback(pd.Timestamp.now().normalize())
        )
        for ticker in context["tickers"]:
            refresh_daily_frame(ticker, end=target)
    filters = BreakoutFilters(
        min_return_20d=min_return_20d,
        min_adr_20d=min_adr_20d,
        min_dollar_volume=min_dollar_volume_m * 1_000_000,
        min_avg_dollar_volume=min_avg_dollar_volume_m * 1_000_000,
        min_consolidation_days=min_consolidation_days,
        max_distance_ma50=max_distance_ma50,
        pivot_proximity=pivot_proximity,
    ).normalized()
    total_universe_count = len(context["tickers"])
    scan_tickers = context["tickers"]
    current_liquidity = context.get("current_dollar_volume") or {}
    if current_liquidity and filters.min_dollar_volume > 0:
        scan_tickers = [
            ticker
            for ticker in scan_tickers
            if ticker not in current_liquidity
            or float(current_liquidity[ticker]) >= filters.min_dollar_volume
        ]
    scan = scan_breakouts(
        scan_tickers,
        filters=filters,
        asof=asof or None,
        names=context["names"],
        sectors=context["sectors"],
    )
    all_rows = scan["rows"]
    normalized_view = view if view in {"all", "setup", "ready", "breakout"} else "all"
    if normalized_view == "setup":
        visible_rows = [row for row in all_rows if row["setup_qualified"]]
    elif normalized_view == "ready":
        visible_rows = [row for row in all_rows if row["status"] in {"READY", "BREAKOUT"}]
    elif normalized_view == "breakout":
        visible_rows = [row for row in all_rows if row["status"] == "BREAKOUT"]
    else:
        visible_rows = all_rows
    for row in visible_rows:
        row["checks_passed"] = sum(bool(v) for v in row["setup_checks"].values())

    scan["all_candidate_count"] = scan["candidate_count"]
    scan["liquidity_prefilter_count"] = scan["universe_count"]
    scan["universe_count"] = total_universe_count
    scan["visible_count"] = len(visible_rows)
    scan["rows"] = visible_rows
    scan["universe"] = context["universe"]
    scan["universe_label"] = context["label"]
    scan["view"] = normalized_view
    scan["data_lag_days"] = max(0, (pd.Timestamp.now().normalize() - pd.Timestamp(scan["asof"])).days)
    scan["market"] = load_market_regime(
        asof=scan["asof"],
        symbol=market_symbol if market_symbol in {"QQQ", "IWM"} else "QQQ",
        fetch_missing=True,
    )
    return scan


def _get_breakout_scan(
    *,
    universe: str | None,
    asof: str | None,
    min_return_20d: float,
    min_adr_20d: float,
    min_dollar_volume_m: float,
    min_avg_dollar_volume_m: float,
    min_consolidation_days: int,
    max_distance_ma50: float,
    pivot_proximity: float,
    market_symbol: str,
    view: str,
    force: bool = False,
) -> dict[str, Any]:
    parameters = {
        "universe": _normalize_momentum_universe(universe),
        "asof": asof or "",
        "min_return_20d": float(min_return_20d),
        "min_adr_20d": float(min_adr_20d),
        "min_dollar_volume_m": float(min_dollar_volume_m),
        "min_avg_dollar_volume_m": float(min_avg_dollar_volume_m),
        "min_consolidation_days": int(min_consolidation_days),
        "max_distance_ma50": float(max_distance_ma50),
        "pivot_proximity": float(pivot_proximity),
        "market_symbol": str(market_symbol).upper(),
        "view": view if view in {"all", "setup", "ready", "breakout"} else "all",
    }
    is_watchlist = parameters["universe"].lower().startswith("watchlist:")
    if not force and not is_watchlist:
        cached = load_scan_cache(parameters)
        if cached is not None:
            return cached

    with _MOMENTUM_SCAN_LOCK:
        if not force and not is_watchlist:
            cached = load_scan_cache(parameters)
            if cached is not None:
                return cached
        scan = _build_breakout_scan(**parameters)
        if not is_watchlist:
            save_scan_cache(parameters, scan)
        return scan


def _breakout_daily_figure(ticker: str, frame: pd.DataFrame, pivot: float | None):
    from plotly.subplots import make_subplots

    data = frame.tail(180).copy()
    ma10 = data["close"].rolling(10).mean()
    ma20 = data["close"].rolling(20).mean()
    ma50 = data["close"].rolling(50).mean()
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.78, 0.22],
    )
    fig.add_trace(go.Candlestick(
        x=data.index,
        open=data["open"], high=data["high"], low=data["low"], close=data["close"],
        name=ticker,
        increasing_line_color="#00C853",
        decreasing_line_color="#FF5252",
    ), row=1, col=1)
    for values, name, color in [
        (ma10, "MA10 日线", "#42A5F5"),
        (ma20, "MA20 日线", "#FFB300"),
        (ma50, "MA50 日线", "#26C6DA"),
    ]:
        fig.add_trace(go.Scatter(
            x=data.index, y=values, mode="lines", name=name,
            line=dict(color=color, width=1.5),
        ), row=1, col=1)
    fig.add_trace(go.Bar(
        x=data.index,
        y=data["volume"],
        name="成交量",
        marker_color="rgba(154,160,166,0.55)",
    ), row=2, col=1)
    if pivot is not None:
        fig.add_hline(
            y=float(pivot), line_width=1, line_dash="dash", line_color="#AB47BC",
            annotation_text="20日 Pivot", annotation_position="top left", row=1, col=1,
        )
    fig.update_layout(
        title=dict(text=f"{ticker} · 日线 Setup", font=dict(color=_PLOT_TEXT, size=15)),
        paper_bgcolor=_PLOT_BG,
        plot_bgcolor=_PLOT_PANEL,
        font=dict(color=_PLOT_TEXT),
        height=640,
        margin=dict(l=44, r=24, t=56, b=32),
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    fig.update_xaxes(gridcolor=_PLOT_GRID)
    fig.update_yaxes(gridcolor=_PLOT_GRID)
    return fig


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

    target = generate_target_weights(
        strategy=strategy,
        universe=universe,
        watchlist_snapshot=watchlist_snapshot,
        asof=asof or None,
        n_groups=int(CONFIG.backtest.n_groups),
        top_group=int(CONFIG.backtest.n_groups),
    )
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
    return {
        "strategy": strategy.to_dict(),
        "universe": universe,
        "universe_label": universe_label,
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
        "score_max": max((float(r["score"]) for r in rows if r.get("score") is not None), default=None),
        "score_min": min((float(r["score"]) for r in rows if r.get("score") is not None), default=None),
    }


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
    components = _strategy_component_rows(s)
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


@router_v2.get("/strategies/{sid}/ranking", response_class=HTMLResponse)
def strategy_ranking_page(
    request: Request,
    sid: str,
    universe: str | None = Query(None),
    asof: str | None = Query(None),
):
    strategy = load_strategy(sid)
    if strategy is None:
        raise HTTPException(status_code=404, detail=f"Strategy not found: {sid}")
    selected_universe = _normalize_ranking_universe(universe)
    presets, watchlists = _ranking_universe_options()
    ranking: dict[str, Any] | None = None
    error = ""
    try:
        ranking = _build_strategy_ranking(
            strategy=strategy,
            universe=selected_universe,
            asof=(asof or "").strip() or None,
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        error = str(e)

    return templates.TemplateResponse(request, "strategy_ranking.html", {
        "title": f"策略排行 · {strategy.name}",
        "strategy": strategy.to_dict(),
        "components": _strategy_component_rows(strategy),
        "ranking": ranking,
        "error": error,
        "selected_universe": selected_universe,
        "selected_asof": (asof or "").strip(),
        "preset_universes": presets,
        "watchlists": watchlists,
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


@router_v2.get("/api/strategies/{sid}/ranking")
def api_get_strategy_ranking(
    sid: str,
    universe: str | None = Query(None),
    asof: str | None = Query(None),
):
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
    default_exec = {
        "timing": str(getattr(CONFIG.backtest.execution, "timing", "next_open")),
        "fee_model": str(getattr(CONFIG.backtest.execution, "fee_model", "ibkr_us_pro_fixed")),
        "slippage_model": str(getattr(CONFIG.backtest.execution, "slippage_model", "volume_share")),
        "slippage_bps": float(getattr(CONFIG.backtest.execution, "slippage_bps", 5)),
        "commission_bps": float(getattr(CONFIG.backtest.execution, "commission_bps", 2)),
    }
    return templates.TemplateResponse(request, "backtest_new.html", {
        "title": "新建回测",
        "strategies": strategies,
        "universes": universes,
        "watchlists": watchlists,
        "preselect_strategy_id": strategy_id,
        "preselect_watchlist_id": watchlist_id,
        "default_start": default_start,
        "default_end": default_end,
        "default_exec": default_exec,
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
    rebalance_mode = str(getattr(CONFIG.backtest, "rebalance_mode", "every_n_days"))

    # 解析 execution（用户在新建回测页可覆盖默认值）
    execution: dict | None = None
    raw_exec = payload.get("execution") or {}
    if raw_exec:
        timing = str(raw_exec.get("timing") or "").lower().strip()
        if timing and timing not in ("close", "next_open"):
            raise HTTPException(
                status_code=400,
                detail=f"execution.timing 非法（必须是 close / next_open）：{timing}",
            )
        try:
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
        if slip is not None and (slip < 0 or slip > 1000):
            raise HTTPException(
                status_code=400, detail=f"slippage_bps 超出合理范围 [0, 1000]: {slip}",
            )
        if comm is not None and (comm < 0 or comm > 1000):
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
            "fee_model": fee_model or None,
            "slippage_model": slippage_model or None,
            "slippage_bps": slip,
            "commission_bps": comm,
        }
        # 把 None 字段去掉，让 runner 用 CONFIG 默认兜底
        execution = {k: v for k, v in execution.items() if v is not None}
        if not execution:
            execution = None

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
# 模拟盘 Tab（内部 FMP 驱动模拟）
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
        "strategies": list_strategies(),
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
def paper_detail_page(request: Request, aid: str):
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
def api_get_paper_account(aid: str):
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
    strategy = load_strategy(strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail=f"策略不存在: {strategy_id}")

    watchlist_snapshot: dict[str, Any] | None = None
    if universe_raw.lower().startswith("watchlist:"):
        wid = universe_raw.split(":", 1)[1].strip()
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
        )
        create_paper_account(account)
    except (PaperTradingValidationError, FactorLibraryError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(_sanitize(account), status_code=201)


@router_v2.post("/api/paper/accounts/{aid}/run")
def api_run_paper_account(aid: str, payload: dict | None = Body(None)):
    asof = None
    if payload:
        raw_asof = payload.get("asof")
        asof = str(raw_asof).strip() if raw_asof else None
    try:
        result = run_account_once(aid, asof=asof)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Paper account not found")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(_sanitize(result))


@router_v2.delete("/api/paper/accounts/{aid}")
def api_delete_paper_account(aid: str):
    ok = delete_paper_account(aid)
    if not ok:
        raise HTTPException(status_code=404, detail="Paper account not found")
    return JSONResponse({"deleted": aid})


# ===============================================================
# 动量交易 Tab
# ===============================================================

@router_v2.get("/breakouts", response_class=HTMLResponse)
def breakouts_page(
    request: Request,
    universe: str | None = Query(None),
    asof: str | None = Query(None),
    min_return_20d: float = Query(20.0, ge=-99.0, le=1000.0),
    min_adr_20d: float = Query(6.0, ge=0.0, le=100.0),
    min_dollar_volume_m: float = Query(10.0, ge=0.0),
    min_avg_dollar_volume_m: float = Query(10.0, ge=0.0),
    min_consolidation_days: int = Query(9, ge=1, le=120),
    max_distance_ma50: float = Query(35.0, ge=0.0, le=300.0),
    pivot_proximity: float = Query(3.0, ge=0.0, le=100.0),
    market_symbol: str = Query("QQQ"),
    view: str = Query("all"),
):
    selected_universe = _normalize_momentum_universe(universe)
    error = None
    scan = None
    try:
        scan = _get_breakout_scan(
            universe=selected_universe,
            asof=(asof or "").strip() or None,
            min_return_20d=min_return_20d,
            min_adr_20d=min_adr_20d,
            min_dollar_volume_m=min_dollar_volume_m,
            min_avg_dollar_volume_m=min_avg_dollar_volume_m,
            min_consolidation_days=min_consolidation_days,
            max_distance_ma50=max_distance_ma50,
            pivot_proximity=pivot_proximity,
            market_symbol=market_symbol.upper(),
            view=view,
        )
    except Exception as exc:  # noqa: BLE001
        error = str(exc)

    preset_universes, watchlists = _momentum_universe_options()
    return templates.TemplateResponse(request, "breakout_list.html", {
        "title": "动量交易",
        "scan": scan,
        "error": error,
        "preset_universes": preset_universes,
        "watchlists": watchlists,
        "selected": {
            "universe": selected_universe,
            "asof": asof or "",
            "min_return_20d": min_return_20d,
            "min_adr_20d": min_adr_20d,
            "min_dollar_volume_m": min_dollar_volume_m,
            "min_avg_dollar_volume_m": min_avg_dollar_volume_m,
            "min_consolidation_days": min_consolidation_days,
            "max_distance_ma50": max_distance_ma50,
            "pivot_proximity": pivot_proximity,
            "market_symbol": market_symbol.upper() if market_symbol.upper() in {"QQQ", "IWM"} else "QQQ",
            "view": view if view in {"all", "setup", "ready", "breakout"} else "all",
        },
    })


@router_v2.get("/api/breakouts/scan")
def api_breakout_scan(
    universe: str | None = Query(None),
    asof: str | None = Query(None),
    min_return_20d: float = Query(20.0, ge=-99.0, le=1000.0),
    min_adr_20d: float = Query(6.0, ge=0.0, le=100.0),
    min_dollar_volume_m: float = Query(10.0, ge=0.0),
    min_avg_dollar_volume_m: float = Query(10.0, ge=0.0),
    min_consolidation_days: int = Query(9, ge=1, le=120),
    max_distance_ma50: float = Query(35.0, ge=0.0, le=300.0),
    pivot_proximity: float = Query(3.0, ge=0.0, le=100.0),
    market_symbol: str = Query("QQQ"),
    view: str = Query("all"),
):
    payload = _get_breakout_scan(
        universe=universe,
        asof=(asof or "").strip() or None,
        min_return_20d=min_return_20d,
        min_adr_20d=min_adr_20d,
        min_dollar_volume_m=min_dollar_volume_m,
        min_avg_dollar_volume_m=min_avg_dollar_volume_m,
        min_consolidation_days=min_consolidation_days,
        max_distance_ma50=max_distance_ma50,
        pivot_proximity=pivot_proximity,
        market_symbol=market_symbol.upper(),
        view=view,
    )
    return JSONResponse(_sanitize(payload))


@router_v2.get("/api/breakouts/check/{ticker}")
def api_breakout_check(
    ticker: str,
    universe: str | None = Query(None),
    asof: str | None = Query(None),
    min_return_20d: float = Query(20.0, ge=-99.0, le=1000.0),
    min_adr_20d: float = Query(6.0, ge=0.0, le=100.0),
    min_dollar_volume_m: float = Query(10.0, ge=0.0),
    min_avg_dollar_volume_m: float = Query(10.0, ge=0.0),
):
    ticker = ticker.upper().strip()
    context = _breakout_universe_context(universe)
    if ticker not in set(context["tickers"]):
        return JSONResponse({
            "ticker": ticker,
            "universe": context["universe"],
            "universe_label": context["label"],
            "in_universe": False,
            "has_data": False,
            "passes_hard_screen": False,
            "rules": [],
        })

    target = (
        pd.Timestamp(asof).normalize()
        if asof
        else pd.offsets.BDay().rollback(pd.Timestamp.now().normalize())
    )
    frame = load_daily_frame(ticker)
    if frame.empty or pd.Timestamp(frame.index.max()).normalize() < target:
        frame, _ = refresh_daily_frame(ticker, end=target)
    filters = BreakoutFilters(
        min_return_20d=min_return_20d,
        min_adr_20d=min_adr_20d,
        min_dollar_volume=min_dollar_volume_m * 1_000_000,
        min_avg_dollar_volume=min_avg_dollar_volume_m * 1_000_000,
    ).normalized()
    metric = evaluate_daily_setup(
        frame,
        ticker=ticker,
        filters=filters,
        asof=(asof or "").strip() or None,
        name=str(context["names"].get(ticker) or ""),
        sector=str(context["sectors"].get(ticker) or ""),
    ) if not frame.empty else None
    if metric is None:
        return JSONResponse({
            "ticker": ticker,
            "name": str(context["names"].get(ticker) or ""),
            "universe": context["universe"],
            "universe_label": context["label"],
            "in_universe": True,
            "has_data": False,
            "passes_hard_screen": False,
            "rules": [],
        })

    rules = [
        {
            "key": "return_20d",
            "label": "20日涨幅",
            "actual": metric["return_20d"],
            "threshold": filters.min_return_20d,
            "actual_text": f"{metric['return_20d']:.2f}%",
            "threshold_text": f"≥ {filters.min_return_20d:.1f}%",
            "passed": metric["base_checks"]["return_20d"],
        },
        {
            "key": "adr_20d",
            "label": "ADR20",
            "actual": metric["adr_20d"],
            "threshold": filters.min_adr_20d,
            "actual_text": f"{metric['adr_20d']:.2f}%",
            "threshold_text": f"≥ {filters.min_adr_20d:.1f}%",
            "passed": metric["base_checks"]["adr_20d"],
        },
        {
            "key": "dollar_volume",
            "label": "当日成交额",
            "actual": metric["dollar_volume"] / 1_000_000,
            "threshold": filters.min_dollar_volume / 1_000_000,
            "actual_text": f"${metric['dollar_volume'] / 1_000_000:.1f}M",
            "threshold_text": f"≥ ${filters.min_dollar_volume / 1_000_000:.1f}M",
            "passed": metric["base_checks"]["dollar_volume"],
        },
        {
            "key": "avg_dollar_volume",
            "label": "20日均成交额",
            "actual": metric["avg_dollar_volume_20d"] / 1_000_000,
            "threshold": filters.min_avg_dollar_volume / 1_000_000,
            "actual_text": f"${metric['avg_dollar_volume_20d'] / 1_000_000:.1f}M",
            "threshold_text": f"≥ ${filters.min_avg_dollar_volume / 1_000_000:.1f}M",
            "passed": metric["base_checks"]["avg_dollar_volume"],
        },
    ]
    return JSONResponse(_sanitize({
        "ticker": ticker,
        "name": metric["name"],
        "sector": metric["sector"],
        "universe": context["universe"],
        "universe_label": context["label"],
        "in_universe": True,
        "has_data": True,
        "data_date": metric["data_date"],
        "passes_hard_screen": metric["base_pass"],
        "status": metric["status"],
        "score": metric["score"],
        "rules": rules,
    }))


@router_v2.get("/breakouts/{ticker}", response_class=HTMLResponse)
def breakout_detail_page(
    request: Request,
    ticker: str,
    universe: str | None = Query(None),
    asof: str | None = Query(None),
):
    ticker = ticker.upper().strip()
    context = _breakout_universe_context(universe)
    frame = load_daily_frame(ticker)
    if frame.empty:
        raise HTTPException(status_code=404, detail=f"没有 {ticker} 的日线缓存")
    if asof:
        frame = frame.loc[frame.index <= pd.Timestamp(asof)]
    metric = evaluate_daily_setup(
        frame,
        ticker=ticker,
        filters=BreakoutFilters(),
        name=str(context["names"].get(ticker) or ""),
        sector=str(context["sectors"].get(ticker) or ""),
    )
    if metric is None:
        raise HTTPException(status_code=422, detail=f"{ticker} 日线样本不足 65 个交易日")

    check_labels = {
        "prior_move": ("前期大涨", f"{metric['prior_move']:.1f}% / 目标 ≥30%"),
        "consolidation": ("整理时间", f"{metric['consolidation_days']} 个交易日 / 目标 ≥9"),
        "ma50_distance": ("距离 MA50", f"{metric['distance_ma50']:.1f}% / 上限 35%"),
        "ma_trend": ("均线与支撑", "价格靠近 MA20，MA10/MA20 保持上升结构"),
        "tight_range": ("波动收缩", f"近3日/ADR 比率 {metric['tightness']:.2f} / 目标 ≤0.55"),
        "higher_lows": ("低点抬高", f"近5日低点斜率 {metric['higher_low_slope']:.2f}%"),
        "volume_dryup": ("整理缩量", f"近期/基准量比 {metric['volume_dryup']:.2f} / 目标 ≤0.85"),
        "near_pivot": ("接近 Pivot", f"距20日 Pivot {metric['pivot_distance']:.1f}% / 目标 ≥-3%"),
        "stop_within_adr": ("止损宽度", f"当日低点风险 {metric['stop_width']:.1f}% vs ADR {metric['adr_20d']:.1f}%"),
    }
    check_rows = [
        {"key": key, "label": check_labels[key][0], "value": check_labels[key][1], "passed": passed}
        for key, passed in metric["setup_checks"].items()
    ]
    market = load_market_regime(asof=metric["data_date"], symbol="QQQ", fetch_missing=True)
    daily_fig = _breakout_daily_figure(ticker, frame, metric.get("pivot"))
    return templates.TemplateResponse(request, "breakout_detail.html", {
        "title": f"{ticker} · 动量诊断",
        "ticker": ticker,
        "universe": context["universe"],
        "universe_label": context["label"],
        "metric": metric,
        "market": market,
        "check_rows": check_rows,
        "daily_fig_json": fig_to_json(daily_fig),
    })


@router_v2.get("/api/breakouts/{ticker}/intraday")
def api_breakout_intraday(
    ticker: str,
    interval: int = Query(5),
    session: str | None = Query(None),
    refresh: bool = Query(True),
):
    if interval not in {1, 5, 15, 30, 60}:
        raise HTTPException(status_code=400, detail="interval 必须是 1/5/15/30/60")
    ticker = ticker.upper().strip()
    frame, source = load_intraday_1min(ticker, refresh=refresh)
    snapshot = build_intraday_snapshot(frame, interval=interval, session_date=session)
    snapshot["ticker"] = ticker
    snapshot["source"] = source

    session_date = snapshot.get("session_date")
    if session_date:
        daily, daily_source = refresh_daily_frame(ticker, end=session_date)
    else:
        daily, daily_source = load_daily_frame(ticker), "cache"
    snapshot["daily_source"] = daily_source
    metric = evaluate_daily_setup(daily, ticker=ticker, filters=BreakoutFilters()) if not daily.empty else None
    if metric and snapshot.get("last_price") is not None:
        snapshot["daily_data_date"] = metric["data_date"]
        snapshot["daily_adr_20d"] = metric["adr_20d"]
        snapshot["daily_pivot"] = metric["pivot"]
        snapshot["pivot_triggered"] = snapshot["last_price"] >= metric["pivot"]
        snapshot["stop_within_adr"] = (
            snapshot.get("stop_width") is not None
            and snapshot["stop_width"] <= metric["adr_20d"]
        )
    return JSONResponse(_sanitize(snapshot))


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
