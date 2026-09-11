"""Contract-gated EP market metrics. No cup detection, model calls or delivery."""
from datetime import datetime, timedelta, time
import math
from statistics import mean
from typing import Literal

from pydantic import Field, model_validator, field_validator

from .market_session import xnys_session_schedule, previous_xnys_sessions
from .llm_contract import StrictModel
from .models import NEW_YORK, digest


class MarketContract(StrictModel):
    revision: str
    evidence_ids: list[str] = Field(default_factory=list)
    timezone_verified: bool = False
    minute_volume_verified: bool = False
    extended_hours_verified: bool = False
    dollar_turnover_verified: bool = False
    adjustment_verified: bool = False

    def blockers(self):
        flags = ('timezone_verified', 'minute_volume_verified', 'extended_hours_verified',
                 'dollar_turnover_verified', 'adjustment_verified')
        return [name.upper() + '_REQUIRED' for name in flags if not getattr(self, name)] + (
            [] if self.evidence_ids else ['MARKET_CONTRACT_EVIDENCE_REQUIRED'])


class Minute(StrictModel):
    starts_at: datetime
    open: float = Field(gt=0, allow_inf_nan=False)
    high: float = Field(gt=0, allow_inf_nan=False)
    low: float = Field(gt=0, allow_inf_nan=False)
    close: float = Field(gt=0, allow_inf_nan=False)
    volume: int = Field(ge=0)
    dollar_volume: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator('starts_at', mode='before')
    @classmethod
    def iso_time(cls, value):
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    @model_validator(mode='after')
    def consistent(self):
        if self.starts_at.tzinfo is None or self.starts_at.second or self.starts_at.microsecond:
            raise ValueError('AWARE_MINUTE_START_REQUIRED')
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError('INVALID_OHLC')
        if self.dollar_volume is not None and not self.low * self.volume - .01 <= self.dollar_volume <= self.high * self.volume + .01:
            raise ValueError('TURNOVER_OUTSIDE_BAR_PRICE_RANGE')
        return self


class SessionTape(StrictModel):
    ticker: str
    session: str
    contract_revision: str
    covered_from: datetime
    covered_until: datetime
    coverage_verified: bool = False
    bars: list[Minute] = Field(max_length=1000)

    @field_validator('covered_from', 'covered_until', mode='before')
    @classmethod
    def iso_time(cls, value):
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    @model_validator(mode='after')
    def consistent(self):
        if self.covered_from.tzinfo is None or self.covered_until.tzinfo is None or self.covered_from > self.covered_until:
            raise ValueError('AWARE_COVERAGE_INTERVAL_REQUIRED')
        stamps = [b.starts_at for b in self.bars]
        if len(set(stamps)) != len(stamps):
            raise ValueError('DUPLICATE_MINUTES_REQUIRE_RECONCILIATION')
        if any(b.starts_at.astimezone(NEW_YORK).date().isoformat() != self.session for b in self.bars):
            raise ValueError('BAR_SESSION_MISMATCH')
        return self


def window(tape, start, end, contract):
    if (not tape.coverage_verified or tape.covered_from > start or tape.covered_until < end
            or tape.contract_revision != contract.revision):
        raise ValueError('WINDOW_COVERAGE_NOT_VERIFIED')
    bars = sorted((b for b in tape.bars if start <= b.starts_at and b.starts_at + timedelta(minutes=1) <= end),
                  key=lambda b: b.starts_at)
    if not bars:
        raise ValueError('EMPTY_WINDOW_NOT_ASSUMED_ZERO')
    if any(b.dollar_volume is None for b in bars):
        raise ValueError('TRUE_DOLLAR_TURNOVER_REQUIRED')
    return bars


