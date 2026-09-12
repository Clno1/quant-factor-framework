"""Price-first research WATCH, independent of catalyst and volume confirmation."""
from datetime import datetime, timedelta, time
import json
from typing import Literal

from pydantic import Field, field_validator

from .llm_contract import StrictModel
from .market_session import xnys_session_schedule, previous_xnys_sessions
from .models import NEW_YORK, digest, encode, ticker, timestamp


class PriceObservation(StrictModel):
    ticker: str
    session: str
    price: float = Field(gt=0, allow_inf_nan=False)
    previous_close: float = Field(gt=0, allow_inf_nan=False)
    previous_session: str
    price_at: datetime
    observed_at: datetime
    price_contract_verified: bool = False
    adjustment_verified: bool = False
    security_identity_verified: bool = False
    asset_type: Literal['STOCK', 'ADR', 'ETF', 'UNKNOWN'] = 'UNKNOWN'
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    catalyst_status: Literal['UNKNOWN', 'SOURCE_MATCHED', 'CONFIRMED'] = 'UNKNOWN'
    catalyst_evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    volume_status: Literal['UNKNOWN', 'VERIFIED'] = 'UNKNOWN'
    volume_evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    premarket_volume: int | None = Field(default=None, ge=0)
    premarket_dollar_volume: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    premarket_rvol: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator('ticker')
    @classmethod
    def symbol(cls, value):
        return ticker(value)

    @field_validator('price_at', 'observed_at', mode='before')
    @classmethod
    def aware_time(cls, value):
        value = datetime.fromisoformat(value) if isinstance(value, str) else value
        timestamp(value)
        return value


def evaluate_watch(observation, as_of, *, gap_threshold=4):
    timestamp(as_of)
    row = PriceObservation.model_validate(observation)
    blockers = []
    if not row.security_identity_verified or row.asset_type not in {'STOCK', 'ADR'}:
        blockers.append('VERIFIED_STOCK_OR_ADR_REQUIRED')
    session = as_of.astimezone(NEW_YORK).date().isoformat()
    schedule = xnys_session_schedule(session)
    preopen = datetime.combine(as_of.astimezone(NEW_YORK).date(), time(4), NEW_YORK)
    if not preopen <= row.price_at < schedule.closes_at or row.session != session:
        blockers.append('PRICE_SESSION_MISMATCH')
    if row.previous_session != previous_xnys_sessions(session, 1)[0]:
        blockers.append('PREVIOUS_CLOSE_SESSION_MISMATCH')
    if not row.price_at <= row.observed_at <= as_of or as_of - row.price_at > timedelta(seconds=120):
        blockers.append('STALE_OR_FUTURE_PRICE')
    if not row.price_contract_verified or not row.adjustment_verified:
        blockers.append('PRICE_AND_ADJUSTMENT_CONTRACT_REQUIRED')
    if row.catalyst_status != 'UNKNOWN' and not row.catalyst_evidence_ids:
        blockers.append('CATALYST_STATUS_EVIDENCE_REQUIRED')
    if row.volume_status == 'VERIFIED' and (not row.volume_evidence_ids or any(
            v is None for v in (row.premarket_volume, row.premarket_dollar_volume, row.premarket_rvol))):
        blockers.append('VOLUME_CONTRACT_EVIDENCE_REQUIRED')
    gap = (row.price / row.previous_close - 1) * 100 if not blockers else None
    result = {'ticker': row.ticker, 'session': row.session, 'as_of': timestamp(as_of),
              'price_at': timestamp(row.price_at), 'observed_at': timestamp(row.observed_at),
              'family': 'EP_WATCH', 'status': 'BLOCKED' if blockers else 'WATCH' if gap >= gap_threshold else 'BELOW_THRESHOLD',
              'gap_pct': gap, 'price': row.price if not blockers else None, 'blockers': blockers,
              'catalyst_status': row.catalyst_status, 'evidence_ids': row.evidence_ids,
              'catalyst_evidence_ids': row.catalyst_evidence_ids, 'volume_status': row.volume_status,
              'volume_evidence_ids': row.volume_evidence_ids, 'is_trade_signal': False,
              'eligible_for_rating': False, 'threshold_policy': {'watch_gap_pct': gap_threshold}}
    for key in ('premarket_volume', 'premarket_dollar_volume', 'premarket_rvol'):
        result[key] = getattr(row, key) if row.volume_status == 'VERIFIED' and not blockers else None
    result['snapshot_id'] = digest([row.model_dump(mode='json'), result['status'], blockers])
    return result


