"""
路由定义。

页面（统一支持 ?universe=XXX 切换股票池）：
  GET /                     首页（策略概览）
  GET /factor/{name}        因子详情页
  GET /backtest             回测结果页
  GET /backtest/{name}      回测结果页（指定因子）

JSON API：
  GET /api/universes
  GET /api/factors
  GET /api/factor/{name}/ic
  GET /api/factor/{name}/nav
  GET /api/factor/{name}/summary
"""
from __future__ import annotations

import json

import math
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.config import CONFIG
from src.utils.identifiers import (
    InvalidResourceId,
    safe_path_component,
)
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
    if x is None:
        return "—"
    try:
        value = float(x)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(value) or math.isinf(value):
        return "—"
    return f"{value * 100:.{digits}f}%"


def _format_num(x, digits: int = 4) -> str:
    if x is None:
        return "—"
    try:
        value = float(x)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(value) or math.isinf(value):
        return "—"
    return f"{value:.{digits}f}"


def _status_label(status: str | None) -> str:
    return {
        "PASS": "通过",
        "WATCH": "观察",
        "FAIL": "拒绝",
    }.get(str(status or "").upper(), "未生成")


def _status_badge_class(status: str | None) -> str:
    return {
        "PASS": "green",
        "WATCH": "amber",
        "FAIL": "red",
    }.get(str(status or "").upper(), "")


def _category_label(category: str | None) -> str:
    return {
        "predictive": "预测力",
        "stability": "稳定性",
        "economic": "经济意义",
        "tradability": "可交易性",
        "data_quality": "数据质量",
    }.get(str(category or ""), str(category or "—"))


def _format_check_value(check_id: str, value: Any, unit: str = "") -> str:
    x = _format_num(value, 4)
    if x == "—":
        return x
    if unit == "bps":
        return f"{float(value):.1f} bps"
    if check_id in {
        "ic_positive_pct",
        "monthly_positive_pct",
        "rolling_positive_pct_63d",
        "subperiod_positive_pct",
        "top_quantile_turnover_avg",
        "avg_coverage",
        "latest_coverage",
        "zero_std_pct",
        "long_short_ann_return",
    }:
        return _format_pct(float(value), 2)
    if check_id in {"p_value", "q_value"}:
        return _format_num(value, 5)
    if check_id == "n_obs":
        return f"{int(float(value)):,}"
    return x


def _confidence_brief(conf: dict | None, checks_df: pd.DataFrame | None = None) -> dict:
    if not conf:
        return {
            "available": False,
            "verdict": "未生成",
            "verdict_class": "",
            "grade": "—",
            "score": "—",
            "q_value": "—",
            "passed": "—",
        }
    summary = conf.get("summary") or {}
    checks_total = 0
    checks_passed = 0
    if checks_df is not None and not checks_df.empty:
        checks_total = int(len(checks_df))
        checks_passed = int((checks_df["status"] == "PASS").sum()) if "status" in checks_df else 0
    return {
        "available": True,
        "verdict": _status_label(conf.get("verdict")),
        "verdict_class": _status_badge_class(conf.get("verdict")),
        "grade": conf.get("grade") or "—",
        "score": _format_num(conf.get("score"), 1),
        "q_value": _format_num(summary.get("q_value"), 5),
        "p_value": _format_num(summary.get("p_value"), 5),
        "passed": f"{checks_passed}/{checks_total}" if checks_total else "—",
    }


def _confidence_detail(conf: dict | None, checks_df: pd.DataFrame | None) -> dict:
    brief = _confidence_brief(conf, checks_df)
    if not conf:
        return {**brief, "summary": {}, "category_scores": [], "checks": []}

    summary = conf.get("summary") or {}
    category_scores = []
    for category, score in (conf.get("category_scores") or {}).items():
        category_scores.append({
            "category": category,
            "label": _category_label(category),
            "score": _format_num(score, 1),
        })

    checks = []
    if checks_df is not None and not checks_df.empty:
        for row in checks_df.to_dict(orient="records"):
            check_id = str(row.get("check_id") or "")
            unit = str(row.get("unit") or "")
            checks.append({
                "category": _category_label(row.get("category")),
                "label": row.get("label") or check_id,
                "value": _format_check_value(check_id, row.get("value"), unit),
                "threshold": (
                    f"{row.get('operator', '')} "
                    f"{_format_check_value(check_id, row.get('pass_threshold'), unit)}"
                ),
                "watch_threshold": _format_check_value(check_id, row.get("watch_threshold"), unit),
                "status": _status_label(row.get("status")),
                "status_class": _status_badge_class(row.get("status")),
            })

    return {
        **brief,
        "summary": {
            "ic_mean": _format_num(summary.get("ic_mean"), 4),
            "ic_mean_raw": _format_num(summary.get("ic_mean_raw"), 4),
            "ic_ir": _format_num(summary.get("ic_ir"), 4),
            "t_stat": _format_num(summary.get("t_stat"), 3),
            "p_value": _format_num(summary.get("p_value"), 5),
            "q_value": _format_num(summary.get("q_value"), 5),
            "ci95": (
                f"{_format_num(summary.get('ci95_low'), 4)} / "
                f"{_format_num(summary.get('ci95_high'), 4)}"
            ),
            "monthly_positive_pct": _format_pct(summary.get("monthly_positive_pct")),
            "monotonic_corr": _format_num(summary.get("monotonic_corr"), 3),
            "rank_autocorr_median": _format_num(summary.get("rank_autocorr_median"), 3),
            "top_quantile_turnover_avg": _format_pct(summary.get("top_quantile_turnover_avg")),
            "avg_coverage": _format_pct(summary.get("avg_coverage")),
        },
        "category_scores": category_scores,
        "checks": checks,
    }


