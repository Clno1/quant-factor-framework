"""
路由定义。

页面：
  GET /                 首页（策略概览）
  GET /factor/{name}    因子详情页
  GET /backtest         回测结果页（默认展示第一个因子）
  GET /backtest/{name}  回测结果页（指定因子）

JSON API：
  GET /api/factors               因子列表与元信息
  GET /api/factor/{name}/ic      IC 时序 JSON
  GET /api/factor/{name}/nav     分组净值 + Long-Short 净值
  GET /api/factor/{name}/summary IC 汇总 + 绩效汇总
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from src.config import CONFIG
from src.visualization import (
    fig_to_json,
    plot_drawdown_plotly,
    plot_group_bar_plotly,
    plot_ic_series_plotly,
    plot_quintile_nav_plotly,
)
from src.webapp.results_store import list_factors, load_factor

_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

router = APIRouter()


# ----------------------------- 工具 -----------------------------

def _sanitize(obj: Any) -> Any:
    """递归将 NaN/Inf 替换为 None（JSON 兼容）。"""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
    return obj


def _format_pct(x: float | None, digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    return f"{x * 100:.{digits}f}%"


def _format_num(x: float | None, digits: int = 4) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    return f"{x:.{digits}f}"


def _ic_summary_rows() -> list[dict]:
    """组装所有因子 IC 汇总表（首页 + 因子页共用）。"""
    rows = []
    for name in list_factors():
        data = load_factor(name)
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
    """格式化分组绩效表。"""
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


# ----------------------------- 页面 -----------------------------

@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    factors = list_factors()
    rows = _ic_summary_rows()
    hero_fig_json = None
    hero_factor = None
    hero_metrics = {}
    if factors:
        hero_factor = factors[0]
        data = load_factor(hero_factor)
        if data:
            fig = plot_quintile_nav_plotly(data["group_nav"], data["ls_nav"],
                                           title=f"{hero_factor} · Quintile NAV")
            hero_fig_json = fig_to_json(fig)
            hero_metrics = _performance_card(data["group_metrics"], "LongShort")

    return templates.TemplateResponse(request, "index.html", {
        "title": CONFIG.webapp.title,
        "factors": factors,
        "rows": rows,
        "hero_factor": hero_factor,
        "hero_fig_json": hero_fig_json,
        "hero_metrics": hero_metrics,
    })


@router.get("/factor/{name}", response_class=HTMLResponse)
def factor_detail(request: Request, name: str):
    data = load_factor(name)
    if not data:
        raise HTTPException(status_code=404, detail=f"Factor {name} not found")

    ic_fig = plot_ic_series_plotly(data["ic"], title=f"{name} · IC Time Series")
    nav_fig = plot_quintile_nav_plotly(data["group_nav"], data["ls_nav"],
                                       title=f"{name} · Quintile NAV")

    return templates.TemplateResponse(request, "factor.html", {
        "title": f"{name} · Factor Detail",
        "factors": list_factors(),
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
        "ic_fig_json": fig_to_json(ic_fig),
        "nav_fig_json": fig_to_json(nav_fig),
    })


@router.get("/backtest", response_class=HTMLResponse)
def backtest_index(request: Request):
    factors = list_factors()
    if not factors:
        return templates.TemplateResponse(request, "backtest.html", {
            "title": "Backtest",
            "factors": [],
            "name": None,
        })
    return backtest_detail(request, factors[0])


@router.get("/backtest/{name}", response_class=HTMLResponse)
def backtest_detail(request: Request, name: str):
    data = load_factor(name)
    if not data:
        raise HTTPException(status_code=404, detail=f"Factor {name} not found")

    nav_fig = plot_quintile_nav_plotly(data["group_nav"], data["ls_nav"],
                                       title=f"{name} · Quintile NAV")
    bar_fig = plot_group_bar_plotly(data["group_metrics"], column="AnnReturn",
                                    title=f"{name} · Group Annualized Return")
    dd_fig = plot_drawdown_plotly(data["ls_returns"],
                                  title=f"{name} · Long-Short Drawdown")

    return templates.TemplateResponse(request, "backtest.html", {
        "title": f"{name} · Backtest",
        "factors": list_factors(),
        "name": name,
        "meta": data["meta"],
        "backtest_config": data["backtest_config"],
        "ls_metrics": _performance_card(data["group_metrics"], "LongShort"),
        "group_rows": _group_metrics_table(data["group_metrics"]),
        "nav_fig_json": fig_to_json(nav_fig),
        "bar_fig_json": fig_to_json(bar_fig),
        "dd_fig_json": fig_to_json(dd_fig),
    })


# ----------------------------- JSON API -----------------------------

@router.get("/api/factors")
def api_factors():
    factors = list_factors()
    metas = []
    for n in factors:
        d = load_factor(n)
        if d:
            metas.append(d["meta"])
    return JSONResponse(_sanitize({"count": len(factors), "factors": metas}))


@router.get("/api/factor/{name}/ic")
def api_factor_ic(name: str):
    d = load_factor(name)
    if not d:
        raise HTTPException(status_code=404, detail="Factor not found")
    ic: pd.Series = d["ic"]
    payload = {
        "name": name,
        "dates": [dt.strftime("%Y-%m-%d") for dt in ic.index],
        "ic":    ic.tolist(),
        "cum_ic": ic.cumsum().tolist(),
        "summary": d["ic_summary"],
    }
    return JSONResponse(_sanitize(payload))


@router.get("/api/factor/{name}/nav")
def api_factor_nav(name: str):
    d = load_factor(name)
    if not d:
        raise HTTPException(status_code=404, detail="Factor not found")
    group_nav: pd.DataFrame = d["group_nav"]
    ls_nav: pd.Series = d["ls_nav"]
    payload = {
        "name": name,
        "dates": [dt.strftime("%Y-%m-%d") for dt in group_nav.index],
        "groups": {col: group_nav[col].tolist() for col in group_nav.columns},
        "long_short": ls_nav.tolist(),
    }
    return JSONResponse(_sanitize(payload))


@router.get("/api/factor/{name}/summary")
def api_factor_summary(name: str):
    d = load_factor(name)
    if not d:
        raise HTTPException(status_code=404, detail="Factor not found")
    metrics = d["group_metrics"]
    payload = {
        "name": name,
        "meta": d["meta"],
        "ic_summary": d["ic_summary"],
        "backtest_config": d["backtest_config"],
        "group_metrics": {idx: row.to_dict() for idx, row in metrics.iterrows()} if not metrics.empty else {},
    }
    return JSONResponse(_sanitize(payload))


__all__ = ["router"]
