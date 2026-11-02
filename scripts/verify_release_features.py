"""Exercise rc.2 features through a frozen EXE using disposable artificial data."""
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.portfolio_models import serialize
from backend.replay_models import Evidence, utc_now
from backend.replay_storage import write_cache

VERSION = '4.0.0-rc.2'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New test directory, retained for inspection')
    args = parser.parse_args()
    exe = args.exe.resolve()
    assert exe.is_file(), exe
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    home = root / 'home'
    data = home / 'data'
    offline = data / 'a_market_offline'
    offline.mkdir(parents=True)
    # These prices and weekdays are artificial test observations, never shipped.
    codes = ['600519', '300750', '688571']
    names = {code: f'ARTIFICIAL RELEASE TEST {code}' for code in codes}
    (data / 'stock_names.json').write_text(json.dumps(names), encoding='utf-8')
    (data / 'stock_list.csv').write_text('code,name\n' + ''.join(f'{code},{names[code]}\n' for code in codes), encoding='utf-8')
    dates = pd.bdate_range('2024-01-02', '2024-06-28')
    evidence = Evidence('2024-01-02', '2024-06-28', 'artificial-release-test', 'rc2-fixture-v1',
                        'Artificial weekdays and prices for isolated package verification', utc_now())
    write_cache(data / 'portfolio_replay' / 'calendar.json', {
        'provider': 'sina-public-calendar-v1', 'dates': [str(value.date()) for value in dates],
        'evidence': serialize(evidence)})
    for index, code in enumerate(codes):
        price = (index + 1) * 10
        frame = pd.DataFrame({'date': dates, 'open': price, 'close': price,
                              'high': price + 1, 'low': price - 1, 'volume': 10000, 'factor': 1})
        frame.to_csv(offline / f'{code}.{"SZ" if code.startswith("3") else "SH"}.csv', index=False)
    ready_file = root / 'ready.json'
    env = {key: value for key, value in os.environ.items() if not key.startswith(('KLINE_TRAINER_', 'PYTHON'))}
    session = requests.Session()
    session.trust_env = False
    process = None
    base = None

    def call(method, path, body=None, expected=200):
        response = session.request(method, base + path, json=body, timeout=60)
        assert response.status_code == expected, (path, response.status_code, response.text[:1200])
        return response.json()

    def start():
        nonlocal process, base
        ready_file.unlink(missing_ok=True)
        process = subprocess.Popen([str(exe), '--serve-only', '--home', str(home), '--ready-file', str(ready_file)],
                                   cwd=root, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            assert process.poll() is None, 'Frozen application exited during startup'
            if ready_file.is_file():
                ready = json.loads(ready_file.read_text(encoding='utf-8'))
                if ready['pid'] == process.pid:
                    base = ready['url']
                    try:
                        call('GET', '/api/health')
                        assert ready['frozen'] and ready['version'] == VERSION
                        assert Path(ready['home']) == home
                        return
                    except requests.ConnectionError:
                        pass
            time.sleep(.2)
        raise RuntimeError('Frozen application startup timed out')

    def stop():
        nonlocal process
        if process is not None:
            process.terminate()
            process.wait(timeout=15)
            process = None

    def mutate(state, action, **fields):
        prefix = '/api/portfolio/trainings/' + state['training_id']
        body = {'request_id': str(uuid4()), 'expected_version': state['version'], **fields}
        result = call('POST', prefix + '/' + action, body)
        assert call('POST', prefix + '/' + action, body) == result
        return result

    try:
        start()
        html = session.get(base + '/portfolio.html', timeout=10).text
        js = session.get(base + '/js/portfolio.js', timeout=10).text
        for marker in ('id="reset-training"', 'id="reset-dialog"', 'id="order-position"', 'id="pending-orders"'):
            assert marker in html, marker
        for marker in ('pending-order-details', 'data-edit-order', 'renderPendingOrders', 'group.quantity += order.quantity'):
            assert marker in js, marker
        call('POST', '/api/users', {'username': 'artificial_release_check'})
        call('POST', '/api/portfolio/session', {'user': 'artificial_release_check'})
        config = {'request_id': str(uuid4()), 'stocks': codes, 'start_date': '2024-01-06',
                  'end_date': '2024-03-31', 'initial_cash': '100000', 'data_source': 'offline'}
        state = call('POST', '/api/portfolio/trainings', config, 201)
        assert state['config']['practice'] and state['config']['stocks'] == codes
        assert state['current_date'] == '2024-01-08' and state['config']['end_date'] == '2024-03-29'
        prefix = '/api/portfolio/trainings/' + state['training_id']
        original_config = state['config']
        for quantity in (1000, 1000, 1000, 200):
            state = mutate(state, 'orders', stock=codes[0], side='buy', quantity=quantity)
        assert sum(order['quantity'] for order in state['orders'] if order['status'] == 'pending') == 3200
        state = mutate(state, 'cancel', order_id=state['orders'][-1]['order_id'])
        state = mutate(state, 'orders', stock=codes[0], side='buy', quantity=100)
        state = mutate(state, 'orders', stock=codes[1], side='buy', quantity=100)
        state = mutate(state, 'advance', expected_date=state['current_date'])
        assert len(state['fills']) == 5
        assert {row['stock']: row['quantity'] for row in state['positions']} == {codes[0]: 3100, codes[1]: 100, codes[2]: 0}
        spent = sum(Decimal(fill['gross']) + Decimal(fill['fees']['total']) for fill in state['fills'])
        assert Decimal(state['account']['cash']) == Decimal('100000') - spent
        state = mutate(state, 'orders', stock=codes[0], side='sell', quantity=100)
        stop()
        start()
        assert call('POST', prefix + '/restore', {}) == state
        state = mutate(state, 'advance', expected_date=state['current_date'])
        assert state['fills'][-1]['side'] == 'sell' and state['positions'][0]['quantity'] == 3000
        state = mutate(state, 'end')
        assert call('GET', prefix + '/report')['summary']['fill_count'] == 6
        reset_body = {'request_id': str(uuid4()), 'expected_version': state['version']}
        reset = call('POST', prefix + '/reset', reset_body)
        assert reset['status'] == 'active' and reset['ended_at'] is None
        assert reset['version'] == state['version'] + 1 and reset['config'] == original_config
        assert reset['current_date'] == '2024-01-08' and reset['account']['available_cash'] == '100000.00'
        assert reset['account']['frozen_cash'] == '0.00' and len(reset['daily_assets']) == 1
        assert reset['orders'] == reset['fills'] == reset['lots'] == []
        call('GET', prefix + '/report', expected=409)
        call('GET', prefix + '/history/' + codes[0] + '?through=2024-01-09', expected=400)
        fresh = mutate(reset, 'orders', stock=codes[2], side='buy', quantity=100)
        assert call('POST', prefix + '/reset', reset_body) == reset
        assert call('GET', prefix) == fresh
        stop()
        start()
        assert call('POST', prefix + '/restore', {}) == fresh
        random_params = {**config, 'request_id': str(uuid4()), 'stocks': [], 'mode': 'random',
                         'stock_count': 3, 'training_days': 10, 'sector': 'all'}
        random_state = call('POST', '/api/portfolio/trainings', random_params, 201)
        assert set(random_state['config']['stocks']) == set(codes)
        assert random_state['config']['mode'] == 'random'
        assert len(pd.bdate_range(random_state['config']['start_date'], random_state['config']['end_date'])) == 10
        assert call('POST', '/api/portfolio/trainings', random_params, 201) == random_state
        random_state = mutate(random_state, 'advance', expected_date=random_state['current_date'])
        random_reset = mutate(random_state, 'reset')
        assert random_reset['config'] == random_state['config']
        assert random_reset['current_date'] == random_state['config']['start_date']
        result = {'status': 'passed', 'version': VERSION, 'exe': str(exe),
                  'training_id': reset['training_id'], 'test_data': 'artificial offline prices and weekdays',
                  'checks': ['frozen version', 'bundled holdings/grouping/reset UI', 'three arbitrary stocks',
                             'weekend alignment', 'multiple independent orders', 'cancel and resubmit',
                             'shared cash and next-open fills', 'pending-order exact restart', 'partial sell',
                             'ended report', 'ended training reset', 'reset persistence', 'stale reset retry',
                             'future history boundary after reset', 'random portfolio', 'random reset keeps original draw']}
        (root / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        stop()


if __name__ == '__main__':
    main()