def _json_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def _ic_outcome_detail(
    ic_summary: dict[str, Any],
    audit: pd.DataFrame | None,
) -> dict[str, Any]:
    summary = ic_summary.get("forward_outcomes") or {}
    frame = audit if audit is not None else pd.DataFrame()
    cross_sections = (
        frame.loc[frame.get("record_type").eq("IC_CROSS_SECTION")]
        if not frame.empty and "record_type" in frame.columns
        else pd.DataFrame()
    )
    selectively_censored = (
        int(cross_sections.get("status", pd.Series(dtype=str)).eq("selectively_censored").sum())
        if not cross_sections.empty
        else int(ic_summary.get("censored_dates") or 0)
    )
    security = (
        frame.loc[frame.get("record_type").eq("SECURITY_OUTCOME")].copy()
        if not frame.empty and "record_type" in frame.columns
        else pd.DataFrame()
    )
    exceptional = (
        security.loc[
            security.get("status", pd.Series(index=security.index, dtype=str)).isin(
                {
                    "RESOLVED_REVIEWED_EVENT",
                    "UNRESOLVED_MISSING_OUTCOME",
                    "UNRESOLVED_EVENT_SETTLEMENT",
                }
            )
        ]
        if not security.empty
        else pd.DataFrame()
    )
    display_rows: list[dict[str, Any]] = []
    for row in _json_records(exceptional.tail(50)):
        display_rows.append(
            {
                "decision_date": str(row.get("decision_date") or "")[:10],
                "horizon_date": str(row.get("horizon_date") or "")[:10] or "—",
                "ticker": row.get("ticker") or "—",
                "status": row.get("status") or "—",
                "settlement_method": row.get("settlement_method") or "—",
                "outcome": _format_pct(row.get("outcome")),
                "event_reason": row.get("event_reason") or "—",
            }
        )
    return {
        "available": bool(summary),
        "ordinary_complete": int(summary.get("ordinary_complete") or 0),
        "resolved_events": int(summary.get("resolved_reviewed_events") or 0),
        "right_edge": int(summary.get("right_edge_not_observable") or 0),
        "unresolved": int(summary.get("unresolved_missing_outcomes") or 0),
        "selectively_censored_dates": selectively_censored,
        "hac_lags": int(ic_summary.get("HAC_lags") or 0),
        "rows": display_rows,
    }


def _resolve_universe(requested: str | None) -> str:
    """规范化 universe 参数：未指定则取注册表中的 PRIMARY。"""
    if requested:
        try:
            return safe_path_component(
                requested.upper(),
                label="universe",
            )
        except InvalidResourceId as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    from src.research_universes.registry import research_universe_registry

    return safe_path_component(
        research_universe_registry().primary().universe_id,
        label="universe",
    )


def _ic_summary_rows(universe: str) -> list[dict]:
    rows = []
    for name in list_factors(universe=universe):
        data = load_factor(name, universe=universe)
        if not data:
            continue
        s = data["ic_summary"]
        confidence = _confidence_brief(
            data.get("confidence"),
            data.get("confidence_checks"),
        )
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
            "confidence": confidence,
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
    return RedirectResponse(url="/research", status_code=307)


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
        "confidence": _confidence_detail(
            data.get("confidence"),
            data.get("confidence_checks"),
        ),
        "outcome_audit": _ic_outcome_detail(
            data["ic_summary"],
            data.get("ic_outcome_audit"),
        ),
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
        "confidence": d.get("confidence"),
        "backtest_config": d["backtest_config"],
        "group_metrics": {idx: row.to_dict() for idx, row in metrics.iterrows()} if not metrics.empty else {},
    }
    return JSONResponse(_sanitize(payload))


@router.get("/api/factor/{name}/confidence")
def api_factor_confidence(name: str, universe: str | None = Query(None)):
    universe = _resolve_universe(universe)
    d = load_factor(name, universe=universe)
    if not d:
        raise HTTPException(status_code=404, detail="Factor not found")
    checks = d.get("confidence_checks")
    payload = {
        "universe": universe,
        "name": name,
        "confidence": d.get("confidence"),
        "checks": checks.to_dict(orient="records") if checks is not None and not checks.empty else [],
    }
    return JSONResponse(_sanitize(payload))


@router.get("/api/factor/{name}/outcomes")
def api_factor_outcomes(
    name: str,
    universe: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(500, ge=1, le=5000),
):
    universe = _resolve_universe(universe)
    data = load_factor(name, universe=universe)
    if not data:
        raise HTTPException(status_code=404, detail="Factor not found")
    audit = data.get("ic_outcome_audit")
    frame = audit.copy() if isinstance(audit, pd.DataFrame) else pd.DataFrame()
    if status and not frame.empty:
        if "status" not in frame.columns:
            frame = frame.iloc[0:0]
        else:
            frame = frame.loc[
                frame["status"].fillna("").astype(str).str.upper().eq(status.upper())
            ]
    total = len(frame)
    records = _json_records(frame.head(limit))
    return JSONResponse(
        _sanitize(
            {
                "universe": universe,
                "name": name,
                "summary": data["ic_summary"].get("forward_outcomes") or {},
                "censoring": {
                    "dates": data["ic_summary"].get("censored_dates", 0),
                    "observations": data["ic_summary"].get(
                        "censored_observations", 0
                    ),
                },
                "total": total,
                "limit": limit,
                "records": records,
            }
        )
    )


__all__ = ["router"]
