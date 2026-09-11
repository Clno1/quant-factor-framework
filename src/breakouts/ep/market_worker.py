"""Bounded raw market collection and durable shadow reports; never sends alerts."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from .models import digest, ticker, NEW_YORK
from .market import evaluate_market, rank_candidate
from .gap_detector import confirm_gap
from .market_quality import inspect_market_records


class MarketShadowStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and 'ep_market_receipts' not in tables:
                raise ValueError('DEDICATED_EP_MARKET_DATABASE_REQUIRED')
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS ep_market_receipts(
                    id TEXT PRIMARY KEY, ticker TEXT NOT NULL, kind TEXT NOT NULL,
                    observed_at TEXT NOT NULL, payload_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS ep_market_ticker ON ep_market_receipts(ticker, observed_at);
                CREATE TABLE IF NOT EXISTS ep_market_signals(
                    id TEXT PRIMARY KEY, first_seen TEXT NOT NULL, payload_json TEXT NOT NULL);
            ''')

    @contextmanager
    def connection(self):
        with sqlite3.connect(self.path, timeout=10) as db:
            yield db

    def save(self, symbol, kind, payload, now):
        content = json.dumps(payload, sort_keys=True, allow_nan=False)
        key = digest([symbol, kind, now.isoformat(), payload])
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO ep_market_receipts VALUES(?,?,?,?,?)',
                       (key, symbol, kind, now.isoformat(), content))
            if kind == 'SHADOW_REPORT':
                signal = payload.get('confirmation', {})
                if signal.get('status') == 'SHADOW_CONFIRMED':
                    db.execute('INSERT OR IGNORE INTO ep_market_signals VALUES(?,?,?)',
                               (signal['signal_id'], now.isoformat(), json.dumps(signal, allow_nan=False)))
        return key


def collect_market(store, symbols, *, now=None, provider=None, monotonic=time.monotonic):
    from src.data import fmp
    provider = provider or fmp
    live_clock = now is None
    now = now or datetime.now(timezone.utc)
    symbols = list(dict.fromkeys(ticker(s) for s in symbols))
    if not 1 <= len(symbols) <= 5 or now.tzinfo is None:
        raise ValueError('ONE_TO_FIVE_SYMBOLS_AND_AWARE_TIME_REQUIRED')
    end = monotonic() + 90
    allowed = {'symbol', 'price', 'size', 'tradeSize', 'timestamp', 'bidPrice', 'askPrice', 'bidSize', 'askSize',
               'date', 'open', 'high', 'low', 'close', 'volume', 'vwap'}
    outcomes, calls, stop_code = [], 0, None
    session = now.astimezone(NEW_YORK).date().isoformat()
    requests = [('EXTENDED_TRADE', None), ('EXTENDED_QUOTE', None)] + [('MINUTE_DAY', s) for s in symbols]
    for kind, symbol in requests:
        remaining = end - monotonic()
        if remaining <= 0 or stop_code:
            for name in ([symbol] if symbol else symbols):
                payload = {'status': 'PROVIDER_ACCESS_STOPPED' if stop_code else 'CAPACITY_DEFERRED',
                           'http_status': stop_code, 'eligible_for_rating': False, 'records': []}
                rid = store.save(name, kind, payload, now)
                outcomes.append({'kind': kind, 'ticker': name, **payload, 'receipt_id': rid})
            continue
        timeout = min(10, remaining)
        try:
            calls += 1
            rows = (provider.get_ep_minute_day(symbol, session, timeout=timeout)
                    if symbol else provider.get_ep_extended_batch(symbols,
                    kind='trade' if kind == 'EXTENDED_TRADE' else 'quote', timeout=timeout))
            if len(rows) > 1000 or any(not isinstance(r, dict) for r in rows):
                raise ValueError('BOUNDED_MARKET_RECORDS_REQUIRED')
            if symbol and any(r.get('symbol') not in {None, symbol} for r in rows):
                raise ValueError('MINUTE_RESPONSE_TICKER_MISMATCH')
            received = datetime.now(timezone.utc) if live_clock else now
            for name in ([symbol] if symbol else symbols):
                scoped = rows if symbol else [r for r in rows if r.get('symbol') == name]
                data = [{k: v for k, v in row.items() if k in allowed and type(v) in {str, int, float, type(None)}
                         and (not isinstance(v, str) or len(v) < 100)} for row in scoped]
                payload = {'status': 'RAW_UNVERIFIED' if data else 'NO_RECORDS', 'records': data,
                           'normalization': 'TIMEZONE_VOLUME_AND_COVERAGE_NOT_ASSUMED',
                           'eligible_for_rating': False, 'delivery': 'DISABLED_SHADOW_ONLY',
                           'diagnostics': inspect_market_records(kind, data, session=session, as_of=received)}
                receipt = store.save(name, kind, payload, received)
                outcomes.append({'ticker': name, 'kind': kind, 'status': payload['status'],
                                 'record_count': len(data), 'receipt_id': receipt,
                                 'diagnostics': payload['diagnostics']})
        except Exception as exc:
            # Never persist provider error strings, request URLs, or credential echoes.
            code = getattr(getattr(exc, 'response', None), 'status_code', None)
            code = code if type(code) is int and 100 <= code <= 599 else None
            if code in {401, 403, 429}:
                stop_code = code
            for name in ([symbol] if symbol else symbols):
                payload = {'status': 'MARKET_FETCH_FAILED', 'error_type': type(exc).__name__,
                           'http_status': code, 'records': [], 'eligible_for_rating': False}
                rid = store.save(name, kind, payload, now)
                outcomes.append({'ticker': name, 'kind': kind, **payload, 'receipt_id': rid})
    return {'outcomes': outcomes, 'external_requests': calls, 'mode': 'RAW_SHADOW',
            'confirmation_blockers': ['MARKET_DATA_CONTRACT_NOT_VERIFIED'], 'delivery': 'DISABLED_SHADOW_ONLY'}


def replay_market(bundle):
    now = datetime.fromisoformat(bundle['as_of'])
    metrics = evaluate_market(bundle['contract'], bundle['current'], bundle['history'], as_of=now,
                              previous_close=bundle['previous_close'], previous_session=bundle['previous_session'])
    ranking = rank_candidate(metrics, bundle['catalyst'])
    confirmation = confirm_gap(metrics, bundle['current'], bundle['contract'], bundle['catalyst'],
                               bundle.get('cup'), opening_minutes=bundle.get('opening_minutes', 5))
    return {'metrics': metrics, 'ranking': ranking, 'confirmation': confirmation,
            'input_revision': digest(bundle), 'external_requests': 0, 'delivery': 'DISABLED_SHADOW_ONLY'}
