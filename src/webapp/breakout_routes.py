"""Web adapter for momentum breakout pages and JSON APIs."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from src.breakouts import (
    BreakoutFilters,
    build_intraday_snapshot,
    evaluate_daily_setup,
    load_intraday_1min,
    load_market_regime,
    refresh_daily_frame,
)
from src.breakouts.application import (
    BreakoutApplicationError,
    BreakoutScanNotReadyError,
    BreakoutWatchlistNotFoundError,
    UnknownBreakoutUniverseError,
    breakout_universe_options,
    get_breakout_scan,
    normalize_breakout_universe,
    resolve_breakout_universe,
)
from src.breakouts.scanner import load_daily_frame
from src.config import CONFIG
from src.research_universes.registry import research_universe_registry
from src.visualization.plots_plotly import fig_to_json
from src.webapp.results_store import list_universes


_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
router = APIRouter()

_PLOT_BG = "#0E1117"
_PLOT_PANEL = "#1A1F2E"
_PLOT_GRID = "#262B3A"
_PLOT_TEXT = "#E8EAED"


def _sanitize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {key: _sanitize(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(value) for value in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def _enabled_breakout_universes() -> list[str]:
    configured = [
        entry.universe_id
        for entry in research_universe_registry().factor_data_entries()
    ]
    seen: dict[str, None] = {}
    for universe in [*configured, *list_universes()]:
        seen.setdefault(str(universe).upper(), None)
    return list(seen)


def _http_universe_context(raw: str | None) -> dict[str, Any]:
    try:
        return resolve_breakout_universe(
            raw,
            enabled_universes=_enabled_breakout_universes(),
        )
    except BreakoutWatchlistNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnknownBreakoutUniverseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _http_breakout_scan(**parameters: Any) -> dict[str, Any]:
    try:
        return get_breakout_scan(
            **parameters,
            enabled_universes=_enabled_breakout_universes(),
            allow_build=False,
        )
    except BreakoutWatchlistNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnknownBreakoutUniverseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BreakoutScanNotReadyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except BreakoutApplicationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _breakout_daily_figure(
    ticker: str,
    frame: pd.DataFrame,
    pivot: float | None,
):
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
    fig.add_trace(
        go.Candlestick(
            x=data.index,
            open=data["open"],
            high=data["high"],
            low=data["low"],
            close=data["close"],
            name=ticker,
            increasing_line_color="#00C853",
            decreasing_line_color="#FF5252",
        ),
        row=1,
        col=1,
    )
    for values, name, color in [
        (ma10, "MA10 日线", "#42A5F5"),
        (ma20, "MA20 日线", "#FFB300"),
        (ma50, "MA50 日线", "#26C6DA"),
    ]:
        fig.add_trace(
            go.Scatter(
                x=data.index,
                y=values,
                mode="lines",
                name=name,
                line=dict(color=color, width=1.5),
            ),
            row=1,
            col=1,
        )
    fig.add_trace(
        go.Bar(
            x=data.index,
            y=data["volume"],
            name="成交量",
            marker_color="rgba(154,160,166,0.55)",
        ),
        row=2,
        col=1,
    )
    if pivot is not None:
        fig.add_hline(
            y=float(pivot),
            line_width=1,
            line_dash="dash",
            line_color="#AB47BC",
            annotation_text="20日 Pivot",
            annotation_position="top left",
            row=1,
            col=1,
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


@router.get("/breakouts", response_class=HTMLResponse)
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
    selected_universe = normalize_breakout_universe(universe)
    error = None
    scan = None
    try:
        scan = get_breakout_scan(
            universe=selected_universe,
            enabled_universes=_enabled_breakout_universes(),
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
            allow_build=False,
        )
    except Exception as exc:  # noqa: BLE001
        error = str(exc)

    preset_universes, watchlists = breakout_universe_options(
        _enabled_breakout_universes()
    )
    return templates.TemplateResponse(
        request,
        "breakout_list.html",
        {
            "title": "茶杯柄监控",
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
                "market_symbol": (
                    market_symbol.upper()
                    if market_symbol.upper() in {"QQQ", "IWM"}
                    else "QQQ"
                ),
                "view": view if view in {"all", "setup", "ready", "breakout"} else "all",
            },
        },
    )


@router.get("/api/breakouts/scan")
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
    payload = _http_breakout_scan(
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


@router.get("/api/breakouts/check/{ticker}")
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
    context = _http_universe_context(universe)
    if ticker not in set(context["tickers"]):
        return JSONResponse(
            {
                "ticker": ticker,
                "universe": context["universe"],
                "universe_label": context["label"],
                "in_universe": False,
                "has_data": False,
                "passes_hard_screen": False,
                "rules": [],
            }
        )

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
    metric = (
        evaluate_daily_setup(
            frame,
            ticker=ticker,
            filters=filters,
            asof=(asof or "").strip() or None,
            name=str(context["names"].get(ticker) or ""),
            sector=str(context["sectors"].get(ticker) or ""),
        )
        if not frame.empty
        else None
    )
    if metric is None:
        return JSONResponse(
            {
                "ticker": ticker,
                "name": str(context["names"].get(ticker) or ""),
                "universe": context["universe"],
                "universe_label": context["label"],
                "in_universe": True,
                "has_data": False,
                "passes_hard_screen": False,
                "rules": [],
            }
        )

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
            "threshold_text": (
                f"≥ ${filters.min_avg_dollar_volume / 1_000_000:.1f}M"
            ),
            "passed": metric["base_checks"]["avg_dollar_volume"],
        },
    ]
    return JSONResponse(
        _sanitize(
            {
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
            }
        )
    )


@router.get("/breakouts/{ticker}", response_class=HTMLResponse)
def breakout_detail_page(
    request: Request,
    ticker: str,
    universe: str | None = Query(None),
    asof: str | None = Query(None),
):
    ticker = ticker.upper().strip()
    context = _http_universe_context(universe)
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
        "consolidation": (
            "整理时间",
            f"{metric['consolidation_days']} 个交易日 / 目标 ≥9",
        ),
        "ma50_distance": (
            "距离 MA50",
            f"{metric['distance_ma50']:.1f}% / 上限 35%",
        ),
        "ma_trend": ("均线与支撑", "价格靠近 MA20，MA10/MA20 保持上升结构"),
        "tight_range": (
            "波动收缩",
            f"近3日/ADR 比率 {metric['tightness']:.2f} / 目标 ≤0.55",
        ),
        "higher_lows": (
            "低点抬高",
            f"近5日低点斜率 {metric['higher_low_slope']:.2f}%",
        ),
        "volume_dryup": (
            "整理缩量",
            f"近期/基准量比 {metric['volume_dryup']:.2f} / 目标 ≤0.85",
        ),
        "near_pivot": (
            "接近 Pivot",
            f"距20日 Pivot {metric['pivot_distance']:.1f}% / 目标 ≥-3%",
        ),
        "stop_within_adr": (
            "止损宽度",
            f"当日低点风险 {metric['stop_width']:.1f}% vs ADR {metric['adr_20d']:.1f}%",
        ),
    }
    check_rows = [
        {
            "key": key,
            "label": check_labels[key][0],
            "value": check_labels[key][1],
            "passed": passed,
        }
        for key, passed in metric["setup_checks"].items()
    ]
    market = load_market_regime(
        asof=metric["data_date"],
        symbol="QQQ",
        fetch_missing=True,
    )
    daily_fig = _breakout_daily_figure(ticker, frame, metric.get("pivot"))
    return templates.TemplateResponse(
        request,
        "breakout_detail.html",
        {
            "title": f"{ticker} · 茶杯柄诊断",
            "ticker": ticker,
            "universe": context["universe"],
            "universe_label": context["label"],
            "metric": metric,
            "market": market,
            "check_rows": check_rows,
            "daily_fig_json": fig_to_json(daily_fig),
        },
    )


@router.get("/api/breakouts/{ticker}/intraday")
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
    snapshot = build_intraday_snapshot(
        frame,
        interval=interval,
        session_date=session,
    )
    snapshot["ticker"] = ticker
    snapshot["source"] = source

    session_date = snapshot.get("session_date")
    if session_date:
        daily, daily_source = refresh_daily_frame(ticker, end=session_date)
    else:
        daily, daily_source = load_daily_frame(ticker), "cache"
    snapshot["daily_source"] = daily_source
    metric = (
        evaluate_daily_setup(daily, ticker=ticker, filters=BreakoutFilters())
        if not daily.empty
        else None
    )
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


__all__ = ["router", "templates"]
