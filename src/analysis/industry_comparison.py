"""Isolated industry-neutral factor comparison; never changes production CONFIG."""
from __future__ import annotations

import pandas as pd

from src.analysis.ic import compute_ic, ic_summary
from src.data.classification_history import ClassificationHistoryError
from src.data.security_master import PIT_CLASSIFICATION_POLICY
from src.preprocessing.neutralize import neutralize_industry
from src.preprocessing.standardize import zscore_cs
from src.preprocessing.winsorize import winsorize_mad


def comparison_factors(raw: pd.DataFrame, sectors: pd.DataFrame, *, minimum_coverage: float = .95, min_observations: int = 30) -> tuple[dict[str, pd.DataFrame], dict]:
    if sectors.attrs.get("classification_policy") != PIT_CLASSIFICATION_POLICY:
        raise ClassificationHistoryError("Industry comparison requires genuine PIT classification inputs")
    if raw.empty or not 0 < minimum_coverage <= 1:
        raise ValueError("Nonempty factors and coverage in (0, 1] required")
    raw = raw.dropna(how="all")
    classified = sectors.reindex(index=raw.index, columns=raw.columns)
    known = classified.notna() & ~classified.isin(["", "UNKNOWN"])
    count = raw.notna().sum(axis=1)
    coverage = (known & raw.notna()).sum(axis=1) / count
    if coverage.min() < minimum_coverage:
        raise ClassificationHistoryError(f"PIT classification coverage below threshold: {coverage.min():.4f}")
    matched = raw.where(known)
    cleaned = winsorize_mad(matched, n=3)
    residual, audit = neutralize_industry(
        cleaned, classified, return_audit=True, industry_enabled=True,
        mcap_enabled=False, min_observations=min_observations,
    )
    variants = {
        "baseline_all": zscore_cs(winsorize_mad(raw, n=3)),
        "baseline_matched": zscore_cs(cleaned),
        "industry_neutral": zscore_cs(residual),
    }
    # Degenerate residual dates cannot silently give A/B different samples.
    common = variants["baseline_matched"].notna() & variants["industry_neutral"].notna()
    for name in ("baseline_matched", "industry_neutral"):
        variants[name] = variants[name].where(common)
    return variants, {"minimum_coverage": float(coverage.min()), "neutralization": audit.to_dict(), "excluded_observations": int((raw.notna() & ~common).sum().sum()), "methodology": "mad3_industry_ab_v1"}


def compare_ic(variants: dict[str, pd.DataFrame], returns: pd.DataFrame, *, periods: int = 5, min_stocks: int = 30, resolved_forward_returns: pd.DataFrame | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    series = {name: compute_ic(factor, returns, periods=periods, min_stocks=min_stocks, resolved_forward_returns=resolved_forward_returns) for name, factor in variants.items()}
    shared = series["baseline_matched"].index.intersection(series["industry_neutral"].index)
    if shared.empty:
        raise ClassificationHistoryError("No jointly evaluable A/B dates")
    for name in ("baseline_matched", "industry_neutral"):
        original = series[name]
        series[name] = original.reindex(shared)
        series[name].attrs.update(original.attrs)
    return pd.DataFrame(series), pd.DataFrame({name: ic_summary(s) for name, s in series.items()}).T
