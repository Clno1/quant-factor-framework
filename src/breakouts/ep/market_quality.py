"""Raw FMP diagnostics, never a volume contract approval or a trading signal."""
from datetime import datetime, timedelta, timezone
import math

from .market_session import xnys_session_schedule
from .models import NEW_YORK


def _number(value):
    return type(value) in {int, float} and math.isfinite(value)


def inspect_market_records(kind, rows, *, session, as_of):
    reasons = set()
    result = {'contract_verified': False, 'eligible_for_rating': False}
    if not rows:
        return {**result, 'reasons': ['NO_RECORDS_NOT_PROOF_OF_NO_TRADING']}
    if kind == 'MINUTE_DAY':
        labels = []
        for row in rows:
            try:
                value = datetime.strptime(row['date'], '%Y-%m-%d %H:%M:%S')
                if value.second or value.date().isoformat() != session:
                    raise ValueError('SESSION_OR_MINUTE_START_MISMATCH')
                labels.append(value.strftime('%H:%M'))
            except (ValueError, TypeError, KeyError):
                reasons.add('MINUTE_DATE_OR_SESSION_INVALID')
        reasons.update({'MINUTE_TIMEZONE_UNVERIFIED', 'MINUTE_VOLUME_SCOPE_UNVERIFIED',
                        'TRUE_DOLLAR_TURNOVER_NOT_PROVIDED'})
        if len(labels) != len(set(labels)):
            reasons.add('DUPLICATE_MINUTE_LABELS')
        premarket = sum('04:00' <= at < '09:30' for at in labels)
        fractional = sum(_number(row.get('volume')) and row['volume'] % 1 != 0 for row in rows)
        if not premarket:
            reasons.add('PREMARKET_MINUTE_COVERAGE_NOT_OBSERVED')
        if fractional:
            reasons.add('FRACTIONAL_VOLUME_OBSERVED')
        if any(not _number(row.get('volume')) or row['volume'] < 0 for row in rows):
            reasons.add('MINUTE_VOLUME_INVALID')
        try:
            schedule = xnys_session_schedule(session)
        except ValueError:
            schedule = None
            reasons.add('NOT_AN_XNYS_SESSION')
        missing, windows = {}, {}
        if schedule:
            for width in (1, 5, 15):
                expected = {(schedule.opens_at + timedelta(minutes=i)).strftime('%H:%M') for i in range(width)}
                missing[str(width)] = sorted(expected - set(labels))
                closed = as_of >= schedule.opens_at + timedelta(minutes=width)
                windows[str(width)] = ('NOT_YET_CLOSED' if not closed else
                                       'MISSING_LABELS' if missing[str(width)] else 'LABELS_PRESENT')
                if closed and missing[str(width)]:
                    reasons.add(f'OPENING_{width}M_LABELS_INCOMPLETE')
        result.update(label_timezone='UNVERIFIED_EXCHANGE_LOCAL_INTERPRETATION',
            premarket_label_count=premarket, fractional_volume_rows=fractional,
            missing_opening_labels=missing, opening_window_states=windows)
    else:
        reasons.add('TRADE_SIZE_NOT_CUMULATIVE_VOLUME' if kind == 'EXTENDED_TRADE' else 'QUOTE_VOLUME_SCOPE_UNVERIFIED')
        times = []
        for row in rows:
            epoch = row.get('timestamp')
            if not _number(epoch) or not 1e12 <= epoch < 1e13:
                reasons.add('EXTENDED_TIMESTAMP_MILLISECONDS_REQUIRED')
                continue
            at = datetime.fromtimestamp(epoch / 1000, timezone.utc)
            age = (as_of - at).total_seconds()
            times.append({'at': at.isoformat(), 'age_seconds': round(age, 3)})
            if age < 0:
                reasons.add('FUTURE_EXTENDED_SNAPSHOT')
            elif age > 120:
                reasons.add('STALE_EXTENDED_SNAPSHOT')
            if at.astimezone(NEW_YORK).date().isoformat() != session:
                reasons.add('EXTENDED_SNAPSHOT_NOT_CURRENT_SESSION')
            if kind == 'EXTENDED_QUOTE':
                volume = row.get('volume')
                reasons.add('QUOTE_FIELD_TIMESTAMPS_NOT_PROVIDED')
                if not _number(volume) or volume < 0:
                    reasons.add('QUOTE_VOLUME_INVALID')
                elif volume % 1 != 0:
                    reasons.add('FRACTIONAL_QUOTE_VOLUME_OBSERVED')
                bid, ask = row.get('bidPrice'), row.get('askPrice')
                if not _number(bid) or not _number(ask) or bid <= 0 or ask < bid:
                    reasons.add('INVALID_BID_ASK')
                else:
                    spread = (ask - bid) / ((ask + bid) / 2) * 100
                    result['spread_pct'] = spread
                    if spread > 5:
                        reasons.add('WIDE_SPREAD_OVER_5_PERCENT')
        result['snapshot_times'] = times
    return {**result, 'reasons': sorted(reasons)}
