"""
路由定义。

页面（统一支持 ?universe=XXX 切换股票池）：
  GET /                     首页（策略概览）
  GET /factor/{name}        因子详情页
  GET /backtest             回测结果页
  GET /backtest/{name}      回测结果页（指定因子）
  GET /stock/{ticker}       单股诊断页（A 诊断卡 + B 时序图）

JSON API：
  GET /api/universes
  GET /api/factors
  GET /api/factor/{name}/ic
  GET /api/factor/{name}/nav
  GET /api/factor/{name}/summary
  GET /api/stock/{ticker}                  完整因子 + 快照 JSON
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.analysis import compute_single_stock_factors
from src.config import CONFIG
from src.visualization import (
    fig_to_json,
    plot_drawdown_plotly,
    plot_factor_coverage_plotly,
    plot_factor_latest_distribution_plotly,
    plot_group_bar_plotly,
    plot_group_diagnostics_plotly,
    plot_group_monotonicity_plotly,
    plot_ic_distribution_plotly,
    plot_ic_monthly_heatmap_plotly,
    plot_ic_rolling_plotly,
    plot_ic_series_plotly,
    plot_quintile_nav_plotly,
    plot_return_distribution_plotly,
)
from src.webapp.results_store import (
    DEFAULT_UNIVERSE,
    list_factors,
    list_universes,
    load_factor,
    load_factor_values,
)

_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

router = APIRouter()


# ----------------------------- 工具 -----------------------------

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


def _format_num(x, digits: int = 4) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    return f"{x:.{digits}f}"


def _resolve_universe(requested: str | None) -> str:
    """规范化 universe 参数：未指定则取配置默认。"""
    if requested:
        return requested.upper()
    try:
        return str(CONFIG.universes.default).upper()
    except AttributeError:
        return DEFAULT_UNIVERSE


def _ic_summary_rows(universe: str) -> list[dict]:
    rows = []
    for name in list_factors(universe=universe):
        data = load_factor(name, universe=universe)
        if not data:
            continue
        s = data["ic_summary"]
        rows.append({
            "factor": name,
            "description": data["meta"].get("description", ""),
            "direction": data["meta"].get("direction", 0),
            "IC_mean": _format_num(s.get("IC_mean")),
            "IC_std":  _format_num(s.get("IC_std")),
            "IC_IR":   _format_num(s.get("IC_IR")),
            "IC_gt0_pct": _format_pct(s.get("IC_gt0_pct")),
            "IC_abs_gt_thr_pct": _format_pct(s.get("IC_abs_gt_thr_pct")),
            "t_stat":  _format_num(s.get("t_stat")),
            "N":       s.get("N", 0),
        })
    return rows


def _performance_card(metrics_df: pd.DataFrame, group: str = "LongShort") -> dict:
    if metrics_df.empty or group not in metrics_df.index:
        return {"AnnReturn": "—", "Sharpe": "—", "MaxDD": "—", "Calmar": "—"}
    row = metrics_df.loc[group]
    return {
        "AnnReturn": _format_pct(row.get("AnnReturn")),
        "Sharpe":    _format_num(row.get("Sharpe"), 3),
        "MaxDD":     _format_pct(row.get("MaxDD")),
        "Calmar":    _format_num(row.get("Calmar"), 3),
    }


def _group_metrics_table(metrics_df: pd.DataFrame) -> list[dict]:
    if metrics_df.empty:
        return []
    rows = []
    for idx, row in metrics_df.iterrows():
        rows.append({
            "group":     idx,
            "AnnReturn": _format_pct(row.get("AnnReturn")),
            "AnnVol":    _format_pct(row.get("AnnVol")),
            "Sharpe":    _format_num(row.get("Sharpe"), 3),
            "MaxDD":     _format_pct(row.get("MaxDD")),
            "Calmar":    _format_num(row.get("Calmar"), 3),
            "WinRate":   _format_pct(row.get("WinRate")),
            "AvgTurnover": _format_pct(row.get("AvgTurnover")) if "AvgTurnover" in row else "—",
        })
    return rows


def _factor_quality_summary(factor_df: pd.DataFrame | None) -> dict:
    empty = {
        "latest_date": "—",
        "latest_valid": "—",
        "latest_missing": "—",
        "median_valid": "—",
        "avg_cross_section_std": "—",
        "latest_p5": "—",
        "latest_p95": "—",
    }
    if factor_df is None or factor_df.empty:
        return empty

    f = factor_df.apply(pd.to_numeric, errors="coerce").replace([math.inf, -math.inf], float("nan"))
    total = max(len(f.columns), 1)
    active = f.dropna(how="all")
    if active.empty:
        return empty

    latest_date = active.index.max()
    latest = pd.to_numeric(active.loc[latest_date], errors="coerce").dropna()
    valid_count = f.notna().sum(axis=1)
    cross_section_std = f.std(axis=1, ddof=1).replace([math.inf, -math.inf], float("nan")).dropna()

    latest_valid = int(latest.shape[0])
    return {
        "latest_date": pd.Timestamp(latest_date).strftime("%Y-%m-%d"),
        "latest_valid": f"{latest_valid:,}",
        "latest_missing": _format_pct(1.0 - latest_valid / total),
        "median_valid": f"{int(valid_count.median()):,}" if not valid_count.empty else "—",
        "avg_cross_section_std": _format_num(float(cross_section_std.mean())) if not cross_section_std.empty else "—",
        "latest_p5": _format_num(float(latest.quantile(0.05))) if not latest.empty else "—",
        "latest_p95": _format_num(float(latest.quantile(0.95))) if not latest.empty else "—",
    }


# ----------------------------- 页面 -----------------------------

@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    factor: str | None = None,
    universe: str | None = Query(None),
):
    universe = _resolve_universe(universe)
    factors = list_factors(universe=universe)
    rows = _ic_summary_rows(universe)
    universes = list_universes()
    hero_fig_json = None
    hero_factor = None
    hero_metrics = {}

    if factors:
        if factor and factor in factors:
            hero_factor = factor
        else:
            best = None
            best_abs_ir = -1.0
            for name in factors:
                d = load_factor(name, universe=universe)
                if not d:
                    continue
                ir = d["ic_summary"].get("IC_IR")
                if ir is None or (isinstance(ir, float) and math.isnan(ir)):
                    continue
                if abs(ir) > best_abs_ir:
                    best_abs_ir = abs(ir)
                    best = name
            hero_factor = best or factors[0]

        data = load_factor(hero_factor, universe=universe)
        if data:
            fig = plot_quintile_nav_plotly(
                data["group_nav"], data["ls_nav"],
                title=f"[{universe}] {hero_factor} · 五分位累计净值",
            )
            hero_fig_json = fig_to_json(fig)
            hero_metrics = _performance_card(data["group_metrics"], "LongShort")

    return templates.TemplateResponse(request, "index.html", {
        "title": CONFIG.webapp.title,
        "universe": universe,
        "universes": universes,
        "factors": factors,
        "rows": rows,
        "hero_factor": hero_factor,
        "hero_fig_json": hero_fig_json,
        "hero_metrics": hero_metrics,
    })


@router.get("/factor/{name}", response_class=HTMLResponse)
def factor_detail(
    request: Request,
    name: str,
    universe: str | None = Query(None),
):
    universe = _resolve_universe(universe)
    data = load_factor(name, universe=universe)
    if not data:
        raise HTTPException(
            status_code=404,
            detail=f"Factor {name} not found in universe {universe}",
        )

    ic_fig = plot_ic_series_plotly(data["ic"], title=f"[{universe}] {name} · IC 时序")
    ic_dist_fig = plot_ic_distribution_plotly(data["ic"], title=f"[{universe}] {name} · IC 分布")
    ic_rolling_fig = plot_ic_rolling_plotly(data["ic"], title=f"[{universe}] {name} · 滚动 IC 稳定性")
    ic_heatmap_fig = plot_ic_monthly_heatmap_plotly(data["ic"], title=f"[{universe}] {name} · 月度 IC 热力图")
    nav_fig = plot_quintile_nav_plotly(
        data["group_nav"], data["ls_nav"],
        title=f"[{universe}] {name} · 五分位累计净值",
    )
    group_diag_fig = plot_group_diagnostics_plotly(
        data["group_metrics"],
        title=f"[{universe}] {name} · 分组收益与 Sharpe",
    )
    monotonicity_fig = plot_group_monotonicity_plotly(
        data["group_metrics"],
        column="AnnReturn",
        title=f"[{universe}] {name} · 分组单调性",
    )
    return_dist_fig = plot_return_distribution_plotly(
        data["ls_returns"],
        title=f"[{universe}] {name} · 多空日收益分布",
    )
    factor_values = load_factor_values(name, universe=universe)
    coverage_fig = plot_factor_coverage_plotly(
        factor_values if factor_values is not None else pd.DataFrame(),
        title=f"[{universe}] {name} · 因子覆盖率与缺失率",
    )
    latest_dist_fig = plot_factor_latest_distribution_plotly(
        factor_values if factor_values is not None else pd.DataFrame(),
        title=f"[{universe}] {name} · 最近一期因子分布",
    )

    return templates.TemplateResponse(request, "factor.html", {
        "title": f"{name} · 因子详情",
        "universe": universe,
        "universes": list_universes(),
        "factors": list_factors(universe=universe),
        "name": name,
        "meta": data["meta"],
        "ic_summary": {
            "IC_mean":           _format_num(data["ic_summary"].get("IC_mean")),
            "IC_std":            _format_num(data["ic_summary"].get("IC_std")),
            "IC_IR":             _format_num(data["ic_summary"].get("IC_IR")),
            "IC_gt0_pct":        _format_pct(data["ic_summary"].get("IC_gt0_pct")),
            "IC_abs_gt_thr_pct": _format_pct(data["ic_summary"].get("IC_abs_gt_thr_pct")),
            "t_stat":            _format_num(data["ic_summary"].get("t_stat")),
            "N":                 data["ic_summary"].get("N", 0),
        },
        "factor_quality": _factor_quality_summary(factor_values),
        "ic_fig_json": fig_to_json(ic_fig),
        "ic_dist_fig_json": fig_to_json(ic_dist_fig),
        "ic_rolling_fig_json": fig_to_json(ic_rolling_fig),
        "ic_heatmap_fig_json": fig_to_json(ic_heatmap_fig),
        "nav_fig_json": fig_to_json(nav_fig),
        "group_diag_fig_json": fig_to_json(group_diag_fig),
        "monotonicity_fig_json": fig_to_json(monotonicity_fig),
        "return_dist_fig_json": fig_to_json(return_dist_fig),
        "coverage_fig_json": fig_to_json(coverage_fig),
        "latest_dist_fig_json": fig_to_json(latest_dist_fig),
    })


@router.get("/backtest", response_class=HTMLResponse)
def backtest_index(request: Request, universe: str | None = Query(None)):
    universe = _resolve_universe(universe)
    factors = list_factors(universe=universe)
    if not factors:
        return templates.TemplateResponse(request, "backtest.html", {
            "title": "回测",
            "universe": universe,
            "universes": list_universes(),
            "factors": [],
            "name": None,
        })
    return backtest_detail(request, factors[0], universe=universe)


@router.get("/backtest/{name}", response_class=HTMLResponse)
def backtest_detail(
    request: Request,
    name: str,
    universe: str | None = Query(None),
):
    universe = _resolve_universe(universe)
    data = load_factor(name, universe=universe)
    if not data:
        raise HTTPException(
            status_code=404,
            detail=f"Factor {name} not found in universe {universe}",
        )

    nav_fig = plot_quintile_nav_plotly(
        data["group_nav"], data["ls_nav"],
        title=f"[{universe}] {name} · 五分位累计净值",
    )
    bar_fig = plot_group_bar_plotly(
        data["group_metrics"], column="AnnReturn",
        title=f"[{universe}] {name} · 分组年化收益",
    )
    dd_fig = plot_drawdown_plotly(
        data["ls_returns"],
        title=f"[{universe}] {name} · 多空回撤",
    )

    return templates.TemplateResponse(request, "backtest.html", {
        "title": f"{name} · 回测详情",
        "universe": universe,
        "universes": list_universes(),
        "factors": list_factors(universe=universe),
        "name": name,
        "meta": data["meta"],
        "backtest_config": data["backtest_config"],
        "ls_metrics": _performance_card(data["group_metrics"], "LongShort"),
        "group_rows": _group_metrics_table(data["group_metrics"]),
        "nav_fig_json": fig_to_json(nav_fig),
        "bar_fig_json": fig_to_json(bar_fig),
        "dd_fig_json": fig_to_json(dd_fig),
    })


# ---- 单股 ----

@router.get("/stock", response_class=HTMLResponse)
def stock_search(request: Request, ticker: str = Query(...)):
    """支持顶部搜索框：/stock?ticker=INTC -> 跳到 /stock/INTC。"""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is required")
    return RedirectResponse(url=f"/stock/{ticker}", status_code=302)


@router.get("/stock/{ticker}", response_class=HTMLResponse)
def stock_detail(
    request: Request,
    ticker: str,
    universe: str | None = Query(None),
):
    universe = _resolve_universe(universe)
    ticker = ticker.upper().strip()
    res = compute_single_stock_factors(ticker, reference_universe=universe)

    # 时序图：每个因子一条线（双 Y 轴：左轴动量/反转/换手率值域较大，右轴波动率较小）
    ts_fig_json = None
    if not res.factor_ts.empty:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
        # 简单方案：所有因子归一化到 z-score 后画在同一张图上，便于对比形态
        ts = res.factor_ts.copy()
        # 沿时间标准化：x -> (x - mean) / std
        ts_z = (ts - ts.mean()) / ts.std(ddof=1).replace(0, pd.NA)
        fig = go.Figure()
        palette = ["#42A5F5", "#FF5252", "#00C853", "#FFB300",
                   "#AB47BC", "#26C6DA", "#FF9100", "#9CCC65"]
        for i, col in enumerate(ts_z.columns):
            fig.add_trace(go.Scatter(
                x=ts_z.index, y=ts_z[col].values, mode="lines",
                name=col,
                line=dict(color=palette[i % len(palette)], width=1.6),
            ))
        fig.update_layout(
            title=dict(text=f"{ticker} · 8 因子时序（Z-Score 标准化）",
                       font=dict(color="#E8EAED", size=15)),
            paper_bgcolor="#0E1117", plot_bgcolor="#1A1F2E",
            font=dict(color="#E8EAED"),
            margin=dict(l=40, r=40, t=60, b=40),
            height=460,
            hovermode="x unified",
            legend=dict(bgcolor="rgba(0,0,0,0)", bordercolor="#262B3A", borderwidth=0.5),
        )
        fig.update_xaxes(gridcolor="#262B3A")
        fig.update_yaxes(gridcolor="#262B3A", title_text="Z-Score")
        ts_fig_json = fig_to_json(fig)

    # 把 snapshot 格式化成模板友好的列表
    snapshot_rows: list[dict] = []
    snapshot = res.snapshot or {}
    for fname, item in (snapshot.get("factors") or {}).items():
        snapshot_rows.append({
            "factor": fname,
            "value":  _format_num(item.get("value"), 4),
            "rank":   item.get("rank"),
            "pool_size": item.get("pool_size"),
            "quintile": item.get("quintile") or "—",
            "percentile": _format_num(item.get("percentile"), 1) if item.get("percentile") is not None else "—",
        })

    return templates.TemplateResponse(request, "stock.html", {
        "title": f"{ticker} · 单股诊断",
        "universe": universe,
        "universes": list_universes(),
        "ticker": ticker,
        "result": res,
        "meta": res.meta,
        "source": res.source,
        "snapshot_date": (res.snapshot or {}).get("date") or "—",
        "snapshot_rows": snapshot_rows,
        "ts_fig_json": ts_fig_json,
        "error": res.error,
    })


# ----------------------------- JSON API -----------------------------

@router.get("/api/universes")
def api_universes():
    return JSONResponse({"universes": list_universes(),
                         "default": _resolve_universe(None)})


@router.get("/api/factors")
def api_factors(universe: str | None = Query(None)):
    universe = _resolve_universe(universe)
    factors = list_factors(universe=universe)
    metas = []
    for n in factors:
        d = load_factor(n, universe=universe)
        if d:
            metas.append(d["meta"])
    return JSONResponse(_sanitize({
        "universe": universe, "count": len(factors), "factors": metas,
    }))


@router.get("/api/factor/{name}/ic")
def api_factor_ic(name: str, universe: str | None = Query(None)):
    universe = _resolve_universe(universe)
    d = load_factor(name, universe=universe)
    if not d:
        raise HTTPException(status_code=404, detail="Factor not found")
    ic: pd.Series = d["ic"]
    payload = {
        "universe": universe,
        "name": name,
        "dates": [dt.strftime("%Y-%m-%d") for dt in ic.index],
        "ic":    ic.tolist(),
        "cum_ic": ic.cumsum().tolist(),
        "summary": d["ic_summary"],
    }
    return JSONResponse(_sanitize(payload))


@router.get("/api/factor/{name}/nav")
def api_factor_nav(name: str, universe: str | None = Query(None)):
    universe = _resolve_universe(universe)
    d = load_factor(name, universe=universe)
    if not d:
        raise HTTPException(status_code=404, detail="Factor not found")
    group_nav: pd.DataFrame = d["group_nav"]
    ls_nav: pd.Series = d["ls_nav"]
    payload = {
        "universe": universe,
        "name": name,
        "dates": [dt.strftime("%Y-%m-%d") for dt in group_nav.index],
        "groups": {col: group_nav[col].tolist() for col in group_nav.columns},
        "long_short": ls_nav.tolist(),
    }
    return JSONResponse(_sanitize(payload))


@router.get("/api/factor/{name}/summary")
def api_factor_summary(name: str, universe: str | None = Query(None)):
    universe = _resolve_universe(universe)
    d = load_factor(name, universe=universe)
    if not d:
        raise HTTPException(status_code=404, detail="Factor not found")
    metrics = d["group_metrics"]
    payload = {
        "universe": universe,
        "name": name,
        "meta": d["meta"],
        "ic_summary": d["ic_summary"],
        "backtest_config": d["backtest_config"],
        "group_metrics": {idx: row.to_dict() for idx, row in metrics.iterrows()} if not metrics.empty else {},
    }
    return JSONResponse(_sanitize(payload))


@router.get("/api/stock/{ticker}")
def api_stock(ticker: str, universe: str | None = Query(None)):
    universe = _resolve_universe(universe)
    res = compute_single_stock_factors(ticker, reference_universe=universe)
    ts = res.factor_ts
    payload = {
        "ticker": res.ticker,
        "source": res.source,
        "pool_universe": res.pool_universe,
        "meta":   res.meta,
        "snapshot": res.snapshot,
        "factor_ts": {
            "dates":   [dt.strftime("%Y-%m-%d") for dt in ts.index],
            "factors": {col: ts[col].tolist() for col in ts.columns},
        } if not ts.empty else {},
        "error": res.error,
    }
    return JSONResponse(_sanitize(payload))


__all__ = ["router"]
