"""Point-in-time construction of the broad US liquid estimation universe."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from pathlib import Path
import tempfile
from typing import Any, Callable

import numpy as np
import pandas as pd

from src.data.foundation import DataFoundationError, QualityCheck
from src.data.membership_state import (
    complete_snapshot_dates,
    normalize_membership_events,
    resolve_membership_asof,
)


ELIGIBILITY_COLUMNS = [
    "date",
    "security_id",
    "ticker",
    "eligible",
    "selection_price",
    "adv20_usd",
    "valid_sessions_20d",
    "listed_pass",
    "asset_type_pass",
    "exchange_pass",
    "price_pass",
    "liquidity_pass",
    "reason_codes",
    "snapshot_type",
    "source_data_version_id",
]
_SNAPSHOT_CATEGORIES = [
    "MONTH_END",
    "FORCED_EXIT",
    "FORCED_EXIT_CARRY_FORWARD",
]
_REASON_CATEGORIES = sorted({
    ";".join(value for value in combination if value)
    for combination in product(
        (
            "",
            "LISTING_DATE_UNKNOWN",
            "NOT_YET_LISTED",
            "DELISTED",
            "TRADING_END_UNKNOWN",
            "NOT_LISTED",
        ),
        ("", "ASSET_TYPE_EXCLUDED"),
        ("", "EXCHANGE_EXCLUDED"),
        ("", "PRICE_MISSING", "PRICE_BELOW_FLOOR"),
        ("", "VALID_SESSIONS_INSUFFICIENT"),
        ("", "ADV20_MISSING", "ADV20_BELOW_FLOOR"),
    )
} | {"FORCED_EXIT_AFTER_DELISTING"})
_MEMBERSHIP_COLUMNS = [
    "date",
    "security_id",
    "ticker",
    "active",
    "selection_price",
    "adv20_usd",
    "valid_sessions_20d",
    "asset_type_pass",
    "price_pass",
    "liquidity_pass",
    "reason_codes",
    "snapshot_type",
    "source_data_version_id",
]


@dataclass(frozen=True)
class LiquidUniverseCandidate:
    target_session: pd.Timestamp
    membership: pd.DataFrame
    eligibility: pd.DataFrame
    checks: tuple[QualityCheck, ...]
    methodology: dict[str, Any]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


def _xnys_calendar(calendar: Any | None = None) -> Any:
    if calendar is not None:
        return calendar
    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise DataFoundationError(
            "exchange-calendars is required for broad PIT membership"
        ) from exc
    return xcals.get_calendar("XNYS")


def _sessions(calendar: Any, start: pd.Timestamp, target: pd.Timestamp) -> pd.DatetimeIndex:
    values = pd.DatetimeIndex(
        calendar.sessions_in_range(start.date(), target.date())
    )
    if values.tz is not None:
        values = values.tz_localize(None)
    return values.normalize()


def _month_end_sessions(sessions: pd.DatetimeIndex) -> pd.DatetimeIndex:
    if sessions.empty:
        return sessions
    series = pd.Series(sessions, index=sessions.to_period("M"))
    return pd.DatetimeIndex(series.groupby(level=0).max().tolist()).normalize()


def _normalize_bars(frame: pd.DataFrame, target: pd.Timestamp) -> pd.DataFrame:
    required = {
        "date", "security_id", "ticker", "close", "volume",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise DataFoundationError(f"coverage bars missing columns: {missing}")
    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    out["security_id"] = out["security_id"].fillna("").astype(str).str.strip()
    out["ticker"] = (
        out["ticker"].fillna("").astype(str).str.strip().str.upper()
        .str.replace(".", "-", regex=False)
    )
    out["security_id"] = out["security_id"].astype(object)
    out["ticker"] = out["ticker"].astype(object)
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce")
    out = out.loc[out["date"].le(target)].copy()
    if out["date"].isna().any() or out["security_id"].eq("").any():
        raise DataFoundationError("coverage bars contain invalid identities or dates")
    duplicate_count = int(out.duplicated(["date", "security_id"]).sum())
    if duplicate_count:
        raise DataFoundationError(
            f"coverage bars contain {duplicate_count} duplicate date/security rows"
        )
    return out.sort_values(["date", "security_id"]).reset_index(drop=True)


def _normalize_master(frame: pd.DataFrame) -> pd.DataFrame:
    required = {
        "security_id",
        "current_ticker",
        "asset_type",
        "primary_exchange",
        "listing_date",
        "delisting_date",
        "trading_status",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise DataFoundationError(f"Security Master missing columns: {missing}")
    out = frame.copy()
    out["security_id"] = out["security_id"].astype(str).str.strip()
    out["current_ticker"] = (
        out["current_ticker"].astype(str).str.strip().str.upper()
        .str.replace(".", "-", regex=False)
    )
    for column in ("asset_type", "primary_exchange", "trading_status"):
        out[column] = out[column].fillna("").astype(str).str.strip().str.upper()
    for column in (
        "security_id", "current_ticker", "asset_type", "primary_exchange",
        "trading_status",
    ):
        out[column] = out[column].astype(object)
    for column in ("listing_date", "delisting_date"):
        out[column] = pd.to_datetime(out[column], errors="coerce").dt.normalize()
    if out["security_id"].eq("").any() or out["security_id"].duplicated().any():
        raise DataFoundationError("Security Master security_id must be unique and non-empty")
    return out.reset_index(drop=True)


def _normalize_symbols(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(
            columns=["security_id", "ticker", "effective_from", "effective_to"]
        )
    required = {"security_id", "ticker", "effective_from", "effective_to"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise DataFoundationError(f"symbol history missing columns: {missing}")
    out = frame.copy()
    out["security_id"] = out["security_id"].astype(str)
    out["ticker"] = (
        out["ticker"].astype(str).str.upper().str.replace(".", "-", regex=False)
    )
    if "exchange" not in out.columns:
        out["exchange"] = ""
    out["exchange"] = out["exchange"].fillna("").astype(str).str.upper()
    out["security_id"] = out["security_id"].astype(object)
    out["ticker"] = out["ticker"].astype(object)
    out["effective_from"] = pd.to_datetime(
        out["effective_from"], errors="coerce"
    ).dt.normalize()
    out["effective_to"] = pd.to_datetime(
        out["effective_to"], errors="coerce"
    ).dt.normalize()
    return out


def _ticker_map(
    *,
    date: pd.Timestamp,
    master: pd.DataFrame,
    symbols: pd.DataFrame,
    session_bars: pd.DataFrame,
) -> dict[str, str]:
    mapping = dict(
        zip(master["security_id"], master["current_ticker"], strict=True)
    )
    if not symbols.empty:
        active = symbols.loc[
            (symbols["effective_from"].isna() | symbols["effective_from"].le(date))
            & (symbols["effective_to"].isna() | symbols["effective_to"].ge(date))
        ].sort_values(["security_id", "effective_from"], na_position="first")
        mapping.update(
            active.drop_duplicates("security_id", keep="last")
            .set_index("security_id")["ticker"]
            .astype(str)
            .to_dict()
        )
    if not session_bars.empty:
        mapping.update(
            session_bars.drop_duplicates("security_id", keep="last")
            .set_index("security_id")["ticker"]
            .astype(str)
            .to_dict()
        )
    return mapping


def _exchange_map(
    *,
    date: pd.Timestamp,
    master: pd.DataFrame,
    symbols: pd.DataFrame,
) -> dict[str, str]:
    mapping = dict(
        zip(master["security_id"], master["primary_exchange"], strict=True)
    )
    if symbols.empty:
        return mapping
    active = symbols.loc[
        (symbols["effective_from"].isna() | symbols["effective_from"].le(date))
        & (symbols["effective_to"].isna() | symbols["effective_to"].ge(date))
        & symbols["exchange"].ne("")
    ].sort_values(["security_id", "effective_from"], na_position="first")
    mapping.update(
        active.drop_duplicates("security_id", keep="last")
        .set_index("security_id")["exchange"]
        .astype(str)
        .to_dict()
    )
    return mapping


def _reason_codes(row: pd.Series) -> str:
    reasons: list[str] = []
    if not bool(row["listed_pass"]):
        listing = row.get("listing_date")
        delisting = row.get("delisting_date")
        status = str(row.get("trading_status") or "")
        if pd.isna(listing):
            reasons.append("LISTING_DATE_UNKNOWN")
        elif pd.Timestamp(listing) > pd.Timestamp(row["date"]):
            reasons.append("NOT_YET_LISTED")
        elif not pd.isna(delisting) and pd.Timestamp(delisting) < pd.Timestamp(row["date"]):
            reasons.append("DELISTED")
        elif status == "INACTIVE" and pd.isna(delisting):
            reasons.append("TRADING_END_UNKNOWN")
        else:
            reasons.append("NOT_LISTED")
    if not bool(row["asset_type_pass"]):
        reasons.append("ASSET_TYPE_EXCLUDED")
    if not bool(row["exchange_pass"]):
        reasons.append("EXCHANGE_EXCLUDED")
    if not bool(row["price_pass"]):
        price = row.get("selection_price")
        reasons.append(
            "PRICE_MISSING" if pd.isna(price) else "PRICE_BELOW_FLOOR"
        )
    if int(row.get("valid_sessions_20d") or 0) < int(row["min_valid_sessions"]):
        reasons.append("VALID_SESSIONS_INSUFFICIENT")
    if not bool(row["liquidity_pass"]):
        adv = row.get("adv20_usd")
        reasons.append("ADV20_MISSING" if pd.isna(adv) else "ADV20_BELOW_FLOOR")
    return ";".join(dict.fromkeys(reasons))


def _compact_eligibility_strings(
    frame: pd.DataFrame,
    *,
    security_categories: list[str],
    ticker_categories: list[str],
    parent_version_id: str,
) -> pd.DataFrame:
    """Dictionary-encode repeated strings before monthly audits accumulate."""
    security_dictionary = pd.Index(
        np.asarray(security_categories, dtype=object), dtype=object
    )
    ticker_dictionary = pd.Index(
        np.asarray(ticker_categories, dtype=object), dtype=object
    )
    reason_dictionary = pd.Index(
        np.asarray(_REASON_CATEGORIES, dtype=object), dtype=object
    )
    snapshot_dictionary = pd.Index(
        np.asarray(_SNAPSHOT_CATEGORIES, dtype=object), dtype=object
    )
    dictionaries = {
        "security_id": security_dictionary,
        "ticker": ticker_dictionary,
        "reason_codes": reason_dictionary,
        "snapshot_type": snapshot_dictionary,
        "source_data_version_id": pd.Index(
            [str(parent_version_id)], dtype=object
        ),
    }
    violations: list[str] = []
    for column, dictionary in dictionaries.items():
        raw = frame[column].astype(object)
        missing_count = int(raw.isna().sum())
        if missing_count:
            violations.append(f"{column}=<NULL> ({missing_count} rows)")
        unknown = raw.loc[raw.notna() & ~raw.isin(dictionary)]
        if not unknown.empty:
            samples = sorted({str(value) for value in unknown})[:8]
            violations.append(
                f"{column}={samples!r} ({len(unknown)} rows)"
            )
    if violations:
        raise DataFoundationError(
            "eligibility dictionary encoding encountered unknown or missing "
            "values: " + "; ".join(violations)
        )
    frame["security_id"] = pd.Categorical(
        frame["security_id"].astype(object), categories=security_dictionary
    )
    frame["ticker"] = pd.Categorical(
        frame["ticker"].astype(object), categories=ticker_dictionary
    )
    frame["reason_codes"] = pd.Categorical(
        frame["reason_codes"].astype(object), categories=reason_dictionary
    )
    frame["snapshot_type"] = pd.Categorical(
        frame["snapshot_type"].astype(object), categories=snapshot_dictionary
    )
    frame["source_data_version_id"] = pd.Categorical(
        frame["source_data_version_id"].astype(object),
        categories=dictionaries["source_data_version_id"],
    )
    encoded = [
        "security_id", "ticker", "reason_codes", "snapshot_type",
        "source_data_version_id",
    ]
    if frame[encoded].isna().any().any():
        raise AssertionError("validated eligibility dictionary encoding lost values")
    return frame


def _forced_exit_schedule(
    master: pd.DataFrame,
    sessions: pd.DatetimeIndex,
    *,
    start: pd.Timestamp,
    target: pd.Timestamp,
    complete_snapshot_dates: set[pd.Timestamp],
) -> dict[pd.Timestamp, set[str]]:
    forced: dict[pd.Timestamp, set[str]] = {}
    for row in master.loc[master["delisting_date"].notna()].itertuples(index=False):
        delisting = pd.Timestamp(row.delisting_date).normalize()
        position = sessions.searchsorted(delisting, side="right")
        if position >= len(sessions):
            continue
        effective = pd.Timestamp(sessions[position]).normalize()
        if (
            start <= effective <= target
            and effective not in complete_snapshot_dates
        ):
            forced.setdefault(effective, set()).add(str(row.security_id))
    return forced


def _compact_membership_timeline(
    *,
    snapshots: dict[pd.Timestamp, pd.DataFrame],
    master: pd.DataFrame,
    sessions: pd.DatetimeIndex,
    parent_version_id: str,
    start: pd.Timestamp,
    target: pd.Timestamp,
    initial_active: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, list[pd.DataFrame]]:
    """Store full month-end snapshots and one inactive row per forced exit."""
    complete_dates = {pd.Timestamp(value).normalize() for value in snapshots}
    forced = _forced_exit_schedule(
        master,
        sessions,
        start=start,
        target=target,
        complete_snapshot_dates=complete_dates,
    )
    active = (
        initial_active.copy().set_index("security_id", drop=False)
        if initial_active is not None and not initial_active.empty
        else pd.DataFrame(index=pd.Index([], name="security_id"))
    )
    membership_rows: list[pd.DataFrame] = []
    forced_audits: list[pd.DataFrame] = []
    timeline = sorted(complete_dates | set(forced))
    for event_date in timeline:
        if event_date in snapshots:
            rows = snapshots[event_date].copy()
            rows["date"] = event_date
            rows["active"] = True
            rows["snapshot_type"] = "MONTH_END"
            rows["source_data_version_id"] = parent_version_id
            membership_rows.append(rows.loc[:, _MEMBERSHIP_COLUMNS])
            active = rows.set_index("security_id", drop=False)
            continue
        if active.empty:
            continue
        removed_ids = set(active.index.astype(str)) & forced[event_date]
        if not removed_ids:
            continue
        removed = active.loc[
            active.index.astype(str).isin(removed_ids)
        ].copy()
        removed["date"] = event_date
        removed["active"] = False
        removed["reason_codes"] = "FORCED_EXIT_AFTER_DELISTING"
        removed["snapshot_type"] = "FORCED_EXIT"
        removed["source_data_version_id"] = parent_version_id
        membership_rows.append(removed.loc[:, _MEMBERSHIP_COLUMNS])

        audit_rows = master.loc[
            master["security_id"].astype(str).isin(removed_ids)
        ].copy()
        ticker_by_id = removed.set_index("security_id")["ticker"].astype(str)
        audit_rows["date"] = event_date
        audit_rows["ticker"] = audit_rows["security_id"].map(ticker_by_id)
        audit_rows["eligible"] = False
        audit_rows["selection_price"] = np.nan
        audit_rows["adv20_usd"] = np.nan
        audit_rows["valid_sessions_20d"] = 0
        audit_rows["listed_pass"] = False
        audit_rows["asset_type_pass"] = audit_rows["asset_type"].eq("STOCK")
        audit_rows["exchange_pass"] = audit_rows["primary_exchange"].isin(
            {"NYSE", "NASDAQ", "AMEX"}
        )
        audit_rows["price_pass"] = False
        audit_rows["liquidity_pass"] = False
        audit_rows["reason_codes"] = "FORCED_EXIT_AFTER_DELISTING"
        audit_rows["snapshot_type"] = "FORCED_EXIT"
        audit_rows["source_data_version_id"] = parent_version_id
        forced_audits.append(audit_rows.loc[:, ELIGIBILITY_COLUMNS])
        active = active.loc[~active.index.astype(str).isin(removed_ids)].copy()

    if not membership_rows:
        raise DataFoundationError("no securities passed broad PIT eligibility")
    membership = (
        pd.concat(membership_rows, ignore_index=True)
        .sort_values(["date", "security_id"])
        .reset_index(drop=True)
    )
    return membership, forced_audits


def _evaluate_month_end(
    *,
    date: pd.Timestamp,
    sessions: pd.DatetimeIndex,
    bars: pd.DataFrame,
    master: pd.DataFrame,
    symbols: pd.DataFrame,
    parent_version_id: str,
    adv_sessions: int,
    min_valid_sessions: int,
    min_price: float,
    min_adv20_usd: float,
    include_adr: bool,
) -> pd.DataFrame:
    position = sessions.searchsorted(date, side="right")
    window_dates = sessions[max(0, position - adv_sessions):position]
    window = bars.loc[bars["date"].isin(window_dates)].copy()
    finite = (
        np.isfinite(window["close"].to_numpy(dtype="float64"))
        & np.isfinite(window["volume"].to_numpy(dtype="float64"))
        & window["close"].gt(0).to_numpy()
        & window["volume"].ge(0).to_numpy()
    )
    valid = window.loc[finite].copy()
    valid["dollar_volume"] = valid["close"] * valid["volume"]
    metrics = valid.groupby("security_id", sort=False).agg(
        adv20_usd=("dollar_volume", "mean"),
        valid_sessions_20d=("date", "nunique"),
    )
    session_bars = valid.loc[valid["date"].eq(date)].copy()
    prices = session_bars.drop_duplicates("security_id", keep="last").set_index(
        "security_id"
    )["close"]
    tickers = _ticker_map(
        date=date,
        master=master,
        symbols=symbols,
        session_bars=session_bars,
    )
    exchanges = _exchange_map(date=date, master=master, symbols=symbols)
    audit = master.copy()
    audit["date"] = date
    audit["ticker"] = audit["security_id"].map(tickers).fillna(
        audit["current_ticker"]
    )
    audit["selection_price"] = audit["security_id"].map(prices)
    audit["adv20_usd"] = audit["security_id"].map(metrics["adv20_usd"])
    audit["valid_sessions_20d"] = (
        audit["security_id"].map(metrics["valid_sessions_20d"]).fillna(0).astype(int)
    )
    known_lifetime = audit["listing_date"].notna()
    before_or_on_delist = audit["delisting_date"].isna() | audit[
        "delisting_date"
    ].ge(date)
    known_inactive_end = ~(
        audit["trading_status"].eq("INACTIVE")
        & audit["delisting_date"].isna()
    )
    audit["listed_pass"] = (
        known_lifetime
        & audit["listing_date"].le(date)
        & before_or_on_delist
        & known_inactive_end
    )
    accepted_types = {"STOCK"} | ({"ADR"} if include_adr else set())
    audit["asset_type_pass"] = audit["asset_type"].isin(accepted_types)
    selection_exchange = audit["security_id"].map(exchanges).fillna(
        audit["primary_exchange"]
    )
    audit["exchange_pass"] = selection_exchange.isin(
        {"NYSE", "NASDAQ", "AMEX"}
    )
    audit["price_pass"] = audit["selection_price"].ge(float(min_price))
    audit["liquidity_pass"] = (
        audit["valid_sessions_20d"].ge(int(min_valid_sessions))
        & audit["adv20_usd"].ge(float(min_adv20_usd))
    )
    audit["eligible"] = audit[
        [
            "listed_pass",
            "asset_type_pass",
            "exchange_pass",
            "price_pass",
            "liquidity_pass",
        ]
    ].all(axis=1)
    audit["min_valid_sessions"] = int(min_valid_sessions)
    audit["reason_codes"] = audit.apply(_reason_codes, axis=1)
    audit["snapshot_type"] = "MONTH_END"
    audit["source_data_version_id"] = parent_version_id
    return audit.loc[:, ELIGIBILITY_COLUMNS]


def historical_pit_bar_coverage_check(
    membership: pd.DataFrame,
    partition_paths: list[str | Path],
    *,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    minimum_coverage: float,
    calendar: Any | None = None,
) -> tuple[QualityCheck, dict[str, Any]]:
    """Check each PIT session against authenticated coverage bar identities.

    Membership remains a compact sequence of complete snapshots. DuckDB joins
    each XNYS session to its latest snapshot and counts only ``date/security``
    keys from the bounded coverage partitions, avoiding a multi-million-row
    Pandas expansion on the 2 GB production host.
    """
    required = {"date", "security_id", "active"}
    missing = sorted(required - set(membership.columns))
    if missing:
        raise DataFoundationError(
            f"PIT bar-coverage check is missing membership columns: {missing}"
        )
    if not 0.0 < float(minimum_coverage) <= 1.0:
        raise ValueError("minimum_coverage must be in (0, 1]")
    paths = [Path(value).resolve() for value in partition_paths]
    if not paths or any(not path.is_file() for path in paths):
        raise DataFoundationError("PIT bar-coverage check has no readable partitions")

    start_ts = pd.Timestamp(start).normalize()
    end_ts = pd.Timestamp(end).normalize()
    if pd.isna(start_ts) or pd.isna(end_ts) or start_ts > end_ts:
        raise ValueError("PIT bar-coverage date range is invalid")
    sessions = _sessions(_xnys_calendar(calendar), start_ts, end_ts)
    members = normalize_membership_events(
        membership,
        key_column="security_id",
    )
    if (
        members.empty
        or members["date"].isna().any()
        or members.duplicated(["date", "security_id"]).any()
    ):
        raise DataFoundationError("PIT bar-coverage membership is empty or invalid")

    snapshot_dates = complete_snapshot_dates(membership)
    positions = snapshot_dates.searchsorted(sessions, side="right") - 1
    session_map = pd.DataFrame({
        "date": sessions,
        "snapshot_date": [
            snapshot_dates[position] if position >= 0 else pd.NaT
            for position in positions
        ],
    })
    missing_baseline = int(session_map["snapshot_date"].isna().sum())

    import duckdb

    path_metadata: list[tuple[Path, pd.Timestamp, pd.Timestamp]] = []
    for path in paths:
        probe = pd.read_parquet(path, columns=["date"])
        dates = pd.to_datetime(probe["date"], errors="coerce")
        if probe.empty or dates.isna().any():
            raise DataFoundationError(
                f"PIT bar-coverage partition has invalid dates: {path}"
            )
        path_metadata.append((
            path,
            pd.Timestamp(dates.min()).normalize(),
            pd.Timestamp(dates.max()).normalize(),
        ))

    # The prior full-history join built millions of expected rows beside ten
    # million observed rows at once.  On the 2 GB production host that stayed
    # above MemoryHigh and spent most CPU reclaiming pages.  Calendar-month
    # windows are exactly equivalent because the join key contains `date`, and
    # they keep the working set bounded without weakening the daily gate.
    daily_frames: list[pd.DataFrame] = []
    connection = duckdb.connect()
    try:
        connection.execute("SET threads = 1")
        connection.execute("SET memory_limit = '320MB'")
        connection.execute("SET preserve_insertion_order = false")
        connection.execute("SET max_temp_directory_size = '12GB'")
        connection.register("pit_membership_input", members)
        connection.execute(
            """
            CREATE TEMP TABLE pit_membership AS
            SELECT CAST(date AS DATE) AS date,
                   CAST(security_id AS VARCHAR) AS security_id,
                   CAST(active AS BOOLEAN) AS active,
                   CAST(snapshot_type AS VARCHAR) AS snapshot_type
            FROM pit_membership_input
            """
        )
        connection.unregister("pit_membership_input")
        with tempfile.TemporaryDirectory(prefix="pit-coverage-") as temporary:
            escaped_temp = str(Path(temporary).resolve()).replace("'", "''")
            connection.execute(f"SET temp_directory = '{escaped_temp}'")
            for period in pd.period_range(start_ts, end_ts, freq="M"):
                period_start = max(start_ts, period.start_time.normalize())
                period_end = min(end_ts, period.end_time.normalize())
                window_sessions = session_map.loc[
                    session_map["date"].between(period_start, period_end)
                ].copy()
                if window_sessions.empty:
                    continue
                selected_paths = [
                    path
                    for path, minimum, maximum in path_metadata
                    if minimum <= period_end and maximum >= period_start
                ]
                if not selected_paths:
                    continue
                connection.register("pit_sessions_window", window_sessions)
                try:
                    daily_frames.append(connection.execute(
                        """
                        WITH expected AS (
                            SELECT s.date, m.security_id
                            FROM pit_sessions_window AS s
                            JOIN pit_membership AS m
                              ON m.date = s.snapshot_date
                            WHERE s.snapshot_date IS NOT NULL
                              AND m.active
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM pit_membership AS removed
                                  WHERE NOT removed.active
                                    AND removed.snapshot_type = 'FORCED_EXIT'
                                    AND removed.security_id = m.security_id
                                    AND removed.date > s.snapshot_date
                                    AND removed.date <= s.date
                              )
                        )
                        SELECT e.date,
                               count(*) AS expected_members,
                               count(o.security_id) AS observed_members,
                               count(o.security_id)::DOUBLE / count(*) AS coverage
                        FROM expected AS e
                        LEFT JOIN read_parquet(
                            ?, hive_partitioning = false
                        ) AS o
                          ON o.date = e.date
                         AND o.security_id = e.security_id
                         AND o.date >= ? AND o.date <= ?
                        GROUP BY e.date
                        ORDER BY e.date
                        """,
                        [
                            [str(path) for path in selected_paths],
                            period_start.date(),
                            period_end.date(),
                        ],
                    ).df())
                finally:
                    connection.unregister("pit_sessions_window")
    finally:
        connection.close()
    daily = (
        pd.concat(daily_frames, ignore_index=True)
        .sort_values("date")
        .reset_index(drop=True)
        if daily_frames
        else pd.DataFrame(
            columns=[
                "date", "expected_members", "observed_members", "coverage"
            ]
        )
    )

    minimum = float(daily["coverage"].min()) if not daily.empty else 0.0
    failing = daily.loc[daily["coverage"].lt(float(minimum_coverage))]
    worst = (
        daily.nsmallest(20, "coverage")
        .assign(date=lambda value: pd.to_datetime(value["date"]).dt.date.astype(str))
        .to_dict("records")
    )
    diagnostics = {
        "start": start_ts.date().isoformat(),
        "end": end_ts.date().isoformat(),
        "session_count": int(len(sessions)),
        "evaluated_session_count": int(len(daily)),
        "missing_baseline_sessions": missing_baseline,
        "minimum_daily_coverage": minimum,
        "failing_session_count": int(len(failing)),
        "worst_sessions": worst,
    }
    passed = (
        missing_baseline == 0
        and len(daily) == len(sessions)
        and minimum >= float(minimum_coverage)
    )
    check = QualityCheck(
        "historical_pit_daily_bar_coverage",
        passed,
        diagnostics,
        {
            "minimum_daily_coverage": float(minimum_coverage),
            "missing_baseline_sessions": 0,
        },
        (
            f"minimum PIT-member daily bar coverage {minimum:.2%}"
            if passed
            else "at least one PIT session is missing too many member bars"
        ),
    )
    return check, diagnostics


def build_liquid_5m_candidate(
    bars: pd.DataFrame | None,
    security_master: pd.DataFrame,
    *,
    parent_version_id: str,
    target_session: str | pd.Timestamp,
    history_start: str | pd.Timestamp,
    research_start: str | pd.Timestamp,
    symbol_history: pd.DataFrame | None = None,
    min_price: float = 1.0,
    min_adv20_usd: float = 5_000_000.0,
    adv_sessions: int = 20,
    min_valid_sessions: int = 15,
    include_adr: bool = False,
    calendar: Any | None = None,
    bar_loader: Callable[[pd.Timestamp, pd.Timestamp], pd.DataFrame] | None = None,
) -> LiquidUniverseCandidate:
    """Build deterministic month-end membership plus forced delisting exits."""
    target = pd.Timestamp(target_session).normalize()
    start = pd.Timestamp(history_start).normalize()
    study_start = pd.Timestamp(research_start).normalize()
    if any(pd.isna(value) for value in (target, start, study_start)) or start > target:
        raise ValueError("history/research/target dates are invalid")
    if adv_sessions < 1 or not 1 <= min_valid_sessions <= adv_sessions:
        raise ValueError("ADV session parameters are invalid")
    if min_price <= 0 or min_adv20_usd <= 0:
        raise ValueError("price and ADV thresholds must be positive")

    calendar = _xnys_calendar(calendar)
    sessions = _sessions(calendar, start, target)
    if target not in sessions:
        raise DataFoundationError(f"{target.date()} is not an XNYS session")
    if bars is None and bar_loader is None:
        raise ValueError("bars or bar_loader is required")
    normalized_bars = _normalize_bars(bars, target) if bars is not None else None
    master = _normalize_master(security_master)
    symbols = _normalize_symbols(symbol_history)
    security_categories = sorted(master["security_id"].astype(str).unique())
    ticker_categories = sorted(
        set(master["current_ticker"].astype(str))
        | set(symbols["ticker"].astype(str))
    )
    extended_sessions = _sessions(
        calendar, start, target + pd.Timedelta(days=40)
    )
    month_ends = _month_end_sessions(extended_sessions)
    month_ends = month_ends[month_ends <= target]
    usable_month_ends = [
        value
        for value in month_ends
        if sessions.searchsorted(value, side="right") >= adv_sessions
    ]
    if not usable_month_ends:
        raise DataFoundationError("coverage history has no complete ADV window")

    audits: list[pd.DataFrame] = []
    snapshots: dict[pd.Timestamp, pd.DataFrame] = {}
    for snapshot_date in usable_month_ends:
        snapshot_date = pd.Timestamp(snapshot_date)
        position = sessions.searchsorted(snapshot_date, side="right")
        window_dates = sessions[max(0, position - adv_sessions):position]
        snapshot_bars = (
            normalized_bars.loc[normalized_bars["date"].isin(window_dates)].copy()
            if normalized_bars is not None
            else _normalize_bars(
                bar_loader(pd.Timestamp(window_dates.min()), snapshot_date),
                snapshot_date,
            )
        )
        audit = _evaluate_month_end(
            date=snapshot_date,
            sessions=sessions,
            bars=snapshot_bars,
            master=master,
            symbols=symbols,
            parent_version_id=parent_version_id,
            adv_sessions=adv_sessions,
            min_valid_sessions=min_valid_sessions,
            min_price=min_price,
            min_adv20_usd=min_adv20_usd,
            include_adr=include_adr,
        )
        audit = _compact_eligibility_strings(
            audit,
            security_categories=security_categories,
            ticker_categories=ticker_categories,
            parent_version_id=parent_version_id,
        )
        audits.append(audit)
        snapshots[pd.Timestamp(snapshot_date)] = audit.loc[audit["eligible"]].copy()

    membership, forced_audits = _compact_membership_timeline(
        snapshots=snapshots,
        master=master,
        sessions=sessions,
        parent_version_id=parent_version_id,
        start=start,
        target=target,
    )
    audits.extend(forced_audits)
    eligibility = (
        pd.concat(audits, ignore_index=True)
        .sort_values(["date", "security_id"])
        .reset_index(drop=True)
    )
    membership = _compact_eligibility_strings(
        membership,
        security_categories=security_categories,
        ticker_categories=ticker_categories,
        parent_version_id=parent_version_id,
    )
    eligibility = _compact_eligibility_strings(
        eligibility,
        security_categories=security_categories,
        ticker_categories=ticker_categories,
        parent_version_id=parent_version_id,
    )

    active_membership = membership.loc[membership["active"]].copy()
    merged = active_membership.merge(
        master[["security_id", "listing_date", "delisting_date", "asset_type"]],
        on="security_id",
        how="left",
        validate="many_to_one",
    )
    future_listing_rows = int(merged["date"].lt(merged["listing_date"]).sum())
    post_delist_rows = int(merged["date"].gt(merged["delisting_date"]).sum())
    monthly_members = membership.loc[membership["snapshot_type"].eq("MONTH_END")]
    monthly_audit_members = eligibility.loc[
        eligibility["snapshot_type"].eq("MONTH_END") & eligibility["eligible"]
    ]
    left_keys = set(
        zip(monthly_members["date"], monthly_members["security_id"], strict=False)
    )
    right_keys = set(
        zip(
            monthly_audit_members["date"],
            monthly_audit_members["security_id"],
            strict=False,
        )
    )
    first_snapshot = pd.Timestamp(monthly_members["date"].min())
    checks = (
        QualityCheck(
            "unique_membership_identity",
            not membership.duplicated(["date", "security_id"]).any(),
            int(membership.duplicated(["date", "security_id"]).sum()),
            0,
            "membership date/security keys are unique",
        ),
        QualityCheck(
            "membership_matches_monthly_eligibility",
            left_keys == right_keys,
            {"membership": len(left_keys), "eligibility": len(right_keys)},
            "exact",
            "month-end membership is exactly reproducible from eligibility",
        ),
        QualityCheck(
            "no_future_listing_members",
            future_listing_rows == 0,
            future_listing_rows,
            0,
            "no current list was backfilled before its listing date",
        ),
        QualityCheck(
            "no_post_delisting_members",
            post_delist_rows == 0,
            post_delist_rows,
            0,
            "members exit after their final confirmed trading date",
        ),
        QualityCheck(
            "ordinary_equity_only",
            set(merged["asset_type"].dropna().astype(str)).issubset(
                {"STOCK", "ADR"} if include_adr else {"STOCK"}
            ),
            sorted(merged["asset_type"].dropna().astype(str).unique()),
            ["STOCK"] if not include_adr else ["STOCK", "ADR"],
            "membership respects the approved asset-type policy",
        ),
        QualityCheck(
            "membership_baseline",
            first_snapshot <= study_start,
            first_snapshot.date().isoformat(),
            f"<= {study_start.date().isoformat()}",
            "membership baseline reaches the formal research start",
        ),
        QualityCheck(
            "latest_snapshot_not_future",
            membership["date"].max() <= target,
            membership["date"].max().date().isoformat(),
            target.date().isoformat(),
            "latest membership snapshot is valid for the target session",
        ),
    )
    methodology = {
        "methodology_version": "US_LIQUID_5M_PIT_V2_COMPACT_EVENTS",
        "reconstitution": "XNYS_MONTH_END_PLUS_CONFIRMED_DELISTING_EXIT",
        "membership_storage": "COMPLETE_MONTH_END_PLUS_REMOVAL_EVENTS",
        "adv_price": "UNADJUSTED_CLOSE",
        "adv_sessions": int(adv_sessions),
        "min_valid_sessions": int(min_valid_sessions),
        "min_price": float(min_price),
        "min_adv20_usd": float(min_adv20_usd),
        "include_adr": bool(include_adr),
        "history_start": start.date().isoformat(),
        "research_start": study_start.date().isoformat(),
        "target_session": target.date().isoformat(),
        "parent_dataset_version_id": parent_version_id,
    }
    return LiquidUniverseCandidate(
        target_session=target,
        membership=membership,
        eligibility=eligibility,
        checks=checks,
        methodology=methodology,
    )


def roll_forward_liquid_5m_candidate(
    previous_membership: pd.DataFrame,
    previous_eligibility: pd.DataFrame,
    security_master: pd.DataFrame,
    *,
    parent_version_id: str,
    previous_target_session: str | pd.Timestamp,
    target_session: str | pd.Timestamp,
    refresh_start: str | pd.Timestamp,
    history_start: str | pd.Timestamp,
    research_start: str | pd.Timestamp,
    symbol_history: pd.DataFrame | None = None,
    min_price: float = 1.0,
    min_adv20_usd: float = 5_000_000.0,
    adv_sessions: int = 20,
    min_valid_sessions: int = 15,
    include_adr: bool = False,
    calendar: Any | None = None,
    bar_loader: Callable[[pd.Timestamp, pd.Timestamp], pd.DataFrame] | None = None,
) -> LiquidUniverseCandidate:
    """Rebuild only snapshots whose inputs can intersect the overlap window."""
    target = pd.Timestamp(target_session).normalize()
    previous_target = pd.Timestamp(previous_target_session).normalize()
    refresh = pd.Timestamp(refresh_start).normalize()
    start = pd.Timestamp(history_start).normalize()
    study_start = pd.Timestamp(research_start).normalize()
    if not previous_target < target:
        raise ValueError("roll-forward target must be after the previous target")
    if refresh > target or refresh < start:
        raise ValueError("refresh_start is outside the coverage range")
    if bar_loader is None:
        raise ValueError("roll-forward requires a bounded bar_loader")
    calendar = _xnys_calendar(calendar)
    sessions = _sessions(calendar, start, target)
    if target not in sessions:
        raise DataFoundationError(f"{target.date()} is not an XNYS session")
    master = _normalize_master(security_master)
    symbols = _normalize_symbols(symbol_history)
    security_categories = sorted(master["security_id"].astype(str).unique())
    ticker_categories = sorted(
        set(master["current_ticker"].astype(str))
        | set(symbols["ticker"].astype(str))
    )

    membership = previous_membership.copy()
    eligibility = previous_eligibility.copy()
    for frame, label in ((membership, "membership"), (eligibility, "eligibility")):
        for column in (
            "security_id", "ticker", "reason_codes", "snapshot_type",
            "source_data_version_id",
        ):
            if column in frame.columns and isinstance(
                frame[column].dtype, pd.CategoricalDtype
            ):
                frame[column] = frame[column].astype(str)
        if "date" not in frame.columns or "source_data_version_id" not in frame.columns:
            raise DataFoundationError(f"previous {label} contract is incomplete")
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
        if frame["date"].isna().any() or frame["date"].gt(previous_target).any():
            raise DataFoundationError(f"previous {label} contains invalid dates")
    if membership.duplicated(["date", "security_id"]).any():
        raise DataFoundationError("previous membership contains duplicate identities")
    if eligibility.duplicated(["date", "security_id"]).any():
        raise DataFoundationError("previous eligibility contains duplicate identities")

    retained_membership = membership.loc[membership["date"].lt(refresh)].copy()
    retained_eligibility = eligibility.loc[eligibility["date"].lt(refresh)].copy()
    retained_complete = retained_membership.loc[
        ~retained_membership["snapshot_type"].astype(str).eq("FORCED_EXIT")
    ].copy()
    snapshots: dict[pd.Timestamp, pd.DataFrame] = {
        pd.Timestamp(snapshot_date): rows.copy()
        for snapshot_date, rows in retained_complete.groupby("date", sort=True)
    }
    audits: list[pd.DataFrame] = (
        [retained_eligibility] if not retained_eligibility.empty else []
    )

    extended_sessions = _sessions(calendar, start, target + pd.Timedelta(days=40))
    month_ends = _month_end_sessions(extended_sessions)
    month_ends = month_ends[(month_ends >= refresh) & (month_ends <= target)]
    for snapshot_date in month_ends:
        snapshot_date = pd.Timestamp(snapshot_date).normalize()
        position = sessions.searchsorted(snapshot_date, side="right")
        if position < adv_sessions:
            continue
        window_dates = sessions[max(0, position - adv_sessions):position]
        snapshot_bars = _normalize_bars(
            bar_loader(pd.Timestamp(window_dates.min()), snapshot_date),
            snapshot_date,
        )
        audit = _evaluate_month_end(
            date=snapshot_date,
            sessions=sessions,
            bars=snapshot_bars,
            master=master,
            symbols=symbols,
            parent_version_id=parent_version_id,
            adv_sessions=adv_sessions,
            min_valid_sessions=min_valid_sessions,
            min_price=min_price,
            min_adv20_usd=min_adv20_usd,
            include_adr=include_adr,
        )
        audits.append(audit)
        snapshots[snapshot_date] = audit.loc[audit["eligible"]].copy()

    refresh_position = sessions.searchsorted(refresh, side="left") - 1
    if refresh_position < 0:
        raise DataFoundationError("roll-forward has no membership baseline")
    initial_active = resolve_membership_asof(
        retained_membership,
        pd.Timestamp(sessions[refresh_position]),
    )
    new_snapshots = {
        date: rows for date, rows in snapshots.items() if date >= refresh
    }
    rebuilt_membership, forced_audits = _compact_membership_timeline(
        snapshots=new_snapshots,
        master=master,
        sessions=sessions,
        parent_version_id=parent_version_id,
        start=refresh,
        target=target,
        initial_active=initial_active,
    )
    audits.extend(forced_audits)
    rolled_membership = (
        pd.concat([retained_membership, rebuilt_membership], ignore_index=True)
        .assign(source_data_version_id=parent_version_id)
        .sort_values(["date", "security_id"])
        .reset_index(drop=True)
    )
    rolled_eligibility = (
        pd.concat(audits, ignore_index=True)
        .assign(source_data_version_id=parent_version_id)
        .sort_values(["date", "security_id"])
        .reset_index(drop=True)
    )
    rolled_membership = _compact_eligibility_strings(
        rolled_membership,
        security_categories=security_categories,
        ticker_categories=ticker_categories,
        parent_version_id=parent_version_id,
    )
    rolled_eligibility = _compact_eligibility_strings(
        rolled_eligibility,
        security_categories=security_categories,
        ticker_categories=ticker_categories,
        parent_version_id=parent_version_id,
    )

    active_membership = rolled_membership.loc[rolled_membership["active"]].copy()
    merged = active_membership.merge(
        master[["security_id", "listing_date", "delisting_date", "asset_type"]],
        on="security_id",
        how="left",
        validate="many_to_one",
    )
    future_listing_rows = int(merged["date"].lt(merged["listing_date"]).sum())
    post_delist_rows = int(merged["date"].gt(merged["delisting_date"]).sum())
    monthly_members = rolled_membership.loc[
        rolled_membership["snapshot_type"].eq("MONTH_END")
    ]
    monthly_audit_members = rolled_eligibility.loc[
        rolled_eligibility["snapshot_type"].eq("MONTH_END")
        & rolled_eligibility["eligible"]
    ]
    member_keys = set(zip(monthly_members["date"], monthly_members["security_id"]))
    audit_keys = set(zip(monthly_audit_members["date"], monthly_audit_members["security_id"]))
    first_snapshot = pd.Timestamp(monthly_members["date"].min())
    comparison_columns = [
        column
        for column in retained_membership.columns
        if column != "source_data_version_id"
        and column in rolled_membership.columns
    ]
    old_retained = (
        retained_membership.loc[:, comparison_columns]
        .sort_values(["date", "security_id"])
        .reset_index(drop=True)
    )
    new_retained = (
        rolled_membership.loc[rolled_membership["date"].lt(refresh), comparison_columns]
        .sort_values(["date", "security_id"])
        .reset_index(drop=True)
    )
    for frame in (old_retained, new_retained):
        for column in comparison_columns:
            if isinstance(frame[column].dtype, pd.CategoricalDtype):
                frame[column] = frame[column].astype(str)
    retained_equal = old_retained.equals(new_retained)
    checks = (
        QualityCheck(
            "unique_membership_identity",
            not rolled_membership.duplicated(["date", "security_id"]).any(),
            int(rolled_membership.duplicated(["date", "security_id"]).sum()),
            0,
            "membership date/security keys are unique",
        ),
        QualityCheck(
            "membership_matches_monthly_eligibility",
            member_keys == audit_keys,
            {"membership": len(member_keys), "eligibility": len(audit_keys)},
            "exact",
            "month-end membership is exactly reproducible from eligibility",
        ),
        QualityCheck(
            "no_future_listing_members",
            future_listing_rows == 0,
            future_listing_rows,
            0,
            "no current list was backfilled before its listing date",
        ),
        QualityCheck(
            "no_post_delisting_members",
            post_delist_rows == 0,
            post_delist_rows,
            0,
            "members exit after their final confirmed trading date",
        ),
        QualityCheck(
            "ordinary_equity_only",
            set(merged["asset_type"].dropna().astype(str)).issubset(
                {"STOCK", "ADR"} if include_adr else {"STOCK"}
            ),
            sorted(merged["asset_type"].dropna().astype(str).unique()),
            ["STOCK"] if not include_adr else ["STOCK", "ADR"],
            "membership respects the approved asset-type policy",
        ),
        QualityCheck(
            "membership_baseline",
            first_snapshot <= study_start,
            first_snapshot.date().isoformat(),
            f"<= {study_start.date().isoformat()}",
            "membership baseline reaches the formal research start",
        ),
        QualityCheck(
            "latest_snapshot_not_future",
            rolled_membership["date"].max() <= target,
            rolled_membership["date"].max().date().isoformat(),
            target.date().isoformat(),
            "latest membership snapshot is valid for the target session",
        ),
        QualityCheck(
            "retained_snapshots_are_unchanged",
            retained_equal,
            {"previous_rows": len(old_retained), "rolled_rows": len(new_retained)},
            "exact",
            "snapshots before the overlap boundary were carried without recomputation",
        ),
    )
    methodology = {
        "methodology_version": "US_LIQUID_5M_PIT_V2_COMPACT_EVENTS",
        "reconstitution": "XNYS_MONTH_END_PLUS_CONFIRMED_DELISTING_EXIT",
        "membership_storage": "COMPLETE_MONTH_END_PLUS_REMOVAL_EVENTS",
        "calculation_mode": "INCREMENTAL_OVERLAP_REBUILD",
        "refresh_start": refresh.date().isoformat(),
        "previous_target_session": previous_target.date().isoformat(),
        "adv_price": "UNADJUSTED_CLOSE",
        "adv_sessions": int(adv_sessions),
        "min_valid_sessions": int(min_valid_sessions),
        "min_price": float(min_price),
        "min_adv20_usd": float(min_adv20_usd),
        "include_adr": bool(include_adr),
        "history_start": start.date().isoformat(),
        "research_start": study_start.date().isoformat(),
        "target_session": target.date().isoformat(),
        "parent_dataset_version_id": parent_version_id,
    }
    return LiquidUniverseCandidate(
        target_session=target,
        membership=rolled_membership,
        eligibility=rolled_eligibility,
        checks=checks,
        methodology=methodology,
    )


__all__ = [
    "ELIGIBILITY_COLUMNS",
    "LiquidUniverseCandidate",
    "build_liquid_5m_candidate",
    "historical_pit_bar_coverage_check",
    "roll_forward_liquid_5m_candidate",
]
