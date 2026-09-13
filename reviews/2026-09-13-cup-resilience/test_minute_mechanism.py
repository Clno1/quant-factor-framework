import importlib.util
from pathlib import Path

import pandas as pd
import pytest

spec = importlib.util.spec_from_file_location('mechanism', Path(__file__).with_name('minute_mechanism.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def frames():
    one = pd.DataFrame({'open': [10]*5, 'high': [11]*5, 'low': [9]*5,
                        'close': [10]*5, 'volume': [100.0]*5},
                       index=pd.date_range('2026-09-11 09:30', periods=5, freq='min'))
    five = one.iloc[[0]].copy()
    five['volume'] = 500
    return one, five


def test_identical_complete_aggregate():
    one, five = frames()
    result = module.compare(one, five, '2026-09-11')
    assert result['complete_positive']['all_ohlcv_equal'] == 1


def test_numeric_serialization_difference_is_not_revision():
    one, _ = frames()
    other = one.astype(float)
    assert module.values_equal(one, other)
    other.loc[other.index[0], 'close'] += 0.001
    assert not module.values_equal(one, other)


def test_missing_minutes_not_complete_or_filled():
    one, five = frames()
    result = module.compare(one.iloc[:1], five, '2026-09-11')
    assert result['complete_positive']['pairs'] == 0
    assert result['valid_observed']['different_by_field']['volume'] == 1


@pytest.mark.parametrize('value', [0, -1, float('nan'), float('inf')])
def test_bad_volume_cannot_be_hidden(value):
    one, five = frames()
    one.loc[one.index[2], 'volume'] = value
    result = module.compare(one, five, '2026-09-11')
    assert result['invalid_one_rows'] == 1
    assert result['complete_positive']['pairs'] == 0


def test_end_label_hypothesis_does_not_mutate_input():
    one, five = frames()
    five.index += pd.Timedelta(minutes=5)
    before = module.fingerprint(one)
    assert module.compare(one, five, '2026-09-11', 5)['complete_positive']['all_ohlcv_equal'] == 1
    assert module.fingerprint(one) == before


def test_duplicates_reject_comparison():
    one, five = frames()
    assert module.compare(pd.concat([one, one.iloc[:1]]), five, '2026-09-11')['comparable'] is False


def test_off_grid_minutes_are_not_complete():
    one, five = frames()
    one.index += pd.Timedelta(seconds=1)
    assert module.compare(one, five, '2026-09-11')['complete_positive']['pairs'] == 0


def test_identical_empty_responses_do_not_prove_full_day():
    one, five = frames()
    result = module.compare(one.iloc[:0], five.iloc[:0], '2026-09-11')
    assert result['complete_positive']['pairs'] == 0
    assert len(result['one_missing_buckets']) == 78
