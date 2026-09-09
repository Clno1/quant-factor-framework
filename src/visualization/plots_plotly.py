"""
Plotly 交互图（Web 嵌入）。
每个函数返回 plotly.graph_objects.Figure，使用 fig_to_json 可转换为 HTML 嵌入。
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from plotly.utils import PlotlyJSONEncoder

from src.backtest.metrics import drawdown_series

# 主题色
_BG = "#0E1117"
_PANEL = "#1A1F2E"
_GRID = "#262B3A"
_TEXT = "#E8EAED"
_GREEN = "#00C853"
_RED = "#FF5252"
_AMBER = "#FFB300"
_BLUE = "#42A5F5"
_CYAN = "#26C6DA"
_PURPLE = "#AB47BC"
_QUINTILE_PALETTE = ["#FF5252", "#FF9100", "#FFD740", "#69F0AE", "#00C853"]


def _apply_layout(fig: go.Figure, title: str = "", height: int = 420) -> go.Figure:
    fig.update_layout(
        title=dict(text=title, font=dict(color=_TEXT, size=16)),
        paper_bgcolor=_BG,
        plot_bgcolor=_PANEL,
        font=dict(color=_TEXT, family="Roboto, Helvetica, Arial, sans-serif", size=12),
        legend=dict(bgcolor="rgba(0,0,0,0)", bordercolor=_GRID, borderwidth=0.5),
        margin=dict(l=40, r=40, t=60, b=40),
        height=height,
        hovermode="x unified",
    )
    fig.update_xaxes(gridcolor=_GRID, zerolinecolor=_GRID)
    fig.update_yaxes(gridcolor=_GRID, zerolinecolor=_GRID)
    return fig


def _empty_figure(title: str, message: str = "暂无足够数据", height: int = 360) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        text=message,
        showarrow=False,
        font=dict(color=_TEXT, size=14),
    )
    _apply_layout(fig, title=title, height=height)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


def _numeric_series(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()


def plot_ic_series_plotly(ic: pd.Series, title: str = "IC 时序") -> go.Figure:
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    colors = [_GREEN if v >= 0 else _RED for v in ic.values]
    fig.add_trace(
        go.Bar(x=ic.index, y=ic.values, marker_color=colors, name="日度 IC",
               opacity=0.75),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(x=ic.index, y=ic.cumsum().values, mode="lines",
                   line=dict(color=_AMBER, width=2.2), name="累计 IC"),
        secondary_y=True,
    )
    _apply_layout(fig, title=title, height=440)
    fig.update_yaxes(title_text="日度 IC", secondary_y=False)
    fig.update_yaxes(title_text="累计 IC", secondary_y=True, showgrid=False)
    return fig


def plot_ic_distribution_plotly(ic: pd.Series, title: str = "IC 分布") -> go.Figure:
    s = _numeric_series(ic)
    if s.empty:
        return _empty_figure(title)

    fig = go.Figure()
    fig.add_trace(go.Histogram(
        x=s.values,
        nbinsx=min(60, max(12, int(np.sqrt(len(s)) * 2))),
        histnorm="probability density",
        marker=dict(color=_BLUE, line=dict(color=_GRID, width=1)),
        opacity=0.78,
        name="IC 密度",
    ))
    fig.add_vline(x=0, line_color=_GRID, line_width=1)
    fig.add_vline(
        x=float(s.mean()),
        line_color=_AMBER,
        line_width=2,
        line_dash="dash",
        annotation_text=f"均值 {s.mean():.4f}",
        annotation_font_color=_AMBER,
    )
    _apply_layout(fig, title=title, height=360)
    fig.update_xaxes(title_text="IC")
    fig.update_yaxes(title_text="密度")
    return fig


def plot_ic_rolling_plotly(
    ic: pd.Series,
    windows: tuple[int, int] = (21, 63),
    title: str = "滚动 IC 稳定性",
) -> go.Figure:
    s = _numeric_series(ic)
    if s.empty:
        return _empty_figure(title)

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    colors = [_BLUE, _CYAN, _AMBER]
    for i, window in enumerate(windows):
        min_periods = max(5, int(window * 0.5))
        roll = s.rolling(window, min_periods=min_periods).mean()
        fig.add_trace(go.Scatter(
            x=roll.index,
            y=roll.values,
            mode="lines",
            line=dict(color=colors[i % len(colors)], width=2.0),
            name=f"{window}日滚动 IC",
        ), secondary_y=False)

    ir_window = max(windows)
    min_periods = max(10, int(ir_window * 0.5))
    roll_mean = s.rolling(ir_window, min_periods=min_periods).mean()
    roll_std = s.rolling(ir_window, min_periods=min_periods).std(ddof=1).replace(0, np.nan)
    roll_ir = roll_mean / roll_std
    fig.add_trace(go.Scatter(
        x=roll_ir.index,
        y=roll_ir.values,
        mode="lines",
        line=dict(color=_PURPLE, width=1.8, dash="dot"),
        name=f"{ir_window}日滚动 IC_IR",
    ), secondary_y=True)

    _apply_layout(fig, title=title, height=390)
    fig.add_hline(y=0, line_color=_GRID, line_width=1)
    fig.update_yaxes(title_text="滚动 IC 均值", secondary_y=False)
    fig.update_yaxes(title_text="滚动 IC_IR", secondary_y=True, showgrid=False)
    return fig


def plot_ic_monthly_heatmap_plotly(ic: pd.Series, title: str = "月度 IC 热力图") -> go.Figure:
    s = _numeric_series(ic)
    if s.empty:
        return _empty_figure(title)

    idx = pd.to_datetime(s.index, errors="coerce")
    s = pd.Series(s.values, index=idx).dropna()
    s = s[pd.notna(s.index)]
    if s.empty:
        return _empty_figure(title)

    monthly = s.groupby([s.index.year, s.index.month]).mean().unstack()
    monthly = monthly.reindex(columns=range(1, 13))
    month_labels = [f"{m}月" for m in range(1, 13)]
    z = monthly.values

    fig = go.Figure(go.Heatmap(
        x=month_labels,
        y=[str(y) for y in monthly.index],
        z=z,
        zmid=0,
        colorscale=[
            [0.00, _RED],
            [0.50, _PANEL],
            [1.00, _GREEN],
        ],
        colorbar=dict(title="月均 IC"),
        hovertemplate="%{y} %{x}<br>月均 IC=%{z:.4f}<extra></extra>",
    ))
    _apply_layout(fig, title=title, height=max(280, 58 * len(monthly.index) + 130))
    fig.update_xaxes(side="top")
    fig.update_yaxes(title_text="年份", autorange="reversed")
    return fig


def plot_quintile_nav_plotly(group_nav: pd.DataFrame, ls_nav: pd.Series,
                             title: str = "五分位累计净值") -> go.Figure:
    fig = go.Figure()
    for i, col in enumerate(group_nav.columns):
        fig.add_trace(go.Scatter(
            x=group_nav.index, y=group_nav[col].values, mode="lines",
            name=col,
            line=dict(color=_QUINTILE_PALETTE[i % len(_QUINTILE_PALETTE)], width=1.8),
        ))
    fig.add_trace(go.Scatter(
        x=ls_nav.index, y=ls_nav.values, mode="lines", name="多空组合",
        line=dict(color=_BLUE, width=2.4, dash="dash"),
    ))
    _apply_layout(fig, title=title, height=480)
    fig.update_yaxes(title_text="净值（初始 = 1.0）")
    fig.add_hline(y=1.0, line_color=_GRID, line_width=1)
    return fig


def plot_group_diagnostics_plotly(
    metrics_df: pd.DataFrame,
    title: str = "分组收益与风险调整收益",
) -> go.Figure:
    df = metrics_df.drop(index="LongShort", errors="ignore")
    if df.empty or "AnnReturn" not in df.columns:
        return _empty_figure(title)

    fig = make_subplots(
        rows=1,
        cols=2,
        subplot_titles=("年化收益", "Sharpe"),
        horizontal_spacing=0.12,
    )
    colors = [_QUINTILE_PALETTE[i % len(_QUINTILE_PALETTE)] for i in range(len(df))]
    ann = pd.to_numeric(df["AnnReturn"], errors="coerce")
    fig.add_trace(go.Bar(
        x=list(df.index),
        y=ann.values,
        marker_color=colors,
        text=[f"{v:.2%}" if pd.notna(v) else "" for v in ann.values],
        textposition="outside",
        name="年化收益",
    ), row=1, col=1)

    if "Sharpe" in df.columns:
        sharpe = pd.to_numeric(df["Sharpe"], errors="coerce")
        fig.add_trace(go.Bar(
            x=list(df.index),
            y=sharpe.values,
            marker_color=colors,
            text=[f"{v:.2f}" if pd.notna(v) else "" for v in sharpe.values],
            textposition="outside",
            name="Sharpe",
        ), row=1, col=2)

    _apply_layout(fig, title=title, height=390)
    fig.update_yaxes(title_text="年化收益", tickformat=".1%", row=1, col=1)
    fig.update_yaxes(title_text="Sharpe", row=1, col=2)
    fig.update_xaxes(title_text="分组")
    return fig


def plot_group_monotonicity_plotly(
    metrics_df: pd.DataFrame,
    column: str = "AnnReturn",
    title: str = "分组单调性检查",
) -> go.Figure:
    df = metrics_df.drop(index="LongShort", errors="ignore")
    if df.empty or column not in df.columns:
        return _empty_figure(title)

    y = pd.to_numeric(df[column], errors="coerce")
    ok = y.notna()
    y = y[ok]
    labels = list(df.index[ok])
    if len(y) < 2:
        return _empty_figure(title, "分组数量不足，无法计算单调性")

    x = np.arange(1, len(y) + 1, dtype=float)
    corr = pd.Series(x).corr(pd.Series(y.values), method="spearman")
    slope, intercept = np.polyfit(x, y.values, 1)
    trend = slope * x + intercept
    colors = [_QUINTILE_PALETTE[i % len(_QUINTILE_PALETTE)] for i in range(len(y))]
    value_text = [f"{v:.2%}" if column in {"AnnReturn", "AnnVol", "MaxDD", "WinRate", "AvgTurnover"} else f"{v:.3f}" for v in y.values]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=labels,
        y=y.values,
        marker_color=colors,
        text=value_text,
        textposition="outside",
        name=column,
    ))
    fig.add_trace(go.Scatter(
        x=labels,
        y=trend,
        mode="lines+markers",
        line=dict(color=_AMBER, width=2.2, dash="dash"),
        marker=dict(size=7),
        name=f"趋势线 · Spearman={corr:.2f}",
    ))
    _apply_layout(fig, title=f"{title} · Spearman={corr:.2f}", height=360)
    fig.update_xaxes(title_text="分组")
    if column in {"AnnReturn", "AnnVol", "MaxDD", "WinRate", "AvgTurnover"}:
        fig.update_yaxes(title_text=column, tickformat=".1%")
    else:
        fig.update_yaxes(title_text=column)
    fig.add_hline(y=0, line_color=_GRID, line_width=1)
    return fig


def plot_return_distribution_plotly(
    daily_ret: pd.Series,
    title: str = "多空日收益分布",
) -> go.Figure:
    s = _numeric_series(daily_ret)
    if s.empty:
        return _empty_figure(title)

    var5 = float(s.quantile(0.05))
    fig = go.Figure(go.Histogram(
        x=s.values,
        nbinsx=min(80, max(16, int(np.sqrt(len(s)) * 2))),
        marker=dict(color=_CYAN, line=dict(color=_GRID, width=1)),
        opacity=0.75,
        name="日收益",
    ))
    fig.add_vline(x=0, line_color=_GRID, line_width=1)
    fig.add_vline(
        x=float(s.mean()),
        line_color=_AMBER,
        line_dash="dash",
        annotation_text=f"均值 {s.mean():.2%}",
        annotation_font_color=_AMBER,
    )
    fig.add_vline(
        x=var5,
        line_color=_RED,
        line_dash="dot",
        annotation_text=f"5% VaR {var5:.2%}",
        annotation_font_color=_RED,
    )
    _apply_layout(fig, title=title, height=340)
    fig.update_xaxes(title_text="日收益", tickformat=".1%")
    fig.update_yaxes(title_text="频数")
    return fig


def plot_group_bar_plotly(metrics_df: pd.DataFrame, column: str = "AnnReturn",
                          title: str | None = None) -> go.Figure:
    df = metrics_df.drop(index="LongShort", errors="ignore")
    vals = df[column].astype(float)
    colors = [_QUINTILE_PALETTE[i % len(_QUINTILE_PALETTE)] for i in range(len(vals))]
    text = [f"{v:.2%}" if abs(v) < 10 else f"{v:.3f}" for v in vals.values]
    fig = go.Figure(go.Bar(
        x=list(vals.index), y=vals.values, marker_color=colors,
        text=text, textposition="outside",
    ))
    _COL_LABELS = {
        "AnnReturn": "年化收益",
        "AnnVol": "年化波动",
        "Sharpe": "Sharpe",
        "MaxDD": "最大回撤",
        "Calmar": "Calmar",
        "WinRate": "胜率",
    }
    y_label = _COL_LABELS.get(column, column)
    _apply_layout(fig, title=title or f"分组 {y_label}", height=380)
    fig.update_yaxes(title_text=y_label)
    fig.update_xaxes(title_text="分组")
    return fig


def plot_factor_coverage_plotly(
    factor_df: pd.DataFrame,
    title: str = "因子覆盖率与缺失率",
) -> go.Figure:
    if factor_df is None or factor_df.empty:
        return _empty_figure(title, "暂无因子矩阵，请先重跑 pipeline")

    f = factor_df.replace([np.inf, -np.inf], np.nan)
    total = max(len(f.columns), 1)
    valid_count = f.notna().sum(axis=1)
    missing_rate = 1.0 - valid_count / total

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(
        x=valid_count.index,
        y=valid_count.values,
        mode="lines",
        line=dict(color=_BLUE, width=2.0),
        name="有效股票数",
    ), secondary_y=False)
    fig.add_trace(go.Scatter(
        x=missing_rate.index,
        y=missing_rate.values,
        mode="lines",
        line=dict(color=_RED, width=1.6, dash="dot"),
        name="缺失率",
    ), secondary_y=True)
    _apply_layout(fig, title=title, height=380)
    fig.update_yaxes(title_text="有效股票数", secondary_y=False)
    fig.update_yaxes(title_text="缺失率", tickformat=".0%", secondary_y=True, showgrid=False)
    return fig


def plot_factor_latest_distribution_plotly(
    factor_df: pd.DataFrame,
    title: str = "最近一期因子横截面分布",
) -> go.Figure:
    if factor_df is None or factor_df.empty:
        return _empty_figure(title, "暂无因子矩阵，请先重跑 pipeline")

    f = factor_df.replace([np.inf, -np.inf], np.nan).dropna(how="all")
    if f.empty:
        return _empty_figure(title, "因子矩阵全为空")

    latest_date = f.index.max()
    s = _numeric_series(f.loc[latest_date])
    if s.empty:
        return _empty_figure(title, "最近一期没有有效因子值")

    fig = make_subplots(
        rows=1,
        cols=2,
        subplot_titles=("直方图", "箱线图"),
        column_widths=[0.68, 0.32],
        horizontal_spacing=0.10,
    )
    fig.add_trace(go.Histogram(
        x=s.values,
        nbinsx=min(60, max(12, int(np.sqrt(len(s)) * 2))),
        marker=dict(color=_BLUE, line=dict(color=_GRID, width=1)),
        opacity=0.78,
        name="因子值",
    ), row=1, col=1)
    fig.add_trace(go.Box(
        y=s.values,
        name="分布",
        marker_color=_CYAN,
        boxpoints="outliers",
    ), row=1, col=2)
    _apply_layout(fig, title=f"{title} · {pd.Timestamp(latest_date).date()}", height=380)
    fig.update_xaxes(title_text="因子值", row=1, col=1)
    fig.update_yaxes(title_text="频数", row=1, col=1)
    fig.update_yaxes(title_text="因子值", row=1, col=2)
    return fig


def plot_drawdown_plotly(daily_ret: pd.Series, title: str = "多空回撤") -> go.Figure:
    dd = drawdown_series(daily_ret)
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=dd.index, y=dd.values, mode="lines", fill="tozeroy",
        line=dict(color=_RED, width=1.3),
        fillcolor="rgba(255,82,82,0.35)",
        name="回撤",
    ))
    _apply_layout(fig, title=title, height=320)
    fig.update_yaxes(title_text="回撤", tickformat=".2%")
    return fig


def fig_to_json(fig: go.Figure) -> str:
    """将 Figure 转为 plotly.js 可直接消费的 JSON 字符串。"""
    return json.dumps(fig, cls=PlotlyJSONEncoder)


__all__ = [
    "plot_ic_series_plotly",
    "plot_ic_distribution_plotly",
    "plot_ic_rolling_plotly",
    "plot_ic_monthly_heatmap_plotly",
    "plot_quintile_nav_plotly",
    "plot_group_diagnostics_plotly",
    "plot_group_monotonicity_plotly",
    "plot_return_distribution_plotly",
    "plot_group_bar_plotly",
    "plot_factor_coverage_plotly",
    "plot_factor_latest_distribution_plotly",
    "plot_drawdown_plotly",
    "fig_to_json",
]

_ = np  # suppress unused warning
