"""Cross-sectional factor neutralization with temporal-integrity gates.

Formal historical industry neutralization is applied only when the classification
itself is point-in-time.  A latest-known industry snapshot is explicitly skipped
(and audited) so research can continue without contaminated residuals.  Market-
cap neutralization is stricter: if it is requested, a valid PIT date x ticker
matrix is mandatory and the pipeline fails closed otherwise.
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
    """Requested neutralization exposure is missing or violates PIT semantics."""


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
    requested_industry: bool = False
    requested_mcap: bool = False
    industry_temporal_policy: str | None = None
    mcap_temporal_policy: str | None = None
    industry_skip_reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _temporal_policy(value: Any, key: str) -> str | None:
    attrs = getattr(value, "attrs", {}) or {}
    raw = attrs.get(key)
    if raw is None:
        return None
    normalized = str(raw).strip().upper()
    return normalized or None


def _industry_contract(
    sector_map: pd.Series | pd.DataFrame | None,
) -> tuple[str | None, pd.DataFrame | None, str | None]:
    """Return (policy, usable PIT matrix, skip reason)."""
    if sector_map is None or getattr(sector_map, "empty", True):
        return None, None, "missing_industry_exposure"
    policy = _temporal_policy(sector_map, "classification_policy")
    if policy != PIT_CLASSIFICATION_POLICY:
        return policy, None, (
            "non_pit_industry_exposure_rejected: expected="
            f"{PIT_CLASSIFICATION_POLICY} observed={policy or 'MISSING'}"
        )
    if isinstance(sector_map, pd.Series):
        raise NeutralizationDataError(
            "classification_policy claims PIT but a static ticker->sector Series "
            "cannot represent historical classification"
        )
    if "sector" in sector_map.columns or not isinstance(
        sector_map.index, pd.DatetimeIndex
    ):
        raise NeutralizationDataError(
            "classification_policy claims PIT but sector exposure is not a "
            "date x ticker matrix"
        )
    return policy, sector_map, None


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
    """Neutralize only with temporally valid exposures."""
    if factor_df.empty:
        empty = factor_df.copy()
        audit = NeutralizationAudit(False, False, 0, 0, 0, 0, 0, 0, 0.0, ())
        return (empty, audit) if return_audit else empty

    requested_industry = bool(
        getattr(CONFIG.preprocessing, "neutralize_industry", False)
    )
    requested_mcap = bool(getattr(CONFIG.preprocessing, "neutralize_mcap", False))
    if not requested_industry and not requested_mcap:
        result = factor_df.copy()
        observations = int(result.notna().sum().sum())
        audit = NeutralizationAudit(
            False,
            False,
            0,
            len(result),
            observations,
            observations,
            0,
            0,
            1.0,
            (),
            requested_industry=False,
            requested_mcap=False,
        )
        return (result, audit) if return_audit else result

    industry_policy, sector_frame, industry_skip_reason = _industry_contract(
        sector_map
    ) if requested_industry else (None, None, None)
    active_industry = requested_industry and sector_frame is not None
    if requested_industry and not active_industry:
        log.warning(
            "Industry neutralization requested but skipped to preserve PIT integrity: %s",
            industry_skip_reason,
        )

    mcap_policy = _validate_mcap_contract(mcap_df) if requested_mcap else None
    active_mcap = requested_mcap

    if not active_industry and not active_mcap:
        result = factor_df.copy()
        observations = int(result.notna().sum().sum())
        daily = tuple(
            {
                "date": pd.Timestamp(dt).date().isoformat(),
                "input_non_null": int(row.notna().sum()),
                "regression_observations": 0,
                "known_industry": 0,
                "missing_industry": int(row.notna().sum()),
                "missing_mcap": 0,
                "output_non_null": int(row.notna().sum()),
                "applied": False,
                "reason": industry_skip_reason or "neutralization_not_requested",
            }
            for dt, row in factor_df.iterrows()
        )
        audit = NeutralizationAudit(
            enabled_industry=False,
            enabled_mcap=False,
            applied_days=0,
            skipped_days=len(result),
            observations=observations,
            known_industry_observations=0,
            missing_industry_observations=observations if requested_industry else 0,
            missing_mcap_observations=0,
            industry_coverage=0.0 if requested_industry else 1.0,
            daily=daily,
            requested_industry=requested_industry,
            requested_mcap=requested_mcap,
            industry_temporal_policy=industry_policy,
            mcap_temporal_policy=mcap_policy,
            industry_skip_reason=industry_skip_reason,
        )
        return (result, audit) if return_audit else result

    min_obs = int(getattr(CONFIG.preprocessing, "neutralize_min_obs", 30))
    rows: list[pd.Series] = []
    daily_rows: list[dict] = []
    applied_count = 0
    for dt, row in factor_df.iterrows():
        dt = pd.Timestamp(dt)
        sector = (
            _sector_row(sector_frame, dt, factor_df.columns)
            if active_industry and sector_frame is not None
            else None
        )
        mcap_row = (
            mcap_df.loc[dt].reindex(factor_df.columns)
            if active_mcap and mcap_df is not None and dt in mcap_df.index
            else None
        )
        neutralized, applied, diagnostics = _neutralize_row(
            row,
            sector,
            mcap_row,
            use_industry=active_industry,
            use_mcap=active_mcap,
            min_obs=min_obs,
        )
        rows.append(neutralized)
        daily_rows.append({"date": dt.date().isoformat(), **diagnostics})
        applied_count += int(applied)

    skipped = len(rows) - applied_count
    result = pd.DataFrame(rows, index=factor_df.index, columns=factor_df.columns)
    observations = sum(int(item["input_non_null"]) for item in daily_rows)
    known = sum(int(item["known_industry"]) for item in daily_rows)
    missing_industry = sum(int(item["missing_industry"]) for item in daily_rows)
    missing_mcap = sum(int(item["missing_mcap"]) for item in daily_rows)
    audit = NeutralizationAudit(
        enabled_industry=active_industry,
        enabled_mcap=active_mcap,
        applied_days=applied_count,
        skipped_days=skipped,
        observations=observations,
        known_industry_observations=known,
        missing_industry_observations=missing_industry,
        missing_mcap_observations=missing_mcap,
        industry_coverage=(known / observations if observations else 0.0),
        daily=tuple(daily_rows),
        requested_industry=requested_industry,
        requested_mcap=requested_mcap,
        industry_temporal_policy=industry_policy,
        mcap_temporal_policy=mcap_policy,
        industry_skip_reason=industry_skip_reason,
    )
    if skipped:
        raise NeutralizationDataError(
            "PIT neutralization could not be applied on every factor date; "
            f"applied={applied_count} skipped={skipped}. Formal research fails closed."
        )
    log.info(
        "PIT neutralization finished: applied=%d industry=%s mcap=%s",
        applied_count,
        active_industry,
        active_mcap,
    )
    return (result, audit) if return_audit else result


__all__ = [
    "NeutralizationAudit",
    "NeutralizationDataError",
    "neutralize_industry",
]
