"""
静态图表（matplotlib + seaborn），深色金融仪表盘风格。
每个函数返回 matplotlib.figure.Figure，便于上层统一保存 / 内嵌。
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

matplotlib.use("Agg")  # 无 GUI 环境兼容（服务器/CI）

# ---- 深色主题 ----
_BG = "#0E1117"
_PANEL = "#1A1F2E"
_GRID = "#262B3A"
_TEXT = "#E8EAED"
_ACCENT = "#1E88E5"
_GREEN = "#00C853"
_RED = "#FF5252"
_AMBER = "#FFB300"
_CYAN = "#26C6DA"

_QUINTILE_PALETTE = ["#FF5252", "#FF9100", "#FFD740", "#69F0AE", "#00C853"]  # Q1->Q5 红->绿
_LS_COLOR = "#42A5F5"


def _apply_style(ax: plt.Axes, title: str = "", xlabel: str = "", ylabel: str = "") -> None:
    ax.set_facecolor(_PANEL)
    for spine in ax.spines.values():
        spine.set_color(_GRID)
    ax.tick_params(colors=_TEXT, which="both")
    ax.grid(True, color=_GRID, linewidth=0.5, alpha=0.7)
    if title:
        ax.set_title(title, color=_TEXT, fontsize=13, fontweight="bold", pad=12)
    if xlabel:
        ax.set_xlabel(xlabel, color=_TEXT)
    if ylabel:
        ax.set_ylabel(ylabel, color=_TEXT)


def _new_fig(figsize=(11, 5)) -> tuple[plt.Figure, plt.Axes]:
    fig, ax = plt.subplots(figsize=figsize, dpi=110)
    fig.patch.set_facecolor(_BG)
    return fig, ax


def plot_ic_series_mpl(ic: pd.Series, title: str = "IC Time Series") -> plt.Figure:
    """IC 柱状图 + 累计 IC 曲线（双 y 轴）。"""
    fig, ax = _new_fig(figsize=(12, 5))
    colors = np.where(ic.values >= 0, _GREEN, _RED)
    ax.bar(ic.index, ic.values, color=colors, alpha=0.7, width=1.2, label="Daily IC")
    _apply_style(ax, title=title, xlabel="Date", ylabel="IC")

    ax2 = ax.twinx()
    cum = ic.cumsum()
    ax2.plot(cum.index, cum.values, color=_AMBER, linewidth=2.0, label="Cumulative IC")
    ax2.set_ylabel("Cumulative IC", color=_AMBER)
    ax2.tick_params(axis="y", colors=_AMBER)
    ax2.spines["right"].set_color(_AMBER)

    # 均值参考线
    mu = ic.mean()
    ax.axhline(mu, color=_CYAN, linestyle="--", linewidth=1.0, alpha=0.8,
               label=f"Mean IC = {mu:.4f}")
    ax.legend(loc="upper left", facecolor=_PANEL, edgecolor=_GRID, labelcolor=_TEXT)
    fig.tight_layout()
    return fig


def plot_quintile_nav_mpl(group_nav: pd.DataFrame, ls_nav: pd.Series,
                          title: str = "Quintile Cumulative NAV") -> plt.Figure:
    """五分位累计净值 + Long-Short。"""
    fig, ax = _new_fig(figsize=(12, 5.5))
    cols = list(group_nav.columns)
    for i, col in enumerate(cols):
        ax.plot(group_nav.index, group_nav[col].values,
                color=_QUINTILE_PALETTE[i % len(_QUINTILE_PALETTE)],
                linewidth=1.6, label=col)
    ax.plot(ls_nav.index, ls_nav.values, color=_LS_COLOR, linewidth=2.2,
            linestyle="--", label="Long-Short")
    _apply_style(ax, title=title, xlabel="Date", ylabel="NAV (init = 1.0)")
    ax.axhline(1.0, color=_GRID, linewidth=0.8, alpha=0.6)
    ax.legend(loc="upper left", facecolor=_PANEL, edgecolor=_GRID, labelcolor=_TEXT, ncol=2)
    fig.tight_layout()
    return fig


def plot_group_bar_mpl(metrics_df: pd.DataFrame, column: str = "AnnReturn",
                       title: str | None = None) -> plt.Figure:
    """分组绩效柱状图（验证单调性）。不含 Long-Short 行。"""
    df = metrics_df.drop(index="LongShort", errors="ignore")
    vals = df[column].astype(float)
    colors = [_QUINTILE_PALETTE[i % len(_QUINTILE_PALETTE)] for i in range(len(vals))]
    fig, ax = _new_fig(figsize=(10, 5))
    ax.bar(vals.index, vals.values, color=colors, edgecolor=_GRID, linewidth=0.8)
    for i, (k, v) in enumerate(vals.items()):
        ax.text(i, v, f"{v:.2%}" if abs(v) < 10 else f"{v:.3f}",
                ha="center", va="bottom" if v >= 0 else "top", color=_TEXT, fontsize=10)
    _apply_style(ax, title=title or f"Quintile {column}", xlabel="Group", ylabel=column)
    fig.tight_layout()
    return fig


def plot_drawdown_mpl(daily_ret: pd.Series, title: str = "Long-Short Drawdown") -> plt.Figure:
    """回撤曲线。"""
    r = daily_ret.dropna()
    nav = (1.0 + r).cumprod()
    peak = nav.cummax()
    dd = nav / peak - 1.0
    fig, ax = _new_fig(figsize=(12, 4))
    ax.fill_between(dd.index, dd.values, 0, color=_RED, alpha=0.55)
    ax.plot(dd.index, dd.values, color=_RED, linewidth=1.2)
    _apply_style(ax, title=title, xlabel="Date", ylabel="Drawdown")
    fig.tight_layout()
    return fig


def save_fig(fig: plt.Figure, path: Path | str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(p, facecolor=fig.get_facecolor(), bbox_inches="tight", dpi=130)
    plt.close(fig)
    return p


__all__ = [
    "plot_ic_series_mpl",
    "plot_quintile_nav_mpl",
    "plot_group_bar_mpl",
    "plot_drawdown_mpl",
    "save_fig",
]

# 避免导入告警
_ = sns
