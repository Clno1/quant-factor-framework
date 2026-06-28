#!/usr/bin/env python
"""
Multi-Factor Quant pipeline 一键脚本（支持多股票池）。

读代码时先抓住一个核心事实：
  这个脚本不是“策略回测任务”的执行器，而是“离线研究产物”的生成器。
  它负责把原始行情、因子、IC、分组回测和置信评估全部算好，写到 outputs/。
  Web 页面大部分时候只是读取这些 outputs，不会在每次打开页面时重新计算因子。

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

推荐阅读顺序：
  main() -> run_pipeline() -> run_pipeline_for_universe()
  先不要展开每个 import 的细节，先看主流程，再回头看 src/data、src/factors、
  src/preprocessing、src/analysis、src/backtest 这些被调用的模块。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 直接运行 `python scripts/run_mvp.py` 时，Python 默认只把 scripts/
# 放进 import 搜索路径。这里手动把项目根目录加入 sys.path，
# 这样下面才能正常 import `src.*` 模块。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis import (  # noqa: E402
    build_factor_confidence,
    compute_ic,
    finalize_confidence_reports,
    ic_summary,
)
from src.backtest import double_sort_backtest, quintile_backtest  # noqa: E402
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
from src.webapp.results_store import (  # noqa: E402
    factor_dir,
    save_factor_artifacts,
    save_factor_confidence_artifacts,
    save_factor_values,
)

log = get_logger("run_mvp")


def parse_args() -> argparse.Namespace:
    """
    解析命令行参数。

    这个函数只负责把用户在终端输入的选项翻译成 args 对象，不做任何业务计算。
    例如：
      --serve-only  只启动网页，不重新计算因子/回测
      --update      强制刷新数据，并且不启动网页
      --universe 20 只取每个股票池前 20 只股票做快速烟测
    """
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
    """
    从配置中读出要跑哪些股票池。

    新配置使用：
      CONFIG.universes.enabled = ["SP500", "MAG7"]
    老配置只有：
      CONFIG.universe.name = "SP500"

    这里保留老配置 fallback，是为了让旧配置文件也能继续运行。
    """
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
    """
    决定每天计算 IC 时至少需要多少只有效股票。

    IC 是“同一天截面上，因子值和未来收益的相关性”。
    如果股票太少，这个相关性没有统计意义，所以配置里有 min_stocks。
    但 MAG7 只有 7 只股票，如果强行用 SP500 的门槛会一天都算不出来，
    所以小股票池会自动降低门槛。
    """
    base = int(CONFIG.ic_analysis.min_stocks)
    # 若池本身就比 base 小，强制设为池大小的一半（至少 3）
    if n_universe < base:
        return max(3, n_universe // 2)
    return base


def _factor_confidence_enabled() -> bool:
    """
    判断是否启用因子置信评估。

    新配置里有 CONFIG.factor_confidence.enabled。
    如果用户还在用旧配置，没有这个字段，则默认启用，保证新功能可见。
    """
    try:
        return bool(CONFIG.factor_confidence.enabled)
    except AttributeError:
        return True


def run_pipeline_for_universe(
    universe: str,
    universe_limit: int | None = None,
    force_refresh: bool = False,
) -> None:
    """
    跑完“一个股票池”的全部离线研究流程。

    参数：
      universe:
        股票池名称，例如 SP500 / MAG7。
      universe_limit:
        只取前 N 只股票，通常用于 smoke test，不用于正式研究。
      force_refresh:
        是否忽略本地缓存，重新拉取/重建行情数据。

    这个函数是本脚本最重要的函数。它的输出不是 return 值，而是写入：
      outputs/universes/<UNIVERSE>/factors/<FACTOR>/
    后续 Web 页面、策略融合、回测任务都会依赖这些产物。
    """
    log.info("#" * 70)
    log.info("# Universe: %s", universe)
    log.info("#" * 70)

    # 1) 取得股票池成分股。
    #    get_universe 会优先读缓存；force_refresh=True 时会重新抓取。
    uni = get_universe(name=universe, force_refresh=force_refresh)
    if universe_limit:
        uni = uni.head(universe_limit)
    tickers = uni["ticker"].tolist()
    log.info("[%s] Universe size: %d tickers", universe, len(tickers))

    # 2) 把每只股票的 OHLCV 日线行情整理成“宽表”：
    #      index = date
    #      columns = ticker
    #    wide["adj_close"]、wide["open"]、wide["returns"] 都是这种形状。
    #    后续因子、IC、回测都围绕这些宽表计算。
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

    # q-value/FDR 校正必须拿到“同一股票池内所有因子”的 p-value 后才能做，
    # 所以每个因子先生成 draft，循环结束后再统一 finalize。
    confidence_candidates = {}

    for fname in enabled:
        # 3) 从 FACTOR_REGISTRY 中取出具体因子对象。
        #    注册发生在 src/factors/__init__.py 导入各因子文件时。
        if fname not in FACTOR_REGISTRY:
            log.error("Factor '%s' not registered. Available: %s",
                      fname, sorted(FACTOR_REGISTRY.keys()))
            continue
        log.info("=" * 60)
        log.info("[%s] Processing factor: %s", universe, fname)
        factor = get_factor(fname)

        # 4) 计算原始因子值。
        #    输入是 wide 行情宽表；输出仍然是 date x ticker 的 DataFrame。
        #    例如 MOM_12M 会用 adj_close 计算 12 个月动量。
        raw = factor.compute_from_wide(wide)
        log.info("Raw factor shape=%s, non-NaN coverage=%.2f%%",
                 raw.shape, 100 * raw.notna().mean().mean())

        # 5) 预处理因子。
        #    典型步骤：去极值、横截面 Z-score、可选行业/市值中性化。
        #    后续 IC 和回测使用 clean，而不是 raw。
        clean = preprocess_factor(
            raw,
            sector_map=wide.get("sector"),
            mcap_df=wide.get("market_cap"),
        )
        log.info("Preprocessed shape=%s", clean.shape)

        # 落盘因子原始值矩阵（用于策略合成 composer 读取）
        save_factor_values(fname, clean, universe=universe)
        log.info("[%s] factor_values.parquet saved for %s", universe, fname)

        # 6) 计算单因子 IC。
        #    IC 使用 factor_t 对齐未来 N 日收益，避免使用未来信息。
        #    summary 是均值、标准差、IR、t 统计量等汇总。
        ic = compute_ic(clean, returns, min_stocks=min_stocks)
        summary = ic_summary(ic)
        log.info("[%s] IC summary for %s: %s", universe, fname, summary)

        # 五分位回测：池太小则降低分组数，避免空组
        n_groups = int(CONFIG.backtest.n_groups)
        if adj_close.shape[1] < n_groups * 2:
            n_groups = max(2, min(n_groups, adj_close.shape[1] // 2))
            log.info("[%s] Reduced n_groups to %d (small universe)", universe, n_groups)

        # 7) 跑单因子分组回测。
        #    把股票按因子值分成 Q1..Qn，观察最高组/最低组/多空组合表现。
        #    这里已经接入 next_open、逐票交易明细、手续费、滑点和可交易过滤。
        result = quintile_backtest(
            clean, returns,
            factor_direction=factor.direction,
            n_groups=n_groups,
            rebalance_mode=str(getattr(CONFIG.backtest, "rebalance_mode", "every_n_days")),
            open_df=wide.get("open"),
            price_df=wide.get("adj_close"),
            volume_df=wide.get("volume"),
        )

        # If point-in-time market cap is available, persist a size-controlled
        # independent double-sort diagnostic for robustness checks.
        mcap = wide.get("market_cap")
        if mcap is not None and not mcap.empty:
            # 8) 如果有市值矩阵，额外做双重排序稳健性检验：
            #    先按市值分层，再在每个市值层内看因子是否仍然有效。
            #    这可以粗略判断因子是不是只在大/小市值股票里有效。
            ds = double_sort_backtest(
                clean,
                mcap,
                returns,
                rebalance_mode=str(getattr(CONFIG.backtest, "rebalance_mode", "every_n_days")),
                open_df=wide.get("open"),
                price_df=wide.get("adj_close"),
                volume_df=wide.get("volume"),
            )
            fdir = factor_dir(fname, universe=universe)
            ds.factor_returns.to_frame("returns").to_parquet(
                fdir / "double_sort_returns.parquet"
            )
            ds.factor_nav.to_frame("nav").to_parquet(
                fdir / "double_sort_nav.parquet"
            )

        # 静态图（保留英文标题，避免 matplotlib 中文字体问题）
        # 9) 保存静态图片，供本地查看或未来报告使用。
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

        # 10) 保存该因子的核心研究产物。
        #     Web 首页、因子详情页、策略合成都会从这些文件读取结果。
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

        if _factor_confidence_enabled():
            try:
                # 11) 生成因子置信评估草稿。
                #     注意这里还没有 q-value，因为 q-value 需要同池所有因子一起校正。
                confidence_candidates[fname] = build_factor_confidence(
                    factor_name=fname,
                    ic=ic,
                    factor_values=clean,
                    group_metrics=result.group_metrics,
                    factor_direction=factor.direction,
                    execution_cost_bps_per_year=result.execution_cost_bps_per_year,
                )
                log.info("[%s] Confidence draft built for %s", universe, fname)
            except Exception as e:  # noqa: BLE001
                log.exception("[%s] Confidence evaluation failed for %s: %s", universe, fname, e)

    if _factor_confidence_enabled() and confidence_candidates:
        # 12) 同一个股票池内所有因子一起做 FDR 校正，得到 q-value。
        #     然后把最终 PASS/WATCH/FAIL、A/B/C/D、检查清单写入 outputs。
        finalized = finalize_confidence_reports(confidence_candidates)
        for fname, art in finalized.items():
            save_factor_confidence_artifacts(
                fname,
                report=art.report,
                checks=art.checks,
                rank_autocorr=art.rank_autocorr,
                quantile_turnover=art.quantile_turnover,
                universe=universe,
            )
            log.info(
                "[%s] Confidence saved for %s: verdict=%s grade=%s score=%.2f q=%s",
                universe,
                fname,
                art.report.get("verdict"),
                art.report.get("grade"),
                float(art.report.get("score") or 0.0),
                art.report.get("summary", {}).get("q_value"),
            )

    log.info("=" * 60)
    log.info("[%s] Pipeline finished. Outputs in: outputs/universes/%s/factors/",
             universe, universe)


def run_pipeline(
    universe_limit: int | None = None,
    force_refresh: bool = False,
    only_universe: str | None = None,
) -> None:
    """
    跑一个或多个股票池。

    这个函数负责“循环哪些 universe”，真正干活的是 run_pipeline_for_universe()。
    它还会解析动态日期，例如配置里的 start="5Y"、end="today"。
    """
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
    """
    启动 FastAPI Web 服务。

    只负责启动网页，不计算任何因子、不更新任何行情。
    网页读取的是 outputs/ 中已经生成好的产物。
    """
    import uvicorn
    h = host or CONFIG.webapp.host
    p = int(port or CONFIG.webapp.port)
    reload = bool(CONFIG.webapp.reload)
    log.info("Starting FastAPI on http://%s:%d (手机浏览器可访问局域网 IP)", h, p)
    uvicorn.run("src.webapp.app:app", host=h, port=p, reload=reload)


def main() -> int:
    """
    命令行入口。

    逻辑很简单：
      1. 解析参数
      2. 如果不是 --serve-only，就先跑离线 pipeline
      3. 如果不是 --no-web，就启动 Web
    """
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
