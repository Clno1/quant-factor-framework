"""
五分位分组回测（Quintile Analysis）。

核心流程（向量化）：
  1. 每隔 rebalance_days 取一个"调仓日"，在调仓日按因子值把股票分为 Q1..QN 组
  2. 组合持仓在下一个调仓日前保持不变（等权）
  3. 组合日收益 = 当日各持仓股票收益的等权平均
  4. Long-Short 组合 = QN - Q1（按因子方向自动调整）
  5. 计算换手率、累计净值、绩效指标

成交模型（execution）：
  - timing="next_open"（推荐）：T 日打分 → T+1 开盘价成交 → 持有期日收益用 open-to-open。
                              **更接近实盘**：决策完到下一个交易日开盘才动手。
  - 调仓日按逐票交易权重估算成交数量，再由共享的 FeeModel / SlippageModel
    计算成交价、滑点成本、券商佣金和监管费用，最后汇总扣到组收益。

防前视偏差：
  - next_open 模式：T 日因子 → T+1 开盘买入 → 区间 [T+1 open, T+2 open) 收益归当日。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from src.backtest.metrics import performance_summary, relative_performance_summary
from src.backtest.rebalance import get_rebalance_dates
from src.config import CONFIG
from src.execution import (
    calculate_execution,
    max_volume_fill_quantity,
    resolve_execution_config,
)
from src.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class QuintileResult:
    """五分位回测输出容器。"""
    group_daily_returns: pd.DataFrame   # date x [Q1..QN]，每组日收益（已扣摩擦）
    long_short_returns: pd.Series       # date，Long-Short 日收益
    group_nav: pd.DataFrame             # 净值曲线（以 1.0 为初值）
    long_short_nav: pd.Series
    turnover: pd.DataFrame              # 每个调仓日的各组换手率（新旧持仓差集 / 持仓数）
    group_metrics: pd.DataFrame         # 每组绩效指标 + Long-Short
    group_assignment: pd.DataFrame      # date x ticker，每日所在组号（NaN 表示当日未分组）
    holdings_detail: pd.DataFrame = field(default_factory=pd.DataFrame)
    trades_detail: pd.DataFrame = field(default_factory=pd.DataFrame)
    costs_detail: pd.DataFrame = field(default_factory=pd.DataFrame)
    benchmark_returns: pd.Series = field(default_factory=pd.Series)
    benchmark_nav: pd.Series = field(default_factory=pd.Series)
    excess_returns: pd.Series = field(default_factory=pd.Series)
    config: dict = field(default_factory=dict)
    execution_cost_bps_per_year: dict = field(default_factory=dict)  # 各组年化摩擦成本 bps
    gross_group_returns: pd.DataFrame = field(default_factory=pd.DataFrame)
    cost_returns: pd.DataFrame = field(default_factory=pd.DataFrame)
    effective_returns: pd.DataFrame = field(default_factory=pd.DataFrame)
    held_assignment: pd.DataFrame = field(default_factory=pd.DataFrame)
    tradable_mask: pd.DataFrame = field(default_factory=pd.DataFrame)
    rebalance_dates: pd.DatetimeIndex = field(
        default_factory=lambda: pd.DatetimeIndex([])
    )


def _compute_open_to_open_returns(open_df: pd.DataFrame) -> pd.DataFrame:
    """
    open-to-open 日收益：r_t = open_{t+1} / open_t - 1，登记到日期 t+1。

    与 close-to-close 对齐：
      r_t 的语义是「持有 [t-1 close, t close]」 的收益。
      open-to-open 把它替换成「持有 [t-1 open, t open]」 的收益，
      但日期标签仍然挂在 t（"今天的收益"）以保持外部接口一致。

    输入  : date × ticker 的复权开盘价
    输出 : date × ticker 的开盘价收益（首日 NaN）
    """
    if open_df is None or open_df.empty:
        return pd.DataFrame()
    return open_df.pct_change(fill_method=None)


def _resolve_execution(execution: dict | None) -> dict:
    """规范化 execution 参数，缺失字段从 CONFIG 兜底。"""
    out = resolve_execution_config(execution or {})
    if out["timing"] != "next_open":
        raise ValueError(
            "Only execution.timing='next_open' is supported. Same-day close "
            "fills are disabled because a close-derived signal cannot trade at "
            "that already-observed close."
        )
    return out


def _safe_lookup(df: pd.DataFrame | None, dt: pd.Timestamp, ticker: str) -> float | None:
    if df is None or df.empty or ticker not in df.columns:
        return None
    try:
        value = df.loc[dt, ticker]
    except KeyError:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) and value > 0 else None


def _safe_trailing_volume(
    df: pd.DataFrame | None,
    decision_date: pd.Timestamp,
    ticker: str,
    window: int,
) -> float | None:
    """Use only volume observed by the decision-date close."""
    if df is None or df.empty or ticker not in df.columns:
        return None
    values = pd.to_numeric(
        df.loc[df.index <= decision_date, ticker],
        errors="coerce",
    )
    values = values[np.isfinite(values) & (values > 0)].tail(max(1, window))
    if values.empty:
        return None
    return float(values.mean())


def _build_execution_details(
    assign_df: pd.DataFrame,
    return_index: pd.Index,
    rebal_dates: pd.DatetimeIndex,
    n_groups: int,
    *,
    execution: dict,
    execution_price_df: pd.DataFrame | None = None,
    volume_df: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Build per-stock target weights, trades, and costs for every rebalance.

    target_weight is the fully invested equal-weight portfolio inside each group.
    trade_weight is signed: positive means buy, negative means sell.
    Costs are calculated by translating weight deltas into an approximate
    dollar notional using execution.portfolio_value, then applying the shared
    FeeModel and SlippageModel.
    """
    group_names = [f"Q{g}" for g in range(1, n_groups + 1)]
    portfolio_value = float(execution.get("portfolio_value", 100000.0) or 100000.0)
    adv_window = int(
        ((execution.get("slippage") or {}).get("adv_window", 20)) or 20
    )

    prev_weights: dict[str, pd.Series] = {
        g: pd.Series(dtype="float64") for g in group_names
    }
    holdings_rows: list[dict] = []
    trades_rows: list[dict] = []
    cost_rows: list[dict] = []

    for decision_date in rebal_dates:
        effective_idx = return_index.searchsorted(decision_date, side="right")
        # Include a next-open trade only when its following valuation open is
        # also inside the measured horizon. Otherwise costs and returns would
        # be truncated on different dates.
        if effective_idx + 1 >= len(return_index):
            continue
        effective_date = return_index[effective_idx]

        assign_row = assign_df.loc[decision_date]
        for group_no, group_name in enumerate(group_names, start=1):
            tickers = list(assign_df.columns[assign_row.values == group_no])
            if tickers:
                new_weights = pd.Series(
                    1.0 / len(tickers),
                    index=pd.Index(tickers, name="ticker"),
                    dtype="float64",
                )
            else:
                new_weights = pd.Series(dtype="float64")

            old_weights = prev_weights[group_name]
            all_tickers = old_weights.index.union(new_weights.index)
            old_aligned = old_weights.reindex(all_tickers, fill_value=0.0)
            new_aligned = new_weights.reindex(all_tickers, fill_value=0.0)
            delta = new_aligned - old_aligned

            for ticker, weight in new_weights.items():
                holdings_rows.append({
                    "date": pd.Timestamp(effective_date).strftime("%Y-%m-%d"),
                    "decision_date": pd.Timestamp(decision_date).strftime("%Y-%m-%d"),
                    "group": group_name,
                    "ticker": ticker,
                    "target_weight": float(weight),
                })

            if old_weights.empty or new_weights.empty:
                turnover = float(delta.abs().sum())
            else:
                turnover = float(delta.abs().sum()) / 2.0

            group_trade_rows: list[dict] = []
            for ticker, trade_weight in delta.items():
                if abs(float(trade_weight)) <= 1e-12:
                    continue
                trade_abs = abs(float(trade_weight))
                side = "BUY" if trade_weight > 0 else "SELL"
                raw_price = _safe_lookup(execution_price_df, pd.Timestamp(effective_date), str(ticker))
                bar_volume = _safe_trailing_volume(
                    volume_df,
                    pd.Timestamp(decision_date),
                    str(ticker),
                    adv_window,
                )
                estimated_notional = trade_abs * portfolio_value
                estimated_quantity = (
                    estimated_notional / raw_price
                    if raw_price is not None and raw_price > 0 else 0.0
                )
                if raw_price is None or raw_price <= 0:
                    raise ValueError(
                        "Missing execution price for required backtest trade: "
                        f"decision_date={pd.Timestamp(decision_date).date()} "
                        f"execution_date={pd.Timestamp(effective_date).date()} "
                        f"ticker={ticker} side={side}. Rebuild open/close data; "
                        "the engine will not synthesize a fill."
                    )
                if estimated_quantity <= 0:
                    raise ValueError(
                        "Backtest trade resolved to a non-positive quantity: "
                        f"ticker={ticker} notional={estimated_notional}"
                    )
                max_quantity = max_volume_fill_quantity(
                    requested_quantity=estimated_quantity,
                    volume=bar_volume,
                    execution=execution,
                )
                if estimated_quantity > max_quantity + 1e-9:
                    raise ValueError(
                        "Backtest order exceeds the configured ADV fill limit: "
                        f"decision_date={pd.Timestamp(decision_date).date()} "
                        f"ticker={ticker} requested={estimated_quantity:.4f} "
                        f"allowed={max_quantity:.4f} adv={bar_volume}. "
                        "Reduce portfolio_value, widen the universe, or use an "
                        "event-driven partial-fill model."
                    )
                ex = calculate_execution(
                    side=side,
                    quantity=estimated_quantity,
                    raw_price=raw_price,
                    volume=bar_volume,
                    execution=execution,
                )
                slippage_cost = float(ex["slippage_cost"])
                fee = float(ex["fee"])
                total_cost_cash = float(ex["total_cost"])
                cost_rate = (
                    total_cost_cash / portfolio_value
                    if portfolio_value > 0
                    else 0.0
                )
                fee_components = ex.get("fee_components") or {}
                fill_price = float(ex["fill_price"])
                slippage_bps_used = float(ex["slippage_bps"])
                participation_rate = float(ex["participation_rate"])
                impact_bps = float(ex["impact_bps"])

                trade_row = {
                    "date": pd.Timestamp(effective_date).strftime("%Y-%m-%d"),
                    "decision_date": pd.Timestamp(decision_date).strftime("%Y-%m-%d"),
                    "group": group_name,
                    "ticker": ticker,
                    "old_weight": float(old_aligned.loc[ticker]),
                    "new_weight": float(new_aligned.loc[ticker]),
                    "trade_weight": float(trade_weight),
                    "trade_abs_weight": trade_abs,
                    "side": side,
                    "portfolio_value": portfolio_value,
                    "estimated_notional": float(estimated_notional),
                    "estimated_quantity": float(estimated_quantity),
                    "raw_price": float(raw_price) if raw_price is not None else np.nan,
                    "fill_price": float(fill_price),
                    "bar_volume": float(bar_volume) if bar_volume is not None else np.nan,
                    "volume_reference": f"ADV{adv_window}_asof_decision",
                    "participation_rate": participation_rate,
                    "slippage_model": execution.get("slippage_model"),
                    "slippage_bps": slippage_bps_used,
                    "impact_bps": impact_bps,
                    "slippage_cost": slippage_cost,
                    "fee_model": execution.get("fee_model"),
                    "broker_commission": float(fee_components.get("broker_commission", 0.0) or 0.0),
                    "sec_fee": float(fee_components.get("sec_fee", 0.0) or 0.0),
                    "finra_taf": float(fee_components.get("finra_taf", 0.0) or 0.0),
                    "finra_cat": float(fee_components.get("finra_cat", 0.0) or 0.0),
                    "clearing_fee": float(fee_components.get("clearing_fee", 0.0) or 0.0),
                    "pass_through_fee": float(fee_components.get("pass_through_fee", 0.0) or 0.0),
                    "exchange_fee": float(fee_components.get("exchange_fee", 0.0) or 0.0),
                    "fee": fee,
                    "total_cost_cash": total_cost_cash,
                    "cost": float(cost_rate),
                }
                group_trade_rows.append(trade_row)
                trades_rows.append(trade_row)

            traded_abs = float(delta.abs().sum())
            total_cost = float(sum(r["cost"] for r in group_trade_rows))
            total_fee = float(sum(r["fee"] for r in group_trade_rows))
            total_slippage = float(sum(r["slippage_cost"] for r in group_trade_rows))
            cost_rows.append({
                "date": pd.Timestamp(effective_date).strftime("%Y-%m-%d"),
                "decision_date": pd.Timestamp(decision_date).strftime("%Y-%m-%d"),
                "group": group_name,
                "traded_weight": traded_abs,
                "turnover": float(turnover),
                "portfolio_value": portfolio_value,
                "fee_model": execution.get("fee_model"),
                "slippage_model": execution.get("slippage_model"),
                "avg_slippage_bps": (
                    float(np.mean([r["slippage_bps"] for r in group_trade_rows]))
                    if group_trade_rows else 0.0
                ),
                "total_slippage_cost": total_slippage,
                "total_fee": total_fee,
                "total_cost_cash": total_slippage + total_fee,
                "cost": float(total_cost),
            })

            prev_weights[group_name] = new_weights

    holdings = pd.DataFrame(holdings_rows)
    trades = pd.DataFrame(trades_rows)
    costs = pd.DataFrame(cost_rows)
    return holdings, trades, costs


