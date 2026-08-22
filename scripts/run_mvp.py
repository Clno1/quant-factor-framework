#!/usr/bin/env python
"""
Multi-Factor Quant pipeline 一键脚本（支持多股票池）。

读代码时先抓住一个核心事实：
  这个脚本不是“策略回测任务”的执行器，而是“离线研究产物”的生成器。
  它负责把原始行情、因子、IC、分组回测和置信评估全部算好，写到 outputs/。
  Web 页面大部分时候只是读取这些 outputs，不会在每次打开页面时重新计算因子。

步骤（对每个启用的股票池循环执行）：
  1. 绑定一个已经通过质量门禁的 DuckDB/Parquet 行情版本
  2. 读取该版本冻结的 OHLCV 与 PIT/成员快照
  3. 构建宽表（adj_close / returns / sector ...）
  4. 对每个启用因子：
     a. 计算因子值
     b. 预处理（MAD 去极值 + 行业/市值中性化 + 最终横截面 Z-score）
     c. 计算 IC 时序 + 汇总指标
     d. 五分位分组回测（Quintile + Long-Short）
     e. 生成 matplotlib 静态图 + Plotly 交互图 JSON
     f. 持久化到 outputs/universes/<UNIVERSE>/factors/<FACTOR>/

用法：
    python scripts/run_mvp.py                       # 跑所有启用的池 + 启动 Web
    python scripts/run_mvp.py --no-web              # 不启动 Web
    python scripts/run_mvp.py --serve-only          # 只启 Web
    python scripts/run_mvp.py --only-universe MAG7  # 只跑 MAG7
    python scripts/run_mvp.py --only-universe MAG7 --universe 5  # 静态池 smoke test

推荐阅读顺序：
  main() -> run_pipeline() -> run_pipeline_for_universe()
  先不要展开每个 import 的细节，先看主流程，再回头看 src/data、src/factors、
  src/preprocessing、src/analysis、src/backtest 这些被调用的模块。
"""
from __future__ import annotations

import argparse
from datetime import date
import sys
from pathlib import Path

import pandas as pd

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
from src.backtest.quintile import build_tradable_mask  # noqa: E402
from src.config import CONFIG  # noqa: E402
from src.data.access import load_published_bundle  # noqa: E402
from src.data.pit import (  # noqa: E402
    build_membership_mask,
    point_in_time_required,
)
from src.factors import FACTOR_REGISTRY, get_factor  # noqa: E402
from src.factors.artifacts import save_factor_matrix_bundle  # noqa: E402
from src.factors.publication import (  # noqa: E402
    dataset_version_provenance,
    publish_factor_research,
)
from src.preprocessing import preprocess_factor  # noqa: E402
from src.utils.file_lock import file_lock  # noqa: E402
from src.utils.io import atomic_save_json  # noqa: E402
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
)

log = get_logger("run_mvp")


def parse_args() -> argparse.Namespace:
    """
    解析命令行参数。

    这个函数只负责把用户在终端输入的选项翻译成 args 对象，不做任何业务计算。
    例如：
      --serve-only  只启动网页，不重新计算因子/回测
      --update      用最新已发布数据重算研究产物，并且不启动网页
      --universe 5  只截取静态股票池做快速烟测；动态 PIT 股票池禁止截断
    """
    p = argparse.ArgumentParser(description="Run Multi-Factor Quant pipeline.")
    p.add_argument("--no-web", action="store_true", help="不启动 Web 服务，只跑计算")
    p.add_argument("--serve-only", action="store_true", help="跳过计算，直接启动 Web")
    p.add_argument("--update", action="store_true",
                   help="快捷指令：用最新发布数据重算研究（等价于 --no-web）")
    p.add_argument("--universe", type=int, default=None,
                   help="smoke test：仅截取静态池前 N 只；动态 PIT 池禁止使用")
    p.add_argument("--only-universe", default=None,
                   help="只跑指定股票池（如 MAG7 / SP500）")
    p.add_argument("--host", default=None, help="覆盖 Web host")
    p.add_argument("--port", type=int, default=None, help="覆盖 Web port")
    return p.parse_args()


def _enabled_universes() -> list[str]:
    """
    从配置中读出要跑哪些股票池。

    正式研究池来自受版本控制的 typed registry。
    """
    from src.research_universes import research_universe_registry

    values = [
        entry.universe_id
        for entry in research_universe_registry().full_research_entries()
    ]
    if not values:
        raise ValueError("research universe registry cannot be empty")
    return values


