"""Bounded-memory, version-pinned input for the unchanged momentum evaluator.

This composition adapter belongs to the premarket integration layer, not the
group domain or breakout framework. Neither core imports this module.
Preflight covers EVERY requested security. Only one batch of frames is retained.
"""
from collections.abc import Mapping

import pandas as pd

from src.breakouts.daily_data import BreakoutDailyDataset, daily_frames_from_bars, load_breakout_daily_dataset
from src.data.access import DataCoverage, MarketDataNotReadyError
from src.data.foundation import DataFoundationError, MarketDataReader
from src.data.universe_ids import US_EQUITY_COVERAGE, US_LIQUID_5M, resolve_market_data_universe

BATCH_SIZE = 50


class BatchFrames(Mapping):
    """A bounded cache implementing the .get contract of BreakoutDailyDataset."""

    def __init__(self, tickers, load_batch, batch_size=BATCH_SIZE):
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.tickers = tuple(tickers)
        self.positions = {ticker: i // batch_size for i, ticker in enumerate(self.tickers)}
        self.batch_size = batch_size
        self.load_batch = load_batch
        self.cached_batch = None
        self.cache = {}

    def __len__(self):
        return len(self.tickers)

    def __iter__(self):
        return iter(self.tickers)

    def __getitem__(self, ticker):
        batch = self.positions[ticker]
        if batch != self.cached_batch:
            self.cache = {}  # release the previous batch BEFORE allocating the next
            self.cached_batch = None
            start = batch * self.batch_size
            self.cache = self.load_batch(self.tickers[start:start + self.batch_size])
            self.cached_batch = batch
        return self.cache.get(ticker, pd.DataFrame())


class PartitionQuery:
    """Short-lived in-memory SQL engine; 64 MB budget excludes pandas copies."""

    def __init__(self, paths, metadata, start, end):
        self.paths = [str(path) for path in paths]
        self.ids = metadata.set_index("ticker").security_id.astype(str).to_dict()
        self.ticker_by_id = {v: k for k, v in self.ids.items()}
        self.start, self.end = pd.Timestamp(start), pd.Timestamp(end)

    def read(self, tickers, *, summary=False):
        import duckdb
        ids = [self.ids[t] for t in tickers]
        conn = duckdb.connect()
        try:
            conn.execute("SET threads=1")
            conn.execute("SET memory_limit='64MB'")
            # Never spill inside the project's working directory.
            conn.execute("SET temp_directory=''")
            conn.read_parquet(self.paths, union_by_name=True).create_view("bars")
            placeholders = ",".join("?" for _ in ids)
            where = f"security_id IN ({placeholders}) AND date >= ? AND date <= ?"
            columns = (
                "security_id, min(date) AS first_date, max(date) AS last_date, "
                "count(*) AS n, count(open) AS n_open"
                if summary else 'date,security_id,open,high,low,close,adj_close,volume'
            )
            sql = f"SELECT {columns} FROM bars WHERE {where}"
            if summary:
                sql += " GROUP BY security_id"
            frame = conn.execute(sql, [*ids, self.start.date(), self.end.date()]).df()
        finally:
            conn.close()
        frame["ticker"] = frame.security_id.astype(str).map(self.ticker_by_id)
        return frame

    def frames(self, tickers):
        return daily_frames_from_bars(self.read(tickers))


def load_rotation_momentum_dataset(*, requested_universe, ticker_selector, end,
                                   min_latest_coverage, reader=None, batch_size=BATCH_SIZE):
    """Same bars, 400-day window, metadata and coverage gates as the eager loader."""
    if resolve_market_data_universe(requested_universe) != US_LIQUID_5M:
        return load_breakout_daily_dataset(
            requested_universe=requested_universe, ticker_selector=ticker_selector,
            end=end, min_latest_coverage=min_latest_coverage, reader=reader,
        )
    from src.breakouts.broad_daily_data import _resolve_context, _contract

    market = reader or MarketDataReader()
    parent, manifest, uv, members, coverage_members, expected = _resolve_context(
        reader=market, dataset_version_id=None, end=end,
    )
    requested = list(dict.fromkeys(str(t).strip().upper() for t in ticker_selector(members.copy())))
    if not requested or "" in requested:
        raise DataFoundationError("Empty rotation momentum selection")
    metadata = members.loc[members.ticker.isin(requested)].copy()
    missing = set(requested) - set(metadata.ticker)
    if missing:
        support = coverage_members.loc[
            coverage_members.ticker.isin(missing)
            & coverage_members.asset_type.astype(str).str.upper().eq("ETF")
            & coverage_members.is_current_coverage.fillna(False).astype(bool)
        ].copy()
        for col in ("selection_price", "adv20_usd", "valid_sessions_20d", "current_dollar_volume"):
            support[col] = pd.NA
        support["sector"], support["sub_industry"] = "", ""
        support["reason_codes"] = "EXPLICIT_BENCHMARK_SUPPORT"
        metadata = pd.concat([metadata, support], ignore_index=True, sort=False)
    if set(requested) != set(metadata.ticker):
        raise DataFoundationError("Unresolved rotation momentum securities")
    metadata = metadata.drop_duplicates("ticker", keep="last").set_index("ticker").loc[requested].reset_index()
    start = expected - pd.Timedelta(days=400)
    # Full verification once, before any batch is consumed. Never re-resolve latest.
    market.verify_version(parent, require_price_semantics=True)
    query = PartitionQuery(market.partition_paths(parent, start=start, end=expected), metadata, start, expected)
    summaries = [query.read(requested[i:i + batch_size], summary=True)
                 for i in range(0, len(requested), batch_size)]
    stats = pd.concat(summaries, ignore_index=True)
    observed = sorted(stats.ticker.tolist())
    latest = set(stats.loc[pd.to_datetime(stats.last_date).eq(expected), "ticker"])
    ratio = len(latest) / len(requested)
    missing = sorted(set(requested) - set(observed))
    failures = []
    if stats.empty:
        failures.append("no_bars")
    if missing and min_latest_coverage >= 1:
        failures.append("missing_tickers")
    if ratio < min_latest_coverage:
        failures.append("latest_session_coverage")
    coverage = DataCoverage(
        data_universe=US_EQUITY_COVERAGE, requested_tickers=tuple(requested),
        observed_tickers=tuple(observed), missing_tickers=tuple(missing), unexpected_tickers=(),
        requested_start=start.date().isoformat(), requested_end=expected.date().isoformat(),
        required_history_start=None, membership_start=None, expected_session=expected.date().isoformat(),
        observed_session=pd.Timestamp(stats.last_date.max()).date().isoformat() if len(stats) else None,
        latest_coverage=ratio, open_coverage=float(stats.n_open.sum() / stats.n.sum()) if len(stats) else 0.,
        min_date_by_ticker={r.ticker: pd.Timestamp(r.first_date).date().isoformat() for r in stats.itertuples()},
        max_date_by_ticker={r.ticker: pd.Timestamp(r.last_date).date().isoformat() for r in stats.itertuples()},
        passed=not failures, failures=tuple(failures),
    )
    if failures:
        raise MarketDataNotReadyError("Rotation momentum coverage failed", data_universe=US_EQUITY_COVERAGE, coverage=coverage)
    contract = _contract(requested_universe=requested_universe, parent=parent,
                         parent_manifest=manifest, universe_version=uv, coverage=coverage)
    return BreakoutDailyDataset(
        requested_universe=requested_universe, data_universe=US_LIQUID_5M,
        version=parent, contract=contract, universe=metadata,
        frames=BatchFrames(requested, query.frames, batch_size),
    )