def record_watch(queue, observation, now):
    result = evaluate_watch(observation, now)
    queue._writable()
    with queue.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        duplicate = db.execute('SELECT * FROM watch_history WHERE snapshot_id=?', (result['snapshot_id'],)).fetchone()
        if duplicate:
            return {**json.loads(duplicate['payload']), 'changed': False, 'duplicate': True}
        old = db.execute('SELECT payload FROM checkpoints WHERE name=?', ('market:' + result['ticker'],)).fetchone()
        previous = json.loads(old[0]) if old else None
        if previous and result['observed_at'] <= previous['observed_at']:
            reason = 'OUT_OF_ORDER_NOT_APPLIED'
            changed = False
        else:
            old = db.execute('''SELECT payload FROM watch_history WHERE ticker=?
                AND json_extract(payload,'$.session')=? AND change_reason NOT IN ('UNCHANGED','OUT_OF_ORDER_NOT_APPLIED')
                ORDER BY observed_at DESC LIMIT 1''', (result['ticker'], result['session'])).fetchone()
            baseline = json.loads(old[0]) if old else None
            reason = 'INITIAL_OBSERVATION'
            if baseline:
                reason = 'UNCHANGED'
                for field in ('status', 'catalyst_status', 'volume_status'):
                    if result[field] != baseline[field]:
                        reason = field.upper() + '_CHANGED'
                        break
                if reason == 'UNCHANGED' and result['gap_pct'] is not None and baseline['gap_pct'] is not None:
                    delta = result['gap_pct'] - baseline['gap_pct']
                    if abs(delta) >= 2:
                        reason = 'GAP_EXPANDED' if delta > 0 else 'GAP_FADED'
                if (reason == 'UNCHANGED' and result['premarket_rvol'] is not None
                        and baseline['premarket_rvol'] is not None
                        and abs(result['premarket_rvol'] - baseline['premarket_rvol']) >= 2):
                    reason = 'RVOL_CHANGED'
            changed = reason != 'UNCHANGED'
            db.execute('''INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(name)
                DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload''',
                ('market:' + result['ticker'], timestamp(now), encode(result)))
        result.update(changed=changed, change_reason=reason, delivery='NOT_DISPATCHED', recorded_at=timestamp(now))
        db.execute('INSERT INTO watch_history VALUES(?,?,?,?,?)',
                   (result['snapshot_id'], result['ticker'], timestamp(now), encode(result), reason))
    return result


def source_priority(queue, symbol, now):
    from .price_discovery import price_priority
    snapshot = queue.checkpoint('market:' + symbol)
    if not snapshot or snapshot.get('status') != 'WATCH':
        return price_priority(queue, symbol, now)
    at = datetime.fromisoformat(snapshot['price_at'])
    if not timedelta(0) <= now - at <= timedelta(seconds=120):
        return price_priority(queue, symbol, now)
    return 100 + min(snapshot['gap_pct'], 100)


def ingest_watch_file(queue, path, now):
    """Normalized input only. Never reinterpret a trade's size as daily volume."""
    from pathlib import Path
    path = Path(path)
    if path.stat().st_size > 5_000_000:
        raise ValueError('WATCH_INPUT_TOO_LARGE')
    bundle = json.loads(path.read_text())
    if bundle.get('version') != 'ep-price-watch-v1' or not isinstance(bundle.get('observations'), list):
        raise ValueError('WATCH_INPUT_CONTRACT_REQUIRED')
    if len(bundle['observations']) > 5000:
        raise ValueError('WATCH_INPUT_CAPACITY_EXCEEDED')
    outcomes = []
    for row in bundle['observations']:
        try:
            outcomes.append(record_watch(queue, row, now))
        except (ValueError, TypeError, KeyError):
            outcomes.append({'status': 'BLOCKED', 'reason': 'INVALID_PRICE_OBSERVATION'})
    return {'items': outcomes, 'external_requests': 0, 'discord_messages': 0,
            'coverage': bundle.get('coverage', 'PROVIDED_SNAPSHOTS_ONLY')}
