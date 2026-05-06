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

# 主题色
_BG = "#0E1117"
_PANEL = "#1A1F2E"
_GRID = "#262B3A"
_TEXT = "#E8EAED"
_GREEN = "#00C853"
_RED = "#FF5252"
_AMBER = "#FFB300"
_BLUE = "#42A5F5"
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


def plot_drawdown_plotly(daily_ret: pd.Series, title: str = "多空回撤") -> go.Figure:
    r = daily_ret.dropna()
    nav = (1.0 + r).cumprod()
    peak = nav.cummax()
    dd = nav / peak - 1.0
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
    "plot_quintile_nav_plotly",
    "plot_group_bar_plotly",
    "plot_drawdown_plotly",
    "fig_to_json",
]

_ = np  # suppress unused warning
