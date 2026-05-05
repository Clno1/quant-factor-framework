"""可视化：matplotlib 静态图 + Plotly 交互图。"""
from src.visualization.plots_mpl import (
    plot_ic_series_mpl,
    plot_quintile_nav_mpl,
    plot_group_bar_mpl,
    plot_drawdown_mpl,
)
from src.visualization.plots_plotly import (
    plot_ic_series_plotly,
    plot_quintile_nav_plotly,
    plot_group_bar_plotly,
    plot_drawdown_plotly,
    fig_to_json,
)

__all__ = [
    "plot_ic_series_mpl",
    "plot_quintile_nav_mpl",
    "plot_group_bar_mpl",
    "plot_drawdown_mpl",
    "plot_ic_series_plotly",
    "plot_quintile_nav_plotly",
    "plot_group_bar_plotly",
    "plot_drawdown_plotly",
    "fig_to_json",
]
