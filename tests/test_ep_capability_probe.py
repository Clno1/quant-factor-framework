import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_ep_radar import NOW

PATH = Path(__file__).resolve().parents[1] / 'reviews/2026-09-11-ep-sg-capability/volume_probe.py'
spec = importlib.util.spec_from_file_location('ep_volume_probe', PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_volume_probe_keeps_distinct_scopes_and_does_not_infer_contract(tmp_path):
    calls = []
    def records(endpoint, params, **kwargs):
        calls.append(endpoint)
        if 'historical-chart' in endpoint:
            return []
        symbols = [params['symbol']] if 'symbol' in params else params['symbols'].split(',')
        return [{'symbol': s, 'volume': 101 if 'aftermarket' in endpoint else 100,
                 'tradeSize': 0.5, 'timestamp': 1789084799000} for s in symbols]
    result = probe.collect(tmp_path, provider=SimpleNamespace(_ep_records=records), clock=lambda: NOW)
    assert len(calls) == result['requests'] == 9
    assert not result['contract_verified']
    assert result['llm_requests'] == result['discord_messages'] == 0
    assert all(r['extended_equals_previous_eod'] is False for r in result['comparisons'])
    assert result['rows'][0]['sample'][0]['tradeSize'] == .5
    assert all(r['status'] == 'NO_RECORDS' for r in result['rows'] if r['kind'] == 'CURRENT_MINUTE')


@pytest.mark.parametrize('code', [401, 403, 429])
def test_volume_probe_stops_after_provider_refusal(tmp_path, code):
    calls = []
    def refused(*args, **kwargs):
        calls.append(args)
        error = ValueError('credential must not be logged')
        error.response = SimpleNamespace(status_code=code)
        raise error
    result = probe.collect(tmp_path, provider=SimpleNamespace(_ep_records=refused), clock=lambda: NOW)
    assert len(calls) == result['requests'] == 1
    assert 'credential' not in str(result)
    assert all(r['status'] == 'PROVIDER_ACCESS_STOPPED' for r in result['rows'][1:])


def test_volume_probe_rejects_cross_symbol_rows(tmp_path):
    def records(endpoint, params, **kwargs):
        return [{'symbol': 'GTLB', 'volume': 100}]
    result = probe.collect(tmp_path, provider=SimpleNamespace(_ep_records=records), clock=lambda: NOW)
    assert all(r['status'] == 'FETCH_FAILED' for r in result['rows'] if r.get('ticker') == 'AAPL')
