"""Bounded price-first discovery. Provisional watches never approve price/volume contracts."""
from collections import Counter, defaultdict
from datetime import datetime, time as day_time, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from . import latency
from .market_session import xnys_session_schedule, previous_xnys_sessions
from .models import NEW_YORK, digest, encode, timestamp, ticker
from .pipeline import eligibility


def load_baselines(session, now, *, catalog_path=None):
    from src.data.access import load_published_daily_data
    from src.data.foundation import MarketDataCatalog, MarketDataReader
    previous = previous_xnys_sessions(session, 1)[0]
    bundle = load_published_daily_data(requested_universe='US_EQUITY_COVERAGE',
        start=previous, end=previous, min_latest_coverage=0,
        reader=MarketDataReader(catalog=MarketDataCatalog(catalog_path)) if catalog_path else None)
    if bundle.version.created_at > now or bundle.contract.target_session != previous:
        raise ValueError('DAILY_BASELINE_STALE_OR_FUTURE')
    contract = bundle.contract.to_dict()
    contract_id = digest(contract)
    rows = {}
    for row in bundle.bars.to_dict('records'):
        if str(row['date'])[:10] != previous or not number(row.get('close')) or row['close'] <= 0:
            continue
        symbol = row['ticker']
        if symbol in rows:
            raise ValueError('AMBIGUOUS_DAILY_BASELINE')
        rows[symbol] = {'close': float(row['close']), 'session': previous,
                        'version_id': contract['dataset_version_id'], 'contract_id': contract_id}
    return rows, contract


def number(value):
    return type(value) in {int, float} and math.isfinite(value)


class PriceStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and 'ep_price_scans' not in tables:
                raise ValueError('DEDICATED_PRICE_DATABASE_REQUIRED')
            db.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS ep_price_scans(id TEXT PRIMARY KEY,started_at TEXT,payload TEXT);
                CREATE TABLE IF NOT EXISTS ep_price_rows(
                    id TEXT PRIMARY KEY,scan_id TEXT,ticker TEXT,observed_at TEXT,payload TEXT);
                CREATE TABLE IF NOT EXISTS ep_price_contracts(id TEXT PRIMARY KEY,payload TEXT);
                CREATE INDEX IF NOT EXISTS ep_price_ticker ON ep_price_rows(ticker,observed_at);''')

    def save_contract(self, contract):
        contract_id = digest(contract)
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('INSERT OR IGNORE INTO ep_price_contracts VALUES(?,?)', (contract_id, encode(contract)))
        coverage = contract.get('coverage', {})
        return {'contract_id': contract_id, 'dataset_version_id': contract.get('dataset_version_id'),
                'target_session': contract.get('target_session'),
                'latest_coverage': coverage.get('latest_coverage'),
                'observed_ticker_count': len(coverage.get('observed_tickers', []))}

    def save(self, scan, rows, summary):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('INSERT OR REPLACE INTO ep_price_scans VALUES(?,?,?)', (scan, summary['started_at'], encode(summary)))
            db.executemany('INSERT OR IGNORE INTO ep_price_rows VALUES(?,?,?,?,?)',
                           [(r['receipt_id'], scan, r['ticker'], r['observed_at'], encode(r)) for r in rows])


def inspect_price(symbol, raw, baseline, now, *, premarket, gap_threshold):
    session = now.astimezone(NEW_YORK).date().isoformat()
    schedule = xnys_session_schedule(session)
    start = datetime.combine(now.astimezone(NEW_YORK).date(), day_time(4), NEW_YORK)
    result = {'ticker': symbol, 'session': session, 'observed_at': timestamp(now),
              'market_phase': 'PREMARKET' if premarket else 'REGULAR',
              'change_basis': 'CURRENT_PRICE_VS_PREVIOUS_SESSION_EXECUTION_CLOSE',
              'status': 'BLOCKED', 'reason': 'NO_PRICE_RECORD', 'baseline': baseline,
              'is_trade_signal': False, 'eligible_for_rating': False,
              'price_contract_verified': False, 'adjustment_verified': False,
              'volume_status': 'UNKNOWN', 'catalyst_status': 'UNKNOWN', 'indicative_gap_pct': None}
    if raw is None:
        return result
    # Whitelist scalar numeric fields. Do not retain provider error strings or credentials.
    result['raw'] = {k: raw[k] for k in ('price', 'timestamp') if number(raw.get(k))}
    if raw.get('symbol') != symbol or not number(raw.get('price')) or raw['price'] <= 0:
        return {**result, 'reason': 'INVALID_PRICE_OR_IDENTITY'}
    epoch = raw.get('timestamp')
    scale = 1000 if premarket else 1
    if not number(epoch) or not 1e9 <= epoch / scale < 1e10:
        return {**result, 'reason': 'PRICE_TIMESTAMP_UNIT_INVALID'}
    at = datetime.fromtimestamp(epoch / scale, timezone.utc)
    result['price_at'] = timestamp(at)
    result['price_age_seconds'] = (now - at).total_seconds()
    if not start <= at < schedule.closes_at or not 0 <= (now - at).total_seconds() <= 120:
        return {**result, 'reason': 'STALE_FUTURE_OR_OUT_OF_SESSION_PRICE'}
    if (premarket and at >= schedule.opens_at) or (not premarket and at < schedule.opens_at):
        return {**result, 'reason': 'PRICE_MARKET_PHASE_MISMATCH'}
    if (not baseline or baseline.get('session') != previous_xnys_sessions(session, 1)[0]
            or not number(baseline.get('close')) or baseline['close'] <= 0 or not baseline.get('contract_id')):
        return {**result, 'reason': 'PREVIOUS_SESSION_BASELINE_UNAVAILABLE'}
    gap = (raw['price'] / baseline['close'] - 1) * 100
    return {**result, 'status': 'PRICE_WATCH' if gap >= gap_threshold else 'BELOW_THRESHOLD',
            'reason': 'PRICE_AND_CORPORATE_ACTIONS_PENDING_ACCEPTANCE', 'indicative_gap_pct': gap}


def discover(queue, price_store, snapshot, provider, config, *, clock, baselines_loader=None,
             monotonic=time.monotonic):
    now = clock()
    session = now.astimezone(NEW_YORK).date().isoformat()
    scan = str(uuid4())
    summary = {'scan_id': scan, 'started_at': timestamp(now), 'status': 'OUTSIDE_DISCOVERY_WINDOW',
               'http_requests': 0, 'llm_requests': 0, 'discord_messages': 0, 'market_complete': False}
    try:
        schedule = xnys_session_schedule(session)
    except ValueError:
        price_store.save(scan, [], summary)
        return summary
    if not datetime.combine(now.astimezone(NEW_YORK).date(), day_time(4), NEW_YORK) <= now < schedule.closes_at:
        price_store.save(scan, [], summary)
        return summary
    profiles = {s: p for s in snapshot.profiles if (p := snapshot.profile(s, now)) and eligibility(p) is None
                and s not in snapshot.rejected}
    unsupported = [s for s in profiles if not re.fullmatch(r'[A-Z0-9][A-Z0-9-]{0,19}', s)]
    profiles = {s: p for s, p in profiles.items() if s not in unsupported}
    symbols = sorted(profiles)
    summary.update(universe_size=len(symbols), identity_provenance=snapshot.provenance,
                   unsupported_provider_symbols=unsupported,
                   excluded_or_unresolved_identity=len(snapshot.profiles) - len(symbols) + len(snapshot.rejected))
    if not symbols:
        summary['status'] = 'CURRENT_SECURITY_IDENTITIES_UNAVAILABLE'
        price_store.save(scan, [], summary)
        return summary
    deadline = monotonic() + config.price_deadline_seconds
    try:
        baselines, contract = (baselines_loader(session, now) if baselines_loader else
            load_baselines(session, now, catalog_path=getattr(config, 'identity_catalog_path', None) or None))
    except Exception as exc:
        coverage = getattr(exc, 'coverage', None)
        summary.update(status='BASELINE_UNAVAILABLE', error_type=type(exc).__name__,
                       contract_failures=list(coverage.failures) if coverage else [],
                       finished_at=timestamp(clock()))
        price_store.save(scan, [], summary)
        queue.save_checkpoint('price:last_scan', summary, clock())
        return summary
    baseline_summary = price_store.save_contract(contract)
    generation = digest([session, symbols, snapshot.provenance])
    cursor = queue.checkpoint('price:cursor') or {}
    offset = cursor.get('offset', 0) if cursor.get('generation') == generation else 0
    if not isinstance(offset, int) or not 0 <= offset < len(symbols):
        offset = 0
    sweep_started = cursor.get('sweep_started_at') if offset else timestamp(now)
    summary.update(status='PARTIAL_SWEEP', universe_generation=generation, offset_start=offset,
                   baseline_contract=baseline_summary, sweep_started_at=sweep_started,
                   baseline_load_seconds=(clock() - now).total_seconds())
    counts = Counter()
    attempted = 0
    with queue.connection() as db:
        previous_candidates = {r[0].removeprefix('price:candidate:') for r in db.execute(
            "SELECT name FROM checkpoints WHERE name LIKE 'price:candidate:%'")}
    for _ in range(config.price_batches_per_cycle):
        remaining = deadline - monotonic()
        if remaining < 1 or clock() >= schedule.closes_at:
            summary['stop_reason'] = 'SCAN_BUDGET_OR_SESSION_ENDED'
            break
        chunk = symbols[offset:offset + 100]
        premarket = clock() < schedule.opens_at
        try:
            summary['http_requests'] += 1
            rows = provider.prices(chunk, premarket=premarket, timeout=min(8, remaining))
            if not isinstance(rows, list) or len(rows) > 200 or any(not isinstance(r, dict) for r in rows):
                raise ValueError('INVALID_PRICE_BATCH')
            grouped = defaultdict(list)
            for row in rows:
                if row.get('symbol') in chunk:
                    grouped[row['symbol']].append(row)
            fetched = clock()
            results = []
            for symbol in chunk:
                matches = grouped[symbol]
                result = inspect_price(symbol, matches[0] if len(matches) == 1 else None,
                    baselines.get(symbol), fetched, premarket=premarket, gap_threshold=config.price_gap_threshold)
                if len(matches) > 1:
                    result['reason'] = 'AMBIGUOUS_PRICE_RECORDS'
                result['receipt_id'] = digest([scan, symbol, result])
                results.append(result)
            counts.update(r['status'] + ':' + r['reason'] for r in results)
            price_store.save(scan, results, summary)
            for result in results:
                symbol = result['ticker']
                if result['status'] == 'PRICE_WATCH':
                    previous_candidates.add(symbol)
                    queue.save_checkpoint('price:candidate:' + symbol, result, fetched)
                    latency.record(queue, symbol, session, 'PRICE_DISCOVERED', session, fetched,
                                   {'receipt_id': result['receipt_id'], 'price_at': result['price_at'],
                                    'indicative_gap_pct': result['indicative_gap_pct'], 'confirmed': False})
                    queue.enqueue('NEWS', symbol, digest(['price-news', symbol, session]), 'v1',
                                  {'session': session, 'price_receipt_id': result['receipt_id']}, fetched, schedule.closes_at)
                elif symbol in previous_candidates:
                    queue.save_checkpoint('price:candidate:' + symbol, result, fetched)
            attempted += len(chunk)
            offset += len(chunk)
            complete = offset >= len(symbols)
            queue.save_checkpoint('price:cursor', {'generation': generation, 'offset': 0 if complete else offset,
                                  'sweep_started_at': sweep_started}, fetched)
            if complete:
                summary['status'] = 'UNIVERSE_SWEEP_ATTEMPTED_NOT_VERIFIED_COVERAGE'
                break
        except Exception as exc:
            # Do not expose provider exception messages (they may contain authenticated URLs).
            code = getattr(getattr(exc, 'response', None), 'status_code', None)
            summary.update(status='PRICE_FETCH_FAILED_CURSOR_RETAINED', error_type=type(exc).__name__,
                           http_status=code if type(code) is int else None)
            break
    summary.update(finished_at=timestamp(clock()), attempted_symbols=attempted, next_offset=0 if offset >= len(symbols) else offset,
                   remaining_symbols=max(0, len(symbols) - offset), counts=dict(counts),
                   counts_scope='THIS_CYCLE_ONLY',
                   elapsed_seconds=(clock() - now).total_seconds())
    price_store.save(scan, [], summary)
    queue.save_checkpoint('price:last_scan', summary, clock())
    return summary


def price_priority(queue, symbol, now):
    result = queue.checkpoint('price:candidate:' + symbol)
    if not result or result['status'] != 'PRICE_WATCH':
        return 0
    age = (now - datetime.fromisoformat(result['price_at'])).total_seconds()
    return 50 + min(result['indicative_gap_pct'], 40) if 0 <= age <= 120 else 0


def price_status(path, symbol, as_of):
    symbol = ticker(symbol)
    if not path or not Path(path).is_file():
        return {'ticker': symbol, 'status': 'PRICE_DATABASE_NOT_INITIALIZED', 'external_requests': 0}
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT payload FROM ep_price_rows WHERE ticker=? AND observed_at<=? ORDER BY observed_at DESC,rowid DESC LIMIT 1',
                         (symbol, timestamp(as_of))).fetchone()
        scan = db.execute("SELECT payload FROM ep_price_scans WHERE json_extract(payload,'$.finished_at')<=? ORDER BY started_at DESC LIMIT 1",
                          (timestamp(as_of),)).fetchone()
    return {'ticker': symbol, 'observation': json.loads(row[0]) if row else None,
            'last_scan': json.loads(scan[0]) if scan else None,
            'status': 'OBSERVED_PRICE_HISTORY' if row else 'NOT_SCANNED_IN_OBSERVED_SCOPE', 'external_requests': 0}