def _min_stocks_for(n_universe: int, universe: str) -> int:
    """
    决定每天计算 IC 时至少需要多少只有效股票。

    IC 是“同一天截面上，因子值和未来收益的相关性”。
    如果股票太少，这个相关性没有统计意义，所以配置里有 min_stocks。
    但 MAG7 只有 7 只股票，如果强行用 SP500 的门槛会一天都算不出来，
    所以小股票池会自动降低门槛。
    """
    from src.research_universes import research_universe_registry

    entry = research_universe_registry().get(universe)
    base = max(
        int(CONFIG.ic_analysis.min_stocks),
        int(entry.minimum_cross_section),
    )
    # 若池本身就比 base 小，强制设为池大小的一半（至少 3）
    if n_universe < base:
        return max(3, n_universe // 2)
    return base


def _factor_confidence_enabled(universe: str) -> bool:
    """Return the required factor-confidence publication switch."""
    from src.research_universes import research_universe_registry

    return bool(
        CONFIG.factor_confidence.enabled
        and research_universe_registry().get(universe).confidence_enabled
    )


def _research_index(
    index: pd.Index,
    *,
    start: str | None,
    end: str | None,
) -> pd.DatetimeIndex:
    """Select the evaluation window while retaining earlier rows as warmup."""
    dates = pd.DatetimeIndex(index)
    lower = pd.Timestamp(start).normalize() if start else dates.min()
    upper = pd.Timestamp(end).normalize() if end else dates.max()
    selected = dates[(dates >= lower) & (dates <= upper)]
    if selected.empty:
        raise ValueError(
            f"Research window {lower.date()}..{upper.date()} has no market data"
        )
    return selected


def run_pipeline_for_universe(
    universe: str,
    universe_limit: int | None = None,
    *,
    dataset_version_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> None:
    """
    跑完“一个股票池”的全部离线研究流程。

    参数：
      universe:
        股票池名称，例如 SP500 / MAG7。
      universe_limit:
        只取前 N 只股票，通常用于 smoke test，不用于正式研究。
    这个函数是本脚本最重要的函数。它的输出不是 return 值，而是写入：
      outputs/universes/<UNIVERSE>/factors/<FACTOR>/
    后续 Web 页面、策略融合、回测任务都会依赖这些产物。
    """
    log.info("#" * 70)
    log.info("# Universe: %s", universe)
    log.info("#" * 70)

    pit_required = point_in_time_required(
        universe,
        strict=bool(
            getattr(
                CONFIG.backtest,
                "require_point_in_time_universe",
                False,
            )
        ),
    )
    membership_kwargs: dict = {}

    # 1) 取得与行情同版本的股票池和 PIT 快照。研究端不访问网络，
    #    因而不会把“今天的成分股”错误地配到“昨天的数据版本”上。
    enabled = list(CONFIG.factors.enabled)
    if not enabled:
        raise ValueError("No factors enabled in config")
    bundle = load_published_bundle(
        requested_universe=universe,
        start=start,
        end=end,
        exact_universe=pit_required,
        required_history_start=(
            getattr(CONFIG.universe.point_in_time, "main_factor_start", None)
            if pit_required
            else None
        ),
        factor_ids=enabled,
        dataset_version_id=dataset_version_id,
    )
    data_version = bundle.version
    data_provenance = dataset_version_provenance(data_version)
    all_version_members = bundle.universe
    current_members = all_version_members
    if "is_current_member" in current_members.columns:
        current_members = current_members.loc[
            current_members["is_current_member"].astype(bool)
        ]
    if universe_limit and pit_required:
        raise ValueError(
            f"Strict PIT universe {universe} cannot be published with "
            "--universe N because historical constituents would be truncated"
        )
    if universe_limit:
        tickers = current_members.head(universe_limit)["ticker"].astype(str).tolist()
    else:
        tickers = all_version_members["ticker"].astype(str).tolist()
    membership_kwargs = {
        "membership_override": bundle.membership,
        "membership_source": data_version.membership_path,
        "membership_source_sha256": data_version.membership_checksum_sha256,
    }
    log.info("[%s] Universe size: %d tickers", universe, len(tickers))

    # 2) 把每只股票的 OHLCV 日线行情整理成“宽表”：
    #      index = date
    #      columns = ticker
    #    wide["adj_close"]、wide["open"]、wide["returns"] 都是这种形状。
    #    后续因子、IC、回测都围绕这些宽表计算。
    wide = bundle.wide
    if universe_limit:
        for key, frame in list(wide.items()):
            if key in {"sector", "market_cap"}:
                wide[key] = frame.reindex(tickers)
            else:
                wide[key] = frame.reindex(columns=tickers)
    adj_close = wide["adj_close"]
    returns = wide["returns"]
    log.info("[%s] adj_close shape=%s, returns shape=%s",
             universe, adj_close.shape, returns.shape)

    if adj_close.empty:
        log.error("[%s] No price data available, skip.", universe)
        return
    analysis_index = _research_index(adj_close.index, start=start, end=end)
    log.info(
        "[%s] Evaluation window: %s -> %s (%d sessions); earlier rows are "
        "factor warmup only",
        universe,
        analysis_index.min().date(),
        analysis_index.max().date(),
        len(analysis_index),
    )

    membership_mask, pit_diagnostics = build_membership_mask(
        adj_close.index,
        adj_close.columns,
        universe,
        required=pit_required,
        **membership_kwargs,
    )
    if membership_mask is None:
        membership_mask = pd.DataFrame(
            True,
            index=adj_close.index,
            columns=adj_close.columns,
        )
    analysis_membership_mask = membership_mask.reindex(index=analysis_index)
    base_tradable_mask = build_tradable_mask(
        index=adj_close.index,
        columns=adj_close.columns,
        returns_df=returns,
        price_df=adj_close,
        open_df=wide.get("open"),
        volume_df=wide.get("volume"),
        timing="next_open",
    ) & membership_mask
    log.info(
        "[%s] PIT membership: %s",
        universe,
        pit_diagnostics.to_dict(),
    )

    log.info("[%s] Enabled factors: %s", universe, enabled)
    missing_factors = sorted(set(enabled) - set(FACTOR_REGISTRY))
    if missing_factors:
        raise ValueError(
            f"Enabled factors are not registered: {missing_factors}"
        )

    from src.research_universes import research_universe_registry

    research_universe = research_universe_registry().get(universe)
    min_stocks = _min_stocks_for(adj_close.shape[1], universe)
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
        raw = factor.compute_from_wide(wide).reindex(
            index=membership_mask.index,
            columns=membership_mask.columns,
        )
        raw = raw.where(membership_mask)
        raw = raw.reindex(index=analysis_index)
        log.info("Raw factor shape=%s, non-NaN coverage=%.2f%%",
                 raw.shape, 100 * raw.notna().mean().mean())
        # 5) 预处理因子。
        #    典型步骤：去极值、可选行业/市值中性化、最终横截面 Z-score。
        #    后续 IC 和回测使用 clean，而不是 raw。
        clean, preprocessing_audit = preprocess_factor(
            raw,
            sector_map=wide.get("sector"),
            mcap_df=wide.get("market_cap"),
            membership_mask=analysis_membership_mask,
            return_audit=True,
        )
        log.info("Preprocessed shape=%s", clean.shape)
        fdir = factor_dir(fname, universe=universe)
        preprocessing_audit_path = fdir / "preprocessing_audit.json"
        atomic_save_json(preprocessing_audit.to_dict(), preprocessing_audit_path)
        disappeared = list(
            preprocessing_audit.raw_non_null_clean_all_null_tickers
        )
        if disappeared:
            raise RuntimeError(
                f"[{universe}/{fname}] preprocessing removed every active raw "
                f"observation for tickers={disappeared[:50]}; audit="
                f"{preprocessing_audit_path}"
            )
        neutralization = preprocessing_audit.neutralization or {}
        industry_coverage = float(
            neutralization.get("industry_coverage", 1.0)
        )
        if (
            research_universe.confidence_enabled
            and bool(CONFIG.preprocessing.neutralize_industry)
            and industry_coverage
            < research_universe.minimum_industry_coverage
        ):
            raise RuntimeError(
                f"[{universe}/{fname}] industry coverage {industry_coverage:.2%} "
                f"is below the formal research gate "
                f"{research_universe.minimum_industry_coverage:.2%}; "
                f"audit={preprocessing_audit_path}"
            )

        # raw + clean 必须作为同一代原子发布；composer 会校验 manifest
        # 和两个文件的 SHA-256，拒绝新旧文件混用。
        save_factor_matrix_bundle(
            fname,
            raw=raw,
            clean=clean,
            universe=universe,
            provenance={
                "factor_module": factor.__class__.__module__,
                "factor_class": factor.__class__.__qualname__,
                "factor_parameters": dict(vars(factor)),
                "factor_direction": factor.direction,
                "research_universe": research_universe.to_dict(),
                "research_window": {
                    "start": analysis_index.min().date().isoformat(),
                    "end": analysis_index.max().date().isoformat(),
                    "sessions": len(analysis_index),
                },
                "preprocessing": dict(CONFIG.preprocessing),
                "point_in_time_universe": pit_diagnostics.to_dict(),
                "data_foundation": data_provenance,
                "preprocessing_audit": {
                    "path": str(preprocessing_audit_path),
                    "raw_non_null": preprocessing_audit.raw_non_null,
                    "clean_non_null": preprocessing_audit.clean_non_null,
                    "neutralization": preprocessing_audit.neutralization,
                },
            },
        )
        log.info(
            "[%s] verified raw/clean factor bundle saved for %s",
            universe,
            fname,
        )

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
            tradable_mask=base_tradable_mask,
            membership_mask=membership_mask,
            membership_events=bundle.membership_events,
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
                factor_direction=factor.direction,
                rebalance_mode=str(getattr(CONFIG.backtest, "rebalance_mode", "every_n_days")),
                open_df=wide.get("open"),
                price_df=wide.get("adj_close"),
                volume_df=wide.get("volume"),
                tradable_mask=base_tradable_mask,
                membership_mask=membership_mask,
                membership_events=bundle.membership_events,
            )
            ds.factor_returns.to_frame("returns").to_parquet(
                fdir / "double_sort_returns.parquet"
            )
            ds.factor_nav.to_frame("nav").to_parquet(
                fdir / "double_sort_nav.parquet"
            )

        # 静态图（保留英文标题，避免 matplotlib 中文字体问题）
        # 9) 保存静态图片，供本地查看或未来报告使用。
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

        if _factor_confidence_enabled(universe):
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

    if _factor_confidence_enabled(universe) and confidence_candidates:
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

    publication_path = publish_factor_research(
        universe=universe,
        version=data_version,
        factor_ids=enabled,
    )
    log.info(
        "[%s] Research publication committed for data version %s: %s",
        universe,
        data_version.version_id,
        publication_path,
    )

    log.info("=" * 60)
    log.info("[%s] Pipeline finished. Outputs in: outputs/universes/%s/factors/",
             universe, universe)


def run_pipeline(
    universe_limit: int | None = None,
    only_universe: str | None = None,
    dataset_version_ids: dict[str, str] | None = None,
    target_session: str | date | pd.Timestamp | None = None,
) -> list[str]:
    """
    跑一个或多个股票池。

    这个函数负责“循环哪些 universe”，真正干活的是 run_pipeline_for_universe()。
    它还会解析动态日期，例如配置里的 start="5Y"、end="today"。
    """
    from src.utils.date_utils import resolve_date_range
    start_iso, end_iso, dynamic = resolve_date_range(
        CONFIG.date_range.start, CONFIG.date_range.end
    )
    if target_session is not None:
        target = pd.Timestamp(target_session).normalize()
        if pd.isna(target):
            raise ValueError(f"Invalid research target session: {target_session}")
        end_iso = target.date().isoformat()
        if pd.Timestamp(start_iso).normalize() > target:
            raise ValueError(
                f"Research start {start_iso} is after target session {end_iso}"
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
    failures: list[str] = []
    for uni in universes:
        try:
            lock_path = (
                PROJECT_ROOT
                / "data"
                / "catalog"
                / f"factor-research-{uni}.lock"
            )
            with file_lock(lock_path):
                run_pipeline_for_universe(
                    uni,
                    universe_limit=universe_limit,
                    dataset_version_id=(dataset_version_ids or {}).get(uni),
                    start=start_iso,
                    end=end_iso,
                )
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] Pipeline failed: %s", uni, e)
            failures.append(uni)
    try:
        from src.data.foundation import MarketDataReader
        from src.research_universes.service import (
            publish_cross_universe_assessments,
        )

        cross_target = None
        if dataset_version_ids:
            sessions = [
                MarketDataReader()
                .require_version(universe, version_id)
                .target_session
                for universe, version_id in dataset_version_ids.items()
            ]
            if sessions:
                cross_target = max(sessions)
        cross_publication = publish_cross_universe_assessments(
            target_session=cross_target,
        )
        log.info(
            "Cross-universe assessment published: generation=%s target=%s "
            "verdicts=%s",
            cross_publication.get("generation_id"),
            cross_publication.get("target_session"),
            cross_publication.get("verdict_counts"),
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Cross-universe assessment publication failed: %s", exc)
        failures.append("CROSS_UNIVERSE")
    return failures


def serve_web(host: str | None = None, port: int | None = None) -> None:
    """
    启动 FastAPI Web 服务。

    只负责启动网页，不计算任何因子、不更新任何行情。
    网页读取的是 outputs/ 中已经生成好的产物。
    """
    import uvicorn
    from src.webapp.security import validate_web_exposure

    h = host or CONFIG.webapp.host
    p = int(port or CONFIG.webapp.port)
    reload = bool(CONFIG.webapp.reload)
    validate_web_exposure(h)
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
    # 数据摄取由 scripts/run_data_pipeline.py 负责；这里仅重算研究产物。
    if args.update:
        args.no_web = True
    if not args.serve_only:
        failures = run_pipeline(
            universe_limit=args.universe,
            only_universe=args.only_universe,
        )
        if failures:
            log.error("Pipeline failed for universes: %s", failures)
            return 1
    if not args.no_web:
        serve_web(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