def _assign_groups_on_rebalance(
    factor_df: pd.DataFrame,
    rebalance_days: int,
    n_groups: int,
    *,
    rebalance_mode: str = "every_n_days",
    tradable_mask: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    仅在调仓日做 qcut 分组；非调仓日 forward-fill 沿用上一次分组。
    输出：date x ticker，值 ∈ {1..n_groups} 或 NaN。
    """
    dates = pd.DatetimeIndex(factor_df.index)
    rebal_dates = get_rebalance_dates(
        dates, mode=rebalance_mode, step_days=rebalance_days
    )

    assign = pd.DataFrame(np.nan, index=dates, columns=factor_df.columns)
    rebalance_set = set(rebal_dates)
    current = pd.Series(np.nan, index=factor_df.columns, dtype="float64")
    portfolio_started = False
    for dt in dates:
        if dt in rebalance_set:
            # A rebalance replaces the complete target snapshot. Ineligible
            # names must become NaN instead of inheriting their previous group.
            current = pd.Series(
                np.nan,
                index=factor_df.columns,
                dtype="float64",
            )
            row = factor_df.loc[dt]
            if tradable_mask is not None:
                if dt not in tradable_mask.index:
                    assign.loc[dt] = current
                    continue
                allowed = tradable_mask.loc[dt].reindex(row.index).fillna(False)
                row = row.where(allowed)
            row = row.dropna()
            if len(row) < n_groups:
                has_future_session = dates.get_loc(dt) < len(dates) - 1
                if portfolio_started and has_future_session:
                    raise ValueError(
                        "Insufficient eligible securities on rebalance date: "
                        f"date={pd.Timestamp(dt).date()} available={len(row)} "
                        f"required={n_groups}"
                    )
                assign.loc[dt] = current
                continue
            try:
                labels = pd.qcut(
                    row.rank(method="first"),
                    q=n_groups,
                    labels=list(range(1, n_groups + 1)),
                )
            except ValueError as exc:
                raise ValueError(
                    "Unable to form complete quantile groups on rebalance "
                    f"date={pd.Timestamp(dt).date()}"
                ) from exc
            current.loc[labels.index] = labels.astype(int).values
            portfolio_started = True
        assign.loc[dt] = current
    return assign


def _compute_turnover(
    assign_df: pd.DataFrame,
    n_groups: int,
    rebal_dates: pd.DatetimeIndex,
) -> pd.DataFrame:
    """计算每组每次调仓的双向换手率（对称差集 / 两期持仓合计）。展示用。"""
    rows = []
    prev_holdings: dict[int, set] = {g: set() for g in range(1, n_groups + 1)}
    for dt in rebal_dates:
        row_result = {"date": dt}
        for g in range(1, n_groups + 1):
            curr = set(assign_df.columns[assign_df.loc[dt].values == g])
            if prev_holdings[g]:
                sym_diff = prev_holdings[g].symmetric_difference(curr)
                denom = max(len(prev_holdings[g]) + len(curr), 1)
                row_result[f"Q{g}"] = len(sym_diff) / denom
            else:
                row_result[f"Q{g}"] = np.nan
            prev_holdings[g] = curr
        rows.append(row_result)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("date")


def _strict_equal_weight_group_returns(
    returns: pd.DataFrame,
    held_assignment: pd.DataFrame,
    *,
    n_groups: int,
    timing: str,
) -> pd.DataFrame:
    """
    Compute equal-weight returns without ex-post removal of missing holdings.

    A partially missing held cross-section is an execution/data error, not an
    instruction to renormalize the remaining stocks. The final next-open row is
    allowed to be wholly unavailable because its holding interval ends outside
    the requested backtest horizon.
    """
    group_cols = [f"Q{g}" for g in range(1, n_groups + 1)]
    out = pd.DataFrame(np.nan, index=returns.index, columns=group_cols)
    final_date = pd.Timestamp(returns.index[-1]) if len(returns.index) else None

    for group_no, group_name in enumerate(group_cols, start=1):
        selected = held_assignment.eq(group_no)
        held_count = selected.sum(axis=1)
        available_count = (selected & returns.notna()).sum(axis=1)
        incomplete = (held_count > 0) & (available_count < held_count)
        if timing == "next_open" and final_date is not None:
            horizon_only = (
                incomplete
                & (available_count == 0)
                & (pd.DatetimeIndex(returns.index) == final_date)
            )
            incomplete &= ~horizon_only
        if incomplete.any():
            bad_date = pd.Timestamp(incomplete.index[incomplete][0])
            missing = selected.loc[bad_date] & returns.loc[bad_date].isna()
            tickers = list(returns.columns[missing])[:10]
            raise ValueError(
                "Missing return for held securities; refusing ex-post "
                "renormalization: "
                f"date={bad_date.date()} group={group_name} "
                f"tickers={tickers}"
            )

        weights = selected.astype("float64").div(
            held_count.replace(0, np.nan),
            axis=0,
        )
        values = (returns * weights).sum(axis=1, min_count=1)
        complete = (held_count > 0) & (available_count == held_count)
        out[group_name] = values.where(complete)
    return out


def _cfg_get(obj: object, key: str, default):
    try:
        return getattr(obj, key)
    except Exception:  # noqa: BLE001
        return default


def build_tradable_mask(
    *,
    index: pd.Index,
    columns: pd.Index,
    returns_df: pd.DataFrame,
    price_df: pd.DataFrame | None = None,
    open_df: pd.DataFrame | None = None,
    volume_df: pd.DataFrame | None = None,
    timing: str = "close",
) -> pd.DataFrame:
    """
    Build a decision-date tradability mask.

    Every rule uses only information known by the decision-date close. Future
    execution-price availability is validated by the fill engine, after ranking.
    """
    cfg = _cfg_get(CONFIG.backtest, "tradability", {})
    enabled = bool(_cfg_get(cfg, "enabled", True))
    mask = pd.DataFrame(True, index=index, columns=columns)
    if not enabled:
        return mask

    idx = pd.DatetimeIndex(index)
    cols = pd.Index(columns)

    px = None
    if price_df is not None and not price_df.empty:
        px = price_df.reindex(index=idx, columns=cols)
        min_price = float(_cfg_get(cfg, "min_price", 0.0) or 0.0)
        mask &= px.notna()
        if min_price > 0:
            mask &= px >= min_price

    vol = None
    if volume_df is not None and not volume_df.empty:
        vol = volume_df.reindex(index=idx, columns=cols)
        mask &= vol.notna() & (vol > 0)
        min_dv = float(_cfg_get(cfg, "min_dollar_volume", 0.0) or 0.0)
        dv_window = int(_cfg_get(cfg, "dollar_volume_window", 20) or 20)
        if min_dv > 0 and px is not None:
            dollar_volume = (px * vol).rolling(
                dv_window, min_periods=max(1, int(dv_window * 0.6))
            ).mean()
            mask &= dollar_volume >= min_dv

    lookback = int(_cfg_get(cfg, "min_valid_return_lookback", 20) or 0)
    min_ratio = float(_cfg_get(cfg, "min_valid_return_ratio", 0.0) or 0.0)
    if lookback > 0 and min_ratio > 0:
        r = returns_df.reindex(index=idx, columns=cols)
        min_obs = max(1, int(np.ceil(lookback * min_ratio)))
        valid_count = r.notna().rolling(lookback, min_periods=1).sum()
        mask &= valid_count >= min_obs

    return mask.fillna(False)


def quintile_backtest(
    factor_df: pd.DataFrame,
    returns_df: pd.DataFrame,
    n_groups: Optional[int] = None,
    rebalance_days: Optional[int] = None,
    factor_direction: int = +1,
    *,
    open_df: pd.DataFrame | None = None,
    price_df: pd.DataFrame | None = None,
    volume_df: pd.DataFrame | None = None,
    tradable_mask: pd.DataFrame | None = None,
    benchmark_returns: pd.Series | None = None,
    rebalance_mode: str | None = None,
    execution: dict | None = None,
) -> QuintileResult:
    """
    五分位分组回测。

    Parameters
    ----------
    factor_df : date x ticker 因子值
    returns_df: date x ticker 日收益率（close-to-close）
    n_groups, rebalance_days, factor_direction : 见原文档
    open_df : date x ticker 复权开盘价。当 execution.timing="next_open" 时**必须**提供。
    execution : {timing, fee_model, slippage_model, slippage_bps, commission_bps}。
                None 时从 CONFIG.backtest.execution 读默认（推荐 next_open）。

    Returns
    -------
    QuintileResult
    """
    n_groups = int(n_groups or CONFIG.backtest.n_groups)
    rebalance_days = int(rebalance_days or CONFIG.backtest.rebalance_days)
    rebalance_mode = str(
        rebalance_mode
        or getattr(CONFIG.backtest, "rebalance_mode", "every_n_days")
    ).lower()
    exec_cfg = _resolve_execution(execution)
    if factor_direction not in {-1, 1}:
        raise ValueError(
            "factor_direction must be fixed ex ante as +1 or -1; choosing "
            "direction from the completed backtest would introduce look-ahead bias."
        )

    # 选择持有期收益矩阵
    if open_df is None or open_df.empty:
        raise ValueError(
            "execution.timing=next_open requires open prices from a published "
            "market-data version. Run `python scripts/run_data_pipeline.py update "
            "--universe <UNIVERSE>` first."
        )
    # At row d+1, assign.shift(1) contains the decision made at d. Moving the
    # open[d+2]/open[d+1] return to row d+1 attributes the return to the
    # holding interval that starts at the executable d+1 open.
    o2o_raw = _compute_open_to_open_returns(open_df)
    held_returns = o2o_raw.shift(-1)

    # 对齐索引与列
    common_dates = factor_df.index.intersection(held_returns.index)
    common_cols = factor_df.columns.intersection(held_returns.columns)
    f = factor_df.loc[common_dates, common_cols].copy()
    r = held_returns.loc[common_dates, common_cols].copy()

    if tradable_mask is None:
        tradable_mask = build_tradable_mask(
            index=common_dates,
            columns=common_cols,
            returns_df=returns_df,
            price_df=price_df,
            open_df=open_df,
            volume_df=volume_df,
            timing=exec_cfg["timing"],
        )
    else:
        tradable_mask = tradable_mask.reindex(
            index=common_dates, columns=common_cols, fill_value=False
        )
    benchmark_base = (
        benchmark_returns.reindex(common_dates)
        if benchmark_returns is not None
        else r.where(tradable_mask.shift(1).fillna(False)).mean(axis=1)
    )
    benchmark_base.name = "Benchmark"

    # 分组：以 t 日因子分组，持仓从 t+1 日生效 —— 故对 assign 做 shift(1)
    assign = _assign_groups_on_rebalance(
        f,
        rebalance_days=rebalance_days,
        n_groups=n_groups,
        rebalance_mode=rebalance_mode,
        tradable_mask=tradable_mask,
    )
    assign_held = assign.shift(1)
    rebal_dates = get_rebalance_dates(
        pd.DatetimeIndex(f.index),
        mode=rebalance_mode,
        step_days=rebalance_days,
    )

    # 各组日收益（严格等权，不因缺失收益事后重分配权重）
    group_cols = [f"Q{g}" for g in range(1, n_groups + 1)]
    gross_ret = _strict_equal_weight_group_returns(
        r,
        assign_held,
        n_groups=n_groups,
        timing=exec_cfg["timing"],
    )
    gross_ret = gross_ret.dropna(how="all")

    # 摩擦扣减：逐票目标权重变化 × 单边成本，再汇总到组级收益。
    holdings_detail, trades_detail, costs_detail = _build_execution_details(
        assign,
        common_dates,
        rebal_dates,
        n_groups=n_groups,
        execution=exec_cfg,
        execution_price_df=open_df if exec_cfg["timing"] == "next_open" else price_df,
        volume_df=volume_df,
    )
    cost_df = pd.DataFrame(0.0, index=gross_ret.index, columns=group_cols)
    if not costs_detail.empty:
        for row in costs_detail.itertuples(index=False):
            dt = pd.Timestamp(row.date)
            group = str(row.group)
            if dt in cost_df.index and group in cost_df.columns:
                cost_df.loc[dt, group] += float(row.cost)

    group_ret = gross_ret - cost_df
    # 给绩效汇总用：每组的年化总成本 bps（粗略）
    days_total = max(len(group_ret.index), 1)
    cost_bps_per_year = {
        f"Q{g}": float(cost_df[f"Q{g}"].sum()) * (252.0 / days_total) * 10000.0
        for g in range(1, n_groups + 1)
    }

    # Long-Short
    top, bot = f"Q{n_groups}", "Q1"
    raw_ls = group_ret[top] - group_ret[bot]
    direction = int(factor_direction)
    ls = raw_ls * direction
    ls.name = "LongShort"
    top_returns = group_ret[top].rename(top)
    benchmark_aligned = benchmark_base.reindex(group_ret.index).dropna()
    excess = (top_returns - benchmark_base).rename("Excess")

    # 净值
    group_nav = (1.0 + group_ret.fillna(0)).cumprod()
    ls_nav = (1.0 + ls.fillna(0)).cumprod()
    ls_nav.name = "LongShort"
    benchmark_nav = (1.0 + benchmark_base.reindex(group_ret.index).fillna(0)).cumprod()
    benchmark_nav.name = "Benchmark"

    # 展示用换手率（双向）
    turnover = _compute_turnover(assign, n_groups=n_groups, rebal_dates=rebal_dates)

    # 每组 + Long-Short 绩效
    metrics_rows = {}
    for col in group_cols:
        metrics_rows[col] = performance_summary(group_ret[col])
        if col == top:
            metrics_rows[col].update(
                relative_performance_summary(group_ret[col], benchmark_base)
            )
    metrics_rows["LongShort"] = performance_summary(ls)
    metrics_df = pd.DataFrame(metrics_rows).T

    if not turnover.empty:
        avg_to = turnover.mean(axis=0)
        avg_to["LongShort"] = (
            turnover.get(top, pd.Series(dtype=float)).mean()
            + turnover.get(bot, pd.Series(dtype=float)).mean()
        )
        metrics_df["AvgTurnover"] = metrics_df.index.map(avg_to.to_dict())

    log.info(
        "Quintile backtest done: n_groups=%d, rebalance=%s/%dd, direction=%+d, "
        "timing=%s, fee_model=%s, slippage_model=%s, "
        "fallback_slippage=%.1fbps, simple_commission=%.1fbps, "
        "LongShort AnnReturn=%.4f, Sharpe=%.3f, MaxDD=%.4f",
        n_groups, rebalance_mode, rebalance_days, direction,
        exec_cfg["timing"], exec_cfg.get("fee_model"), exec_cfg.get("slippage_model"),
        exec_cfg["slippage_bps"], exec_cfg["commission_bps"],
        metrics_df.loc["LongShort", "AnnReturn"],
        metrics_df.loc["LongShort", "Sharpe"],
        metrics_df.loc["LongShort", "MaxDD"],
    )

    return QuintileResult(
        group_daily_returns=group_ret,
        long_short_returns=ls,
        group_nav=group_nav,
        long_short_nav=ls_nav,
        turnover=turnover,
        group_metrics=metrics_df,
        group_assignment=assign,
        holdings_detail=holdings_detail,
        trades_detail=trades_detail,
        costs_detail=costs_detail,
        config={
            "n_groups": n_groups,
            "rebalance_days": rebalance_days,
            "rebalance_mode": rebalance_mode,
            "direction": direction,
            "execution": exec_cfg,
        },
        benchmark_returns=benchmark_aligned,
        benchmark_nav=benchmark_nav,
        excess_returns=excess.dropna(),
        execution_cost_bps_per_year=cost_bps_per_year,
        gross_group_returns=gross_ret,
        cost_returns=cost_df,
        effective_returns=r,
        held_assignment=assign_held,
        tradable_mask=tradable_mask,
        rebalance_dates=rebal_dates,
    )


__all__ = ["QuintileResult", "quintile_backtest"]