def evaluate_market(contract, current, history, *, as_of, previous_close, previous_session):
    contract = MarketContract.model_validate(contract)
    current = SessionTape.model_validate(current)
    history = [SessionTape.model_validate(t) for t in history]
    if as_of.tzinfo is None or not math.isfinite(previous_close) or previous_close <= 0:
        raise ValueError('VALID_ASOF_AND_PREVIOUS_CLOSE_REQUIRED')
    now = as_of.astimezone(NEW_YORK)
    day = now.date().isoformat()
    result = {'ticker': current.ticker, 'session': day, 'as_of': now.isoformat(),
              'previous_close': previous_close, 'previous_session': previous_session,
              'contract_revision': contract.revision, 'status': 'BLOCKED', 'blockers': contract.blockers(),
              'gap_pct': None, 'premarket_volume': None, 'premarket_dollar_volume': None,
              'premarket_rvol': None, 'regular_rvol': None, 'regular_vwap': None,
              'delivery': 'DISABLED_SHADOW_ONLY'}
    schedule = xnys_session_schedule(day)
    expected_history = previous_xnys_sessions(day, 20)
    if current.session != day or previous_session != expected_history[-1]:
        result['blockers'].append('PREVIOUS_CLOSE_OR_CURRENT_SESSION_MISMATCH')
    if len({t.session for t in history}) != len(history):
        result['blockers'].append('DUPLICATE_HISTORY_SESSIONS')
    if result['blockers']:
        return result
    start = datetime.combine(now.date(), time(4), NEW_YORK)
    cutoff = min(now.replace(second=0, microsecond=0), schedule.opens_at)
    regular_end = min(now.replace(second=0, microsecond=0), schedule.closes_at)
    hist = {t.session: t for t in history if t.ticker == current.ticker}
    try:
        if cutoff <= start:
            raise ValueError('PREMARKET_NOT_STARTED')
        pre = window(current, start, cutoff, contract)
        if cutoff - (pre[-1].starts_at + timedelta(minutes=1)) > timedelta(minutes=2):
            raise ValueError('PREMARKET_PRICE_STALE')
        volume = sum(b.volume for b in pre)
        result.update(gap_pct=(pre[-1].close / previous_close - 1) * 100,
                      premarket_volume=volume, premarket_dollar_volume=sum(b.dollar_volume for b in pre),
                      gap_price_at=(pre[-1].starts_at + timedelta(minutes=1)).isoformat())
        pre_baselines, regular_baselines = [], []
        for date in expected_history:
            if date not in hist:
                raise ValueError('TWENTY_SAME_CLOCK_HISTORY_SESSIONS_REQUIRED')
            old_start = datetime.combine(datetime.fromisoformat(date).date(), time(4), NEW_YORK)
            old_cutoff = old_start + (cutoff - start)
            pre_baselines.append(sum(b.volume for b in window(hist[date], old_start, old_cutoff, contract)))
            if regular_end > schedule.opens_at:
                old_open = xnys_session_schedule(date).opens_at
                old_end = old_open + (regular_end - schedule.opens_at)
                if old_end > xnys_session_schedule(date).closes_at:
                    raise ValueError('HISTORICAL_EARLY_CLOSE_NOT_COMPARABLE')
                regular_baselines.append(sum(b.volume for b in window(hist[date], old_open, old_end, contract)))
        if mean(pre_baselines) <= 0:
            raise ValueError('ZERO_PREMARKET_BASELINE')
        result['premarket_rvol'] = volume / mean(pre_baselines)
        if regular_end > schedule.opens_at:
            regular = window(current, schedule.opens_at, regular_end, contract)
            regular_volume = sum(b.volume for b in regular)
            if regular_volume <= 0 or mean(regular_baselines) <= 0:
                raise ValueError('ZERO_REGULAR_VOLUME')
            if regular_end - (regular[-1].starts_at + timedelta(minutes=1)) > timedelta(minutes=1):
                raise ValueError('REGULAR_PRICE_STALE')
            result.update(regular_rvol=regular_volume / mean(regular_baselines),
                          regular_vwap=sum(b.dollar_volume for b in regular) / regular_volume)
        result['status'] = 'METRICS_AVAILABLE_SHADOW_ONLY'
    except ValueError as exc:
        result['blockers'].append(str(exc))
    result['snapshot_id'] = digest(result)
    return result


class CatalystGate(StrictModel):
    ticker: str
    session: str
    evidence_ids: list[str] = Field(default_factory=list)
    direct: bool = False
    fresh: bool = False
    source_verified: bool = False
    quality: Literal['STRONG', 'MODERATE', 'UNKNOWN'] = 'UNKNOWN'


def rank_candidate(metrics, catalyst):
    catalyst = CatalystGate.model_validate(catalyst)
    blockers = list(metrics['blockers'])
    if (catalyst.ticker != metrics['ticker'] or catalyst.session != metrics['session']
            or not catalyst.source_verified or not catalyst.direct or not catalyst.fresh or not catalyst.evidence_ids):
        blockers.append('FRESH_DIRECT_VERIFIED_CATALYST_REQUIRED')
    if metrics.get('gap_pct') is None or metrics['gap_pct'] < 5:
        blockers.append('GAP_BELOW_WATCH_THRESHOLD')
    grade = 'HEADS_UP'
    if not blockers:
        liquid = metrics['premarket_dollar_volume'] >= 1_000_000 and metrics['premarket_rvol'] >= 3
        grade = ('STRONG' if liquid and metrics['gap_pct'] >= 10 and catalyst.quality == 'STRONG'
                 else 'MODERATE' if liquid and catalyst.quality in {'STRONG', 'MODERATE'} else 'WATCH')
    return {'grade': grade, 'blockers': blockers, 'mode': 'SHADOW', 'is_trade_signal': False}
