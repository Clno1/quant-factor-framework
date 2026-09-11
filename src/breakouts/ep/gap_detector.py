"""EP gap-through-cup opening confirmation, independent of cup-handle logic."""
from datetime import datetime, timedelta
import math

from pydantic import Field

from .llm_contract import StrictModel
from .market import MarketContract, SessionTape, rank_candidate, window
from .models import digest
from .market_session import xnys_session_schedule, expected_source_session


class CupReference(StrictModel):
    ticker: str
    source_session: str
    snapshot_id: str
    eligible: bool = False
    rim: float = Field(gt=0, allow_inf_nan=False)
    previous_close: float = Field(gt=0, allow_inf_nan=False)


def confirm_gap(metrics, tape, contract, catalyst, cup=None, *, opening_minutes=5):
    if opening_minutes not in {1, 5, 15}:
        raise ValueError('UNSUPPORTED_OPENING_WINDOW')
    tape, contract = SessionTape.model_validate(tape), MarketContract.model_validate(contract)
    now = datetime.fromisoformat(metrics['as_of'])
    schedule = xnys_session_schedule(metrics['session'])
    result = {'family': 'EP_WATCH', 'status': 'WATCH_ONLY', 'reasons': [], 'delivery': 'DISABLED_SHADOW_ONLY'}
    if cup is None:
        result['reasons'].append('NO_DAILY_CUP_REFERENCE')
        return result
    cup = CupReference.model_validate(cup)
    if not math.isclose(cup.previous_close, metrics['previous_close'], rel_tol=1e-8):
        result['reasons'].append('CUP_REFERENCE_PRICE_MISMATCH')
        return result
    if (not cup.eligible or not cup.snapshot_id or cup.source_session != expected_source_session(metrics['session'])
            or cup.ticker != tape.ticker or tape.ticker != metrics['ticker'] or tape.session != metrics['session']):
        result['reasons'].append('FROZEN_T1_CUP_REQUIRED')
        return result
    rank = rank_candidate(metrics, catalyst)
    result['reasons'] += rank['blockers'] + contract.blockers()
    if rank['grade'] not in {'STRONG', 'MODERATE'}:
        result['reasons'].append('MARKET_AND_CATALYST_QUALITY_NOT_CONFIRMED')
    if not schedule.opens_at <= now < schedule.closes_at:
        result['reasons'].append('OUTSIDE_REGULAR_SESSION')
    if result['reasons']:
        return result
    cutoff = now.replace(second=0, microsecond=0)
    opening_end = schedule.opens_at + timedelta(minutes=opening_minutes)
    if cutoff < opening_end + timedelta(minutes=1):
        result['reasons'].append('WAIT_FOR_BAR_AFTER_OPENING_RANGE')
        return result
    try:
        bars = window(tape, schedule.opens_at, cutoff, contract)
        # Sparse or missing opening bars cannot define a dependable ORH.
        if len(bars) != int((cutoff - schedule.opens_at).total_seconds() // 60):
            raise ValueError('REGULAR_MINUTE_GAP')
        opening = [b for b in bars if b.starts_at < opening_end]
        last, prior = bars[-1], bars[-2]
        if not cup.previous_close <= cup.rim < bars[0].open:
            raise ValueError('NOT_AN_OVERNIGHT_GAP_THROUGH_CUP')
        result['family'] = 'EP_GAP_THROUGH_CUP'
        level = max(b.high for b in opening)
        volume = sum(b.volume for b in bars)
        vwap = sum(b.dollar_volume for b in bars) / volume if volume else None
        if not prior.close <= level < last.close:
            raise ValueError('NO_NEW_CLOSED_BAR_ORH_CROSS')
        if vwap is None or last.close <= vwap or last.close <= cup.rim:
            raise ValueError('VWAP_OR_CUP_RIM_NOT_HELD')
        if metrics.get('regular_rvol') is None or metrics['regular_rvol'] < 2:
            raise ValueError('REGULAR_RVOL_NOT_CONFIRMED')
        stop = min(b.low for b in bars)
        risk = (last.close - stop) / last.close
        if not 0 < risk <= .08:
            raise ValueError('DAY_LOW_STOP_TOO_DISTANT')
        result.update(status='SHADOW_CONFIRMED', opening_minutes=opening_minutes, orh=level,
                      vwap=vwap, price=last.close, stop_reference=stop, risk_fraction=risk,
                      confirmed_at=(last.starts_at + timedelta(minutes=1)).isoformat(),
                      signal_id=digest(['EP_GAP_THROUGH_CUP', tape.ticker, tape.session, cup.snapshot_id, opening_minutes]))
    except ValueError as exc:
        result['reasons'].append(str(exc))
    return result
