"""Explicit PIT industry input contract, separate from current observations.

Intervals are inclusive, matching SecurityMasterStore symbol history. Date-only
knowledge is usable from the following session, never on its publication date.
Import validation checks structure/lineage; source evidence still needs review.
"""
from __future__ import annotations

import pandas as pd

from src.data.security_master import PIT_CLASSIFICATION_POLICY


class ClassificationHistoryError(ValueError):
    pass


def validate_history(history: pd.DataFrame) -> pd.DataFrame:
    required = {"security_id", "sector", "effective_from", "effective_to", "knowledge_date", "classification_policy", "taxonomy", "source", "source_evidence"}
    if history.empty or not required.issubset(history.columns):
        raise ClassificationHistoryError(f"PIT history missing fields: {sorted(required - set(history.columns))}")
    h = history.copy()
    for column in ("security_id", "sector", "taxonomy", "source", "source_evidence"):
        if h[column].isna().any() or h[column].astype(str).str.strip().eq("").any():
            raise ClassificationHistoryError(f"PIT history requires {column}")
    if not h["classification_policy"].eq(PIT_CLASSIFICATION_POLICY).all():
        raise ClassificationHistoryError("Current/backfilled classifications are not PIT history")
    if h["taxonomy"].nunique() != 1:
        raise ClassificationHistoryError("Mixed historical classification taxonomies")
    for column in ("effective_from", "knowledge_date", "effective_to"):
        parsed = pd.to_datetime(h[column], errors="coerce")
        invalid = parsed.isna() if column != "effective_to" else parsed.isna() & h[column].notna() & h[column].astype(str).str.strip().ne("")
        if invalid.any():
            raise ClassificationHistoryError(f"Invalid {column}")
        h[column] = parsed.dt.normalize()
    if (h["effective_to"].notna() & h["effective_to"].lt(h["effective_from"])).any():
        raise ClassificationHistoryError("Classification interval ends before it starts")
    return h


def build_pit_sector_matrix(
    history: pd.DataFrame,
    symbols: pd.DataFrame,
    dates: pd.DatetimeIndex,
    tickers: pd.Index,
) -> pd.DataFrame:
    h = validate_history(history)
    required = {"security_id", "ticker", "effective_from", "effective_to"}
    if not required.issubset(symbols.columns):
        raise ClassificationHistoryError("Dated ticker-to-security identity history is required")
    s = symbols.copy()
    if s[["security_id", "ticker"]].isna().any().any():
        raise ClassificationHistoryError("Missing symbol identity")
    for key in ("effective_from", "effective_to"):
        parsed = pd.to_datetime(s[key], errors="coerce")
        if (s[key].notna() & s[key].astype(str).str.strip().ne("") & parsed.isna()).any():
            raise ClassificationHistoryError("Malformed symbol interval")
        s[key] = parsed.dt.normalize()
    if (s["effective_to"].notna() & s["effective_from"].notna() & s["effective_to"].lt(s["effective_from"])).any():
        raise ClassificationHistoryError("Reversed symbol interval")
    dates = pd.DatetimeIndex(dates)
    if dates.has_duplicates or not dates.is_monotonic_increasing:
        raise ClassificationHistoryError("Decision dates must be sorted and unique")
    out = pd.DataFrame(index=dates, columns=tickers, dtype=object)
    for dt in dates:
        identity = s.loc[(s.effective_from.isna() | s.effective_from.le(dt)) & (s.effective_to.isna() | s.effective_to.ge(dt)) & s.ticker.isin(tickers)]
        if identity.groupby("ticker").security_id.nunique().gt(1).any():
            raise ClassificationHistoryError(f"Ambiguous security identity at {dt.date()}")
        available = h.loc[h.effective_from.le(dt) & (h.effective_to.isna() | h.effective_to.ge(dt)) & h.knowledge_date.lt(dt)]
        # Bitemporal revisions: choose the most recently known version, not a
        # correction which was learned after the decision date.
        latest = available.groupby("security_id").knowledge_date.transform("max")
        available = available.loc[available.knowledge_date.eq(latest)]
        if available.groupby("security_id").sector.nunique().gt(1).any():
            raise ClassificationHistoryError(f"Conflicting PIT sectors at {dt.date()}")
        sectors = available.drop_duplicates("security_id").set_index("security_id").sector
        mapping = identity.drop_duplicates("ticker").set_index("ticker").security_id.map(sectors)
        out.loc[dt] = mapping.reindex(tickers)
    out.attrs["classification_policy"] = PIT_CLASSIFICATION_POLICY
    out.attrs["taxonomy"] = str(h.taxonomy.iloc[0])
    out.attrs["knowledge_policy"] = "DATE_ONLY_NEXT_SESSION"
    return out
