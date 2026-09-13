"""Check bounded same-provider alternatives; successful records stay audit-only."""
import hashlib
import json
from pathlib import Path
import sys
import time

import requests


def main():
    root, output = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    sys.path.insert(0, str(root))
    from src.data.fmp import get_api_key
    from src.utils.env import load_local_env
    from src.breakouts.live.settings import IntradayMonitorSettings
    load_local_env('/etc/quant/intraday-momentum-monitor.env')
    assert IntradayMonitorSettings.load().cup_handle_delivery_enabled is False
    output.mkdir(parents=True, exist_ok=False)
    key = get_api_key()
    inventory = []
    blocked_legacy = False
    jobs = [('older-single', t, '1min', '2026-09-08') for t in ('UAN','IBTA','WBI','SPY')]
    jobs += [('legacy', t, i, '2026-09-11') for t in ('SPY','UAN','IBTA','WBI') for i in ('1min','5min')]
    for mode, ticker, interval, day in jobs:
        if mode == 'legacy' and blocked_legacy:
            continue
        path = (f'/api/v3/historical-chart/{interval}/{ticker}' if mode == 'legacy'
                else f'/stable/historical-chart/{interval}')
        params = {'from': day, 'to': day}
        if mode != 'legacy':
            params['symbol'] = ticker
        row = {'mode': mode, 'ticker': ticker, 'interval': interval, 'date': day, 'path': path}
        try:
            response = requests.get('https://financialmodelingprep.com' + path,
                                    params={**params, 'apikey': key}, timeout=(10,35), allow_redirects=False)
            row.update(http_status=response.status_code, raw_sha256=hashlib.sha256(response.content).hexdigest(),
                       response_date=response.headers.get('Date'), bytes=len(response.content))
            if mode == 'legacy' and response.status_code in (401,402,403):
                blocked_legacy = True
            response.raise_for_status()
            assert key.encode() not in response.content
            payload = response.json()
            assert isinstance(payload, list) and all(isinstance(r,dict) for r in payload)
            (output / f'{ticker}-{interval}-{mode}.raw.json').write_bytes(response.content)
            row.update(status='RECEIVED', rows=len(payload), dates=sorted({r['date'][:10] for r in payload}))
        except Exception as exc:
            row.update(status='FAILED', error_type=type(exc).__name__)
        inventory.append(row)
        (output / 'inventory.json').write_text(json.dumps(inventory, indent=2))
        print(json.dumps(row), flush=True)
        time.sleep(1)


if __name__ == '__main__':
    main()
