#!/usr/bin/env python
"""
Multi-Factor Quant MVP pipeline 一键脚本。

步骤：
  1. 抓取 S&P 500 成分股（本地缓存）
  2. 下载日线 OHLCV（多线程，本地 Parquet 缓存）
  3. 构建宽表（adj_close / returns / sector ...）
  4. 对每个启用因子：
     a. 计算因子值
     b. 预处理（MAD 去极值 + 横截面 Z-score）
     c. 计算 IC 时序 + 汇总指标
     d. 五分位分组回测（Quintile + Long-Short）
     e. 生成 matplotlib 静态图 + Plotly 交互图 JSON
     f. 持久化到 outputs/factors/<name>/

用法：
    python scripts/run_mvp.py              # 完整 pipeline
    python scripts/run_mvp.py --no-web     # 不启动 Web 服务
    python scripts/run_mvp.py --serve-only # 跳过计算，直接启动 Web
    python scripts/run_mvp.py --universe 20   # 仅用前 20 只股票做 smoke test
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
    p = argparse.ArgumentParser(description="Run Multi-Factor Quant MVP pipeline.")
    p.add_argument("--no-web", action="store_true", help="不启动 Web 服务，只跑计算")
    p.add_argument("--serve-only", action="store_true", help="跳过计算，直接启动 Web")
    p.add_argument("--force-refresh", action="store_true", help="强制刷新股票池与数据")
    p.add_argument("--universe", type=int, default=None,
                   help="仅取前 N 只股票做 smoke test")
    p.add_argument("--host", default=None, help="覆盖 Web host")
    p.add_argument("--port", type=int, default=None, help="覆盖 Web port")
    return p.parse_args()


def run_pipeline(universe_limit: int | None = None, force_refresh: bool = False) -> None:
    # --- 1. 股票池 ---
    uni = get_universe(force_refresh=force_refresh)
    if universe_limit:
        uni = uni.head(universe_limit)
    tickers = uni["ticker"].tolist()
    log.info("Universe: %d tickers", len(tickers))

    # --- 2/3. 下载 & 构造宽表 ---
    wide = build_wide_tables(tickers=tickers, force=force_refresh)
    adj_close = wide["adj_close"]
    returns = wide["returns"]
    log.info("adj_close shape=%s, returns shape=%s", adj_close.shape, returns.shape)

    enabled = list(CONFIG.factors.enabled)
    log.info("Enabled factors: %s", enabled)
    if not enabled:
        log.warning("No factors enabled in config. Exiting.")
        return

    for fname in enabled:
        if fname not in FACTOR_REGISTRY:
            log.error("Factor '%s' not registered. Available: %s",
                      fname, sorted(FACTOR_REGISTRY.keys()))
            continue
        log.info("=" * 60)
        log.info("Processing factor: %s", fname)
        factor = get_factor(fname)

        # 4a. 计算因子
        raw = factor.compute(adj_close)
        log.info("Raw factor shape=%s, non-NaN coverage=%.2f%%",
                 raw.shape, 100 * raw.notna().mean().mean())

        # 4b. 预处理
        clean = preprocess_factor(raw, sector_map=wide.get("sector"))
        log.info("Preprocessed shape=%s", clean.shape)

        # 4c. IC
        ic = compute_ic(clean, returns)
        summary = ic_summary(ic)
        log.info("IC summary for %s: %s", fname, summary)

        # 4d. 回测
        result = quintile_backtest(clean, returns, factor_direction=factor.direction)

        # 4e. 静态图（matplotlib）
        fdir = factor_dir(fname)
        save_fig(plot_ic_series_mpl(ic, title=f"{fname} · IC Time Series"),
                 fdir / "ic_series.png")
        save_fig(plot_quintile_nav_mpl(result.group_nav, result.long_short_nav,
                                       title=f"{fname} · Quintile NAV"),
                 fdir / "quintile_nav.png")
        save_fig(plot_group_bar_mpl(result.group_metrics, column="AnnReturn",
                                    title=f"{fname} · Group Ann. Return"),
                 fdir / "group_bar.png")
        save_fig(plot_drawdown_mpl(result.long_short_returns,
                                   title=f"{fname} · Long-Short Drawdown"),
                 fdir / "drawdown.png")
        log.info("Static charts saved to %s", fdir)

        # 4f. 持久化 artifacts
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
        )
        log.info("Artifacts persisted for %s", fname)

    log.info("=" * 60)
    log.info("Pipeline finished successfully. Outputs in: outputs/factors/")


def serve_web(host: str | None = None, port: int | None = None) -> None:
    import uvicorn
    h = host or CONFIG.webapp.host
    p = int(port or CONFIG.webapp.port)
    reload = bool(CONFIG.webapp.reload)
    log.info("Starting FastAPI on http://%s:%d (手机浏览器可访问局域网 IP)", h, p)
    uvicorn.run("src.webapp.app:app", host=h, port=p, reload=reload)


def main() -> int:
    args = parse_args()
    if not args.serve_only:
        run_pipeline(universe_limit=args.universe, force_refresh=args.force_refresh)
    if not args.no_web:
        serve_web(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
