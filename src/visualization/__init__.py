"""可视化：matplotlib 静态图 + Plotly 交互图。"""
from src.visualization.plots_mpl import (
    plot_ic_series_mpl,
    plot_quintile_nav_mpl,
    plot_group_bar_mpl,
    plot_drawdown_mpl,
)
from src.visualization.plots_plotly import (
    plot_factor_coverage_plotly,
    plot_factor_latest_distribution_plotly,
    plot_group_diagnostics_plotly,
    plot_group_monotonicity_plotly,
    plot_ic_series_plotly,
    plot_ic_distribution_plotly,
    plot_ic_monthly_heatmap_plotly,
    plot_ic_rolling_plotly,
    plot_quintile_nav_plotly,
    plot_return_distribution_plotly,
    plot_group_bar_plotly,
    plot_drawdown_plotly,
    fig_to_json,
)

__all__ = [
    "plot_ic_series_mpl",
    "plot_quintile_nav_mpl",
    "plot_group_bar_mpl",
    "plot_drawdown_mpl",
    "plot_factor_coverage_plotly",
    "plot_factor_latest_distribution_plotly",
    "plot_group_diagnostics_plotly",
    "plot_group_monotonicity_plotly",
    "plot_ic_series_plotly",
    "plot_ic_distribution_plotly",
    "plot_ic_monthly_heatmap_plotly",
    "plot_ic_rolling_plotly",
    "plot_quintile_nav_plotly",
    "plot_return_distribution_plotly",
    "plot_group_bar_plotly",
    "plot_drawdown_plotly",
    "fig_to_json",
]
