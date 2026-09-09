"""
策略合成器：把策略（因子 id + 权重）在给定股票池上合成一个"合成因子值矩阵"。

合成算法（路径 B）：
  1. 读取每个成分因子的 factor_values.parquet（按 universe 过滤）
  2. 对日期取交集（长窗口因子起始晚）、对 ticker 取并集
  3. 每个因子做截面 Zscore 标准化（复用 preprocessing.zscore_cs）
  4. 按归一化权重加权求和 → 合成因子 date × ticker
  5. 返回 (composite, diagnostics)，由调用方（runner）传给 quintile_backtest

本模块不做回测，只做合成。回测仍复用 backtest.quintile.quintile_backtest。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import pandas as pd

from src.factors.artifacts import load_factor_matrix_bundle
from src.preprocessing.standardize import zscore_cs
from src.strategies.definition import StrategyComponent
from src.utils.logger import get_logger

log = get_logger(__name__)


class FactorDataMissingError(RuntimeError):
    """某个因子在指定股票池上还没有落盘 factor_values.parquet。"""


@dataclass
class CompositionResult:
    composite: pd.DataFrame                 # date × ticker，合成因子值（已 Zscore 加权）
    components: list[StrategyComponent]     # 原始权重（未归一化）
    normalized_weights: dict[str, float]    # factor_id -> 归一化权重
    date_range: tuple[str, str]             # (start_iso, end_iso) 合成后的有效日期范围
    tickers_count: int                      # 合成矩阵列数
    warnings: list[str] = field(default_factory=list)
    factor_raw: dict[str, pd.DataFrame] = field(default_factory=dict)
    factor_clean: dict[str, pd.DataFrame] = field(default_factory=dict)
    factor_inputs: dict[str, pd.DataFrame] = field(default_factory=dict)
    factor_contributions: dict[str, pd.DataFrame] = field(default_factory=dict)


# ---------------------------------------------------------------
# 加载因子值（带存在性校验）
# ---------------------------------------------------------------

def _load_factor_bundle(
    factor_id: str,
    universe: str,
    *,
    expected_generation_id: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    try:
        raw, clean, manifest = load_factor_matrix_bundle(
            factor_id,
            universe=universe,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise FactorDataMissingError(
            f"因子 {factor_id} 在股票池 {universe} 上没有可验证的 "
            "raw/clean 同代产物，请先运行 "
            f"`python scripts/run_mvp.py --update --only-universe {universe}`。"
        ) from exc
    observed_generation = str(manifest.get("generation_id") or "")
    if (
        expected_generation_id is not None
        and observed_generation != str(expected_generation_id)
    ):
        raise FactorDataMissingError(
            "Factor generation changed during portfolio composition: "
            f"universe={universe} factor={factor_id} "
            f"expected={expected_generation_id!r} "
            f"observed={observed_generation!r}. Refusing a mixed-version run."
        )
    if clean.empty:
        raise FactorDataMissingError(
            f"因子 {factor_id} 在股票池 {universe} 上尚未计算 factor_values.parquet，"
            f"请先运行 `python scripts/run_mvp.py --update --only-universe {universe}`。"
        )
    for values in (raw, clean):
        if not isinstance(values.index, pd.DatetimeIndex):
            values.index = pd.to_datetime(values.index)
    return raw.sort_index(), clean.sort_index()


# ---------------------------------------------------------------
# 合成主逻辑
# ---------------------------------------------------------------

def _normalize_weights(components: Iterable[StrategyComponent]) -> dict[str, float]:
    """按 |w|/Σ|w| 归一化，保留符号。"""
    comps = list(components)
    total = sum(abs(c.weight) for c in comps)
    if total <= 0:
        raise ValueError("策略权重不能全部为 0")
    return {c.factor_id: c.weight / total for c in comps}


def compose_factor(
    components: list[StrategyComponent],
    universe: str,
    *,
    start: pd.Timestamp | str | None = None,
    end: pd.Timestamp | str | None = None,
    expected_generations: dict[str, str] | None = None,
) -> CompositionResult:
    """
    合成因子。

    Parameters
    ----------
    components : 策略的原始成分（不要求已归一化）
    universe   : 股票池（SP500 / MAG7 ...）
    start/end  : 可选——合成前先裁剪日期；不传则使用各因子交集

    Returns
    -------
    CompositionResult
    """
    if not components:
        raise ValueError("components 为空")

    norm = _normalize_weights(components)
    log.info("Composing factor on %s with %d components: %s",
             universe, len(components), {k: round(v, 4) for k, v in norm.items()})

    # 读入各因子值矩阵
    bundles: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {
        c.factor_id: _load_factor_bundle(
            c.factor_id,
            universe,
            expected_generation_id=(expected_generations or {}).get(c.factor_id),
        )
        for c in components
    }
    per_factor = {
        factor_id: values[1]
        for factor_id, values in bundles.items()
    }

    # 日期交集（避免长窗口因子前期 NaN 传染合成结果）
    # 注意：factor_values 保存的是 clean 值（预处理后可能前期 NaN），
    #       用 dropna(how='all') 找到真实有效起点
    date_ranges: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for fid, df in per_factor.items():
        non_empty = df.dropna(how="all")
        if non_empty.empty:
            raise FactorDataMissingError(
                f"因子 {fid} 在 {universe} 上 factor_values 全为 NaN"
            )
        date_ranges[fid] = (non_empty.index.min(), non_empty.index.max())

    common_start = max(r[0] for r in date_ranges.values())
    common_end = min(r[1] for r in date_ranges.values())
    if start is not None:
        common_start = max(common_start, pd.Timestamp(start))
    if end is not None:
        common_end = min(common_end, pd.Timestamp(end))
    if common_start >= common_end:
        raise ValueError(
            f"因子日期交集为空或过短：{common_start} ~ {common_end}"
        )

    # Ticker 并集（对齐 columns）
    all_tickers: set[str] = set()
    for df in per_factor.values():
        all_tickers.update(df.columns)
    tickers = sorted(all_tickers)

    # 对齐后 Zscore + 加权累加
    composite: pd.DataFrame | None = None
    warnings: list[str] = []
    factor_clean: dict[str, pd.DataFrame] = {}
    factor_inputs: dict[str, pd.DataFrame] = {}
    factor_contributions: dict[str, pd.DataFrame] = {}
    for fid, df in per_factor.items():
        aligned = df.reindex(index=pd.date_range(common_start, common_end, freq="B"),
                             columns=tickers)
        # 放宽：有些因子数据天然缺一些交易日，直接用原始 index 更稳妥
        aligned = df.loc[common_start:common_end].reindex(columns=tickers)
        z = zscore_cs(aligned)

        w = norm[fid]
        weighted = z * w
        factor_clean[fid] = aligned
        factor_inputs[fid] = z
        factor_contributions[fid] = weighted
        # Complete-case policy: every stock/date must have every configured
        # factor. Treating one missing factor as a zero contribution would run
        # a different effective strategy for different stocks.
        composite = weighted if composite is None else composite + weighted

    assert composite is not None
    # 清理全 NaN 的行列（合成后仍可能有空洞）
    composite = composite.dropna(how="all")
    if composite.empty:
        raise FactorDataMissingError("合成因子矩阵为空（可能所有因子截面都无有效值）")

    final_index = composite.index
    final_columns = composite.columns
    factor_raw: dict[str, pd.DataFrame] = {}
    for fid, (raw, _) in bundles.items():
        factor_raw[fid] = raw.reindex(
            index=final_index,
            columns=final_columns,
        )

    return CompositionResult(
        composite=composite,
        components=list(components),
        normalized_weights=norm,
        date_range=(
            composite.index.min().strftime("%Y-%m-%d"),
            composite.index.max().strftime("%Y-%m-%d"),
        ),
        tickers_count=composite.shape[1],
        warnings=warnings,
        factor_raw=factor_raw,
        factor_clean={
            fid: values.reindex(index=final_index, columns=final_columns)
            for fid, values in factor_clean.items()
        },
        factor_inputs={
            fid: values.reindex(index=final_index, columns=final_columns)
            for fid, values in factor_inputs.items()
        },
        factor_contributions={
            fid: values.reindex(index=final_index, columns=final_columns)
            for fid, values in factor_contributions.items()
        },
    )


__all__ = [
    "FactorDataMissingError",
    "CompositionResult",
    "compose_factor",
]
