#!/usr/bin/env python
"""
Multi-Factor Quant pipeline 一键脚本（支持多股票池）。

步骤（对每个启用的股票池循环执行）：
  1. 抓取股票池成分股（本地缓存）
  2. 下载日线 OHLCV（本地 Parquet 缓存）
  3. 构建宽表（adj_close / returns / sector ...）
  4. 对每个启用因子：
     a. 计算因子值
     b. 预处理（MAD 去极值 + 横截面 Z-score）
     c. 计算 IC 时序 + 汇总指标
     d. 五分位分组回测（Quintile + Long-Short）
     e. 生成 matplotlib 静态图 + Plotly 交互图 JSON
     f. 持久化到 outputs/universes/<UNIVERSE>/factors/<FACTOR>/

用法：
    python scripts/run_mvp.py                       # 跑所有启用的池 + 启动 Web
    python scripts/run_mvp.py --no-web              # 不启动 Web
    python scripts/run_mvp.py --serve-only          # 只启 Web
    python scripts/run_mvp.py --only-universe MAG7  # 只跑 MAG7
    python scripts/run_mvp.py --universe 20         # smoke test：SP500 取前 20 只
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 保证工程根目录在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis import compute_ic, ic_summary  # noqa: E402
from src.backtest import quintile_backtest  # noqa: E402
from src.config import CONFIG  # noqa: E402
from src.data import build_wide_tables, get_universe  # noqa: E402
from src.factors import FACTOR_REGISTRY, get_factor  # noqa: E402
from src.preprocessing import preprocess_factor  # noqa: E402
from src.utils.logger import get_logger  # noqa: E402
from src.visualization.plots_mpl import (  # noqa: E402
    plot_drawdown_mpl,
    plot_group_bar_mpl,
    plot_ic_series_mpl,
    plot_quintile_nav_mpl,
    save_fig,
)
from src.webapp.results_store import factor_dir, save_factor_artifacts  # noqa: E402

log = get_logger("run_mvp")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run Multi-Factor Quant pipeline.")
    p.add_argument("--no-web", action="store_true", help="不启动 Web 服务，只跑计算")
    p.add_argument("--serve-only", action="store_true", help="跳过计算，直接启动 Web")
    p.add_argument("--force-refresh", action="store_true", help="强制刷新数据")
    p.add_argument("--update", action="store_true",
                   help="快捷指令：刷新到今天（等价于 --force-refresh + --no-web）")
    p.add_argument("--universe", type=int, default=None,
                   help="smoke test：仅取每个池的前 N 只股票")
    p.add_argument("--only-universe", default=None,
                   help="只跑指定股票池（如 MAG7 / SP500）")
    p.add_argument("--host", default=None, help="覆盖 Web host")
    p.add_argument("--port", type=int, default=None, help="覆盖 Web port")
    return p.parse_args()


def _enabled_universes() -> list[str]:
    """从配置读启用的股票池列表，兼容老版 universe.name 单池配置。"""
    universes = getattr(CONFIG, "universes", None)
    if universes is not None:
        try:
            lst = list(universes.enabled or [])
            if lst:
                return [str(u).upper() for u in lst]
        except AttributeError:
            pass
    # fallback 老配置
    return [str(CONFIG.universe.name).upper()]


def _min_stocks_for(n_universe: int) -> int:
    """根据池大小自适应 IC 最小截面样本：池小则降低门槛。"""
    base = int(CONFIG.ic_analysis.min_stocks)
    # 若池本身就比 base 小，强制设为池大小的一半（至少 3）
    if n_universe < base:
        return max(3, n_universe // 2)
    return base


def run_pipeline_for_universe(
    universe: str,
    universe_limit: int | None = None,
    force_refresh: bool = False,
) -> None:
    log.info("#" * 70)
    log.info("# Universe: %s", universe)
    log.info("#" * 70)

    uni = get_universe(name=universe, force_refresh=force_refresh)
    if universe_limit:
        uni = uni.head(universe_limit)
    tickers = uni["ticker"].tolist()
    log.info("[%s] Universe size: %d tickers", universe, len(tickers))

    wide = build_wide_tables(tickers=tickers, universe=universe, force=force_refresh)
    adj_close = wide["adj_close"]
    returns = wide["returns"]
    log.info("[%s] adj_close shape=%s, returns shape=%s",
             universe, adj_close.shape, returns.shape)

    if adj_close.empty:
        log.error("[%s] No price data available, skip.", universe)
        return

    enabled = list(CONFIG.factors.enabled)
    log.info("[%s] Enabled factors: %s", universe, enabled)
    if not enabled:
        log.warning("No factors enabled in config. Exiting.")
        return

    min_stocks = _min_stocks_for(adj_close.shape[1])
    log.info("[%s] IC min_stocks (adaptive) = %d", universe, min_stocks)

    for fname in enabled:
        if fname not in FACTOR_REGISTRY:
            log.error("Factor '%s' not registered. Available: %s",
                      fname, sorted(FACTOR_REGISTRY.keys()))
            continue
        log.info("=" * 60)
        log.info("[%s] Processing factor: %s", universe, fname)
        factor = get_factor(fname)

        raw = factor.compute_from_wide(wide)
        log.info("Raw factor shape=%s, non-NaN coverage=%.2f%%",
                 raw.shape, 100 * raw.notna().mean().mean())

        clean = preprocess_factor(raw, sector_map=wide.get("sector"))
        log.info("Preprocessed shape=%s", clean.shape)

        ic = compute_ic(clean, returns, min_stocks=min_stocks)
        summary = ic_summary(ic)
        log.info("[%s] IC summary for %s: %s", universe, fname, summary)

        # 五分位回测：池太小则降低分组数，避免空组
        n_groups = int(CONFIG.backtest.n_groups)
        if adj_close.shape[1] < n_groups * 2:
            n_groups = max(2, min(n_groups, adj_close.shape[1] // 2))
            log.info("[%s] Reduced n_groups to %d (small universe)", universe, n_groups)

        result = quintile_backtest(
            clean, returns,
            factor_direction=factor.direction,
            n_groups=n_groups,
        )

        # 静态图（保留英文标题，避免 matplotlib 中文字体问题）
        fdir = factor_dir(fname, universe=universe)
        save_fig(plot_ic_series_mpl(ic, title=f"[{universe}] {fname} · IC Time Series"),
                 fdir / "ic_series.png")
        save_fig(plot_quintile_nav_mpl(result.group_nav, result.long_short_nav,
                                       title=f"[{universe}] {fname} · Quintile NAV"),
                 fdir / "quintile_nav.png")
        save_fig(plot_group_bar_mpl(result.group_metrics, column="AnnReturn",
                                    title=f"[{universe}] {fname} · Group Ann. Return"),
                 fdir / "group_bar.png")
        save_fig(plot_drawdown_mpl(result.long_short_returns,
                                   title=f"[{universe}] {fname} · Long-Short Drawdown"),
                 fdir / "drawdown.png")
        log.info("Static charts saved to %s", fdir)

        save_factor_artifacts(
            fname,
            meta=factor.to_meta(),
            ic=ic,
            ic_summary=summary,
            group_nav=result.group_nav,
            ls_nav=result.long_short_nav,
            ls_returns=result.long_short_returns,
            group_metrics=result.group_metrics,
            backtest_config=result.config,
            universe=universe,
        )
        log.info("Artifacts persisted for %s [%s]", fname, universe)

    log.info("=" * 60)
    log.info("[%s] Pipeline finished. Outputs in: outputs/universes/%s/factors/",
             universe, universe)


def run_pipeline(
    universe_limit: int | None = None,
    force_refresh: bool = False,
    only_universe: str | None = None,
) -> None:
    from src.utils.date_utils import resolve_date_range
    start_iso, end_iso, dynamic = resolve_date_range(
        CONFIG.date_range.start, CONFIG.date_range.end
    )
    log.info(
        "Date range: %s → %s  (start=%r  end=%r  dynamic=%s)",
        start_iso, end_iso, CONFIG.date_range.start, CONFIG.date_range.end, dynamic,
    )

    universes = _enabled_universes()
    if only_universe:
        only_universe = only_universe.upper()
        universes = [u for u in universes if u == only_universe] or [only_universe]
    log.info("Pipeline will run for universes: %s", universes)
    for uni in universes:
        try:
            run_pipeline_for_universe(uni, universe_limit=universe_limit, force_refresh=force_refresh)
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] Pipeline failed: %s", uni, e)


def serve_web(host: str | None = None, port: int | None = None) -> None:
    import uvicorn
    h = host or CONFIG.webapp.host
    p = int(port or CONFIG.webapp.port)
    reload = bool(CONFIG.webapp.reload)
    log.info("Starting FastAPI on http://%s:%d (手机浏览器可访问局域网 IP)", h, p)
    uvicorn.run("src.webapp.app:app", host=h, port=p, reload=reload)


def main() -> int:
    args = parse_args()
    # --update 是 --force-refresh + --no-web 的快捷写法
    if args.update:
        args.force_refresh = True
        args.no_web = True
    if not args.serve_only:
        run_pipeline(
            universe_limit=args.universe,
            force_refresh=args.force_refresh,
            only_universe=args.only_universe,
        )
    if not args.no_web:
        serve_web(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
