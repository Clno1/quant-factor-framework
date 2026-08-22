"""Cross-sectional factor neutralization with temporal-integrity gates.

Formal historical neutralization is allowed only when the exposure itself is
point-in-time.  A latest-known sector snapshot or market-cap snapshot is a
future-contaminated historical regressor and must never be silently applied.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.config import CONFIG
from src.data.security_master import PIT_CLASSIFICATION_POLICY
from src.utils.logger import get_logger


log = get_logger(__name__)
UNKNOWN_SECTOR = "UNKNOWN"
PIT_MARKET_CAP_POLICIES = {"PIT_EFFECTIVE_DATED", "PIT_DAILY"}


class NeutralizationDataError(ValueError):
    """Requested neutralization exposure is missing or not point-in-time."""


@dataclass(frozen=True)
class NeutralizationAudit:
    """Machine-readable evidence for every attempted cross-sectional regression."""

    enabled_industry: bool
    enabled_mcap: bool
    applied_days: int
    skipped_days: int
    observations: int
    known_industry_observations: int
    missing_industry_observations: int
    missing_mcap_observations: int
    industry_coverage: float
    daily: tuple[dict, ...]
    industry_temporal_policy: str | None = None
    mcap_temporal_policy: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _temporal_policy(value: Any, key: str) -> str | None:
    attrs = getattr(value, "attrs", {}) or {}
    raw = attrs.get(key)
    if raw is None:
        return None
    normalized = str(raw).strip().upper()
    return normalized or None


def _validate_industry_contract(
    sector_map: pd.Series | pd.DataFrame | None,
) -> str:
    if sector_map is None or getattr(sector_map, "empty", True):
        raise NeutralizationDataError(
            "Industry neutralization is enabled but no PIT sector exposure was supplied"
        )
    policy = _temporal_policy(sector_map, "classification_policy")
    if policy != PIT_CLASSIFICATION_POLICY:
        raise NeutralizationDataError(
            "Formal historical industry neutralization requires "
            f"classification_policy={PIT_CLASSIFICATION_POLICY}; observed={policy or 'MISSING'}. "
            "Latest-known sector snapshots are intentionally rejected until a PIT "
            "classification source is published."
        )
    if isinstance(sector_map, pd.Series):
        raise NeutralizationDataError(
            "A static ticker->sector Series cannot represent PIT historical classification"
        )
    if "sector" in sector_map.columns:
        raise NeutralizationDataError(
            "A one-row-per-security sector table is static metadata, not a date x ticker "
            "PIT classification matrix"
        )
    if not isinstance(sector_map.index, pd.DatetimeIndex):
        raise NeutralizationDataError(
            "PIT sector exposure must be a date x ticker DataFrame"
        )
    return policy


def _validate_mcap_contract(mcap_df: pd.DataFrame | None) -> str:
    if mcap_df is None or mcap_df.empty:
        raise NeutralizationDataError(
            "Market-cap neutralization is enabled but no PIT market-cap matrix was supplied"
        )
    policy = _temporal_policy(mcap_df, "market_cap_policy")
    if policy not in PIT_MARKET_CAP_POLICIES:
        raise NeutralizationDataError(
            "Market-cap neutralization requires a point-in-time date x ticker matrix; "
            f"observed market_cap_policy={policy or 'MISSING'}. Static/latest-known "
            "market cap is rejected instead of being silently skipped."
        )
    if not isinstance(mcap_df.index, pd.DatetimeIndex):
        raise NeutralizationDataError(
            "PIT market-cap exposure must have a DatetimeIndex"
        )
    return policy


def _sector_row(
    sector_map: pd.DataFrame,
    dt: pd.Timestamp,
    columns: pd.Index,
) -> pd.Series | None:
    if dt not in sector_map.index:
        return None
    row = sector_map.loc[dt]
    if isinstance(row, pd.DataFrame):
        if len(row) != 1:
            raise NeutralizationDataError(
                f"PIT sector matrix has duplicate rows for {pd.Timestamp(dt).date()}"
            )
        row = row.iloc[0]
    return row.reindex(columns)


def _neutralize_row(
    factor_row: pd.Series,
    sector: pd.Series | None,
    mcap_row: pd.Series | None,
    *,
    use_industry: bool,
    use_mcap: bool,
    min_obs: int,
) -> tuple[pd.Series, bool, dict]:
    y = factor_row.astype("float64")
    input_valid = y.notna()
    valid = input_valid.copy()
    parts: list[pd.DataFrame | pd.Series] = []
    missing_industry = 0
    known_industry = int(input_valid.sum())
    missing_mcap = 0

    if use_industry:
        if sector is None:
            diagnostics = {
                "input_non_null": int(input_valid.sum()),
                "regression_observations": 0,
                "known_industry": 0,
                "missing_industry": int(input_valid.sum()),
                "missing_mcap": 0,
                "output_non_null": 0,
                "applied": False,
                "reason": "missing_pit_industry_date",
            }
            return pd.Series(np.nan, index=y.index, dtype="float64"), False, diagnostics
        sec = sector.reindex(y.index)
        normalized = sec.fillna(UNKNOWN_SECTOR).astype(str).str.strip()
        normalized = normalized.mask(normalized.eq(""), UNKNOWN_SECTOR)
        known_mask = normalized.ne(UNKNOWN_SECTOR)
        missing_industry = int((input_valid & ~known_mask).sum())
        known_industry = int((input_valid & known_mask).sum())
        valid &= known_mask
        dummies = pd.get_dummies(normalized, dtype="float64")
        if dummies.shape[1] > 1:
            parts.append(dummies.iloc[:, 1:])

    if use_mcap:
        if mcap_row is None:
            diagnostics = {
                "input_non_null": int(input_valid.sum()),
                "regression_observations": 0,
                "known_industry": known_industry,
                "missing_industry": missing_industry,
                "missing_mcap": int(input_valid.sum()),
                "output_non_null": 0,
                "applied": False,
                "reason": "missing_pit_mcap_date",
            }
            return pd.Series(np.nan, index=y.index, dtype="float64"), False, diagnostics
        mcap = pd.to_numeric(mcap_row.reindex(y.index), errors="coerce")
        mcap = mcap.where(np.isfinite(mcap) & (mcap > 0))
        missing_mcap = int((input_valid & mcap.isna()).sum())
        valid &= mcap.notna()
        parts.append(np.log(mcap).rename("log_mcap"))

    diagnostics = {
        "input_non_null": int(input_valid.sum()),
        "regression_observations": int(valid.sum()),
        "known_industry": known_industry,
        "missing_industry": missing_industry,
        "missing_mcap": missing_mcap,
        "output_non_null": 0,
        "applied": False,
        "reason": "no_exposure_columns",
    }
    if not parts:
        return y, False, diagnostics

    X = pd.concat(parts, axis=1).loc[valid]
    yy = y.loc[valid]
    if len(yy) < max(min_obs, X.shape[1] + 2):
        diagnostics["reason"] = "insufficient_cross_section"
        return pd.Series(np.nan, index=y.index, dtype="float64"), False, diagnostics

    X = pd.concat(
        [pd.Series(1.0, index=X.index, name="const"), X.astype("float64")],
        axis=1,
    )
    try:
        beta, *_ = np.linalg.lstsq(X.to_numpy(), yy.to_numpy(), rcond=None)
    except np.linalg.LinAlgError:
        diagnostics["reason"] = "singular_regression"
        return pd.Series(np.nan, index=y.index, dtype="float64"), False, diagnostics
    resid = yy - X.to_numpy().dot(beta)
    out = pd.Series(np.nan, index=y.index, dtype="float64")
    out.loc[valid] = resid
    diagnostics.update(
        {
            "output_non_null": int(out.notna().sum()),
            "applied": True,
            "reason": "applied",
        }
    )
    return out, True, diagnostics


def neutralize_industry(
    factor_df: pd.DataFrame,
    sector_map: pd.Series | pd.DataFrame | None = None,
    mcap_df: pd.DataFrame | None = None,
    *,
    return_audit: bool = False,
) -> pd.DataFrame | tuple[pd.DataFrame, NeutralizationAudit]:
    """Neutralize industry / market cap using only PIT exposure matrices."""
    if factor_df.empty:
        empty = factor_df.copy()
        audit = NeutralizationAudit(False, False, 0, 0, 0, 0, 0, 0, 0.0, ())
        return (empty, audit) if return_audit else empty

    use_industry = bool(getattr(CONFIG.preprocessing, "neutralize_industry", False))
    use_mcap = bool(getattr(CONFIG.preprocessing, "neutralize_mcap", False))
    if not use_industry and not use_mcap:
        result = factor_df.copy()
        observations = int(result.notna().sum().sum())
        audit = NeutralizationAudit(
            False, False, 0, len(result), observations, observations, 0, 0, 1.0, ()
        )
        return (result, audit) if return_audit else result

    industry_policy = (
        _validate_industry_contract(sector_map) if use_industry else None
    )
    mcap_policy = _validate_mcap_contract(mcap_df) if use_mcap else None
    sector_frame = sector_map if isinstance(sector_map, pd.DataFrame) else None

    min_obs = int(getattr(CONFIG.preprocessing, "neutralize_min_obs", 30))
    rows: list[pd.Series] = []
    daily: list[dict] = []
    applied_count = 0
    for dt, row in factor_df.iterrows():
        dt = pd.Timestamp(dt)
        sector = (
            _sector_row(sector_frame, dt, factor_df.columns)
            if use_industry and sector_frame is not None
            else None
        )
        mcap_row = (
            mcap_df.loc[dt].reindex(factor_df.columns)
            if use_mcap and mcap_df is not None and dt in mcap_df.index
            else None
        )
        neutralized, applied, diagnostics = _neutralize_row(
            row,
            sector,
            mcap_row,
            use_industry=use_industry,
            use_mcap=use_mcap,
            min_obs=min_obs,
        )
        rows.append(neutralized)
        daily.append({"date": dt.date().isoformat(), **diagnostics})
        applied_count += int(applied)

    skipped = len(rows) - applied_count
    result = pd.DataFrame(rows, index=factor_df.index, columns=factor_df.columns)
    observations = sum(int(item["input_non_null"]) for item in daily)
    known = sum(int(item["known_industry"]) for item in daily)
    missing_industry = sum(int(item["missing_industry"]) for item in daily)
    missing_mcap = sum(int(item["missing_mcap"]) for item in daily)
    audit = NeutralizationAudit(
        enabled_industry=use_industry,
        enabled_mcap=use_mcap,
        applied_days=applied_count,
        skipped_days=skipped,
        observations=observations,
        known_industry_observations=known,
        missing_industry_observations=missing_industry,
        missing_mcap_observations=missing_mcap,
        industry_coverage=(known / observations if observations else 0.0),
        daily=tuple(daily),
        industry_temporal_policy=industry_policy,
        mcap_temporal_policy=mcap_policy,
    )
    if skipped:
        raise NeutralizationDataError(
            "PIT neutralization could not be applied on every factor date; "
            f"applied={applied_count} skipped={skipped}. Formal research fails closed."
        )
    log.info(
        "PIT neutralization finished: applied=%d industry=%s mcap=%s",
        applied_count,
        use_industry,
        use_mcap,
    )
    return (result, audit) if return_audit else result


__all__ = [
    "NeutralizationAudit",
    "NeutralizationDataError",
    "neutralize_industry",
]
