"""Free stock/date selection, blind-box retry, and saved lifecycle regression tests."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from flask import Flask

from backend.data_manager import DataManager
from backend.portfolio_data_service import replay_factory
from backend.portfolio_models import serialize
from backend.portfolio_repository import PortfolioRepository
from backend.portfolio_routes import register_portfolio_routes
from backend.replay_models import Evidence, utc_now
from backend.replay_storage import write_cache
from backend.user_manager_enhanced import UserManagerEnhanced
from backend.tencent_source import TencentSourceUnavailable


class FreeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.manager = DataManager(str(self.data))
        self.dates = pd.bdate_range('2018-08-01', '2018-10-31')
        evidence = Evidence('2018-08-01', '2018-10-31', 'test-calendar', 'test-v1', 'disposable test observations', utc_now())
        write_cache(self.data / 'portfolio_replay' / 'calendar.json', {
            'provider': 'sina-public-calendar-v1', 'dates': [str(d.date()) for d in self.dates],
            'evidence': serialize(evidence)})
        self.codes = ['600519', '300750', '688001', '830001', '000001']
        self.manager.stock_names = dict(zip(self.codes, ['茅台', '宁德', '科创', '北交', '平安']))
        (self.data / 'stock_names.json').write_text(json.dumps(self.manager.stock_names), encoding='utf-8')
        for code in self.codes:
            pd.DataFrame([{'date': str(d.date()), 'open': '10.10', 'high': '10.80', 'low': '9.80',
                           'close': '10.30', 'volume': '1000.25'} for d in self.dates]).to_csv(
                Path(self.manager.offline_dir) / f'{code}.SZ.csv', index=False)
        self.users = UserManagerEnhanced(str(self.root / 'users'))
        self.users.create_user('tester')
        self.path = self.root / 'portfolio.sqlite3'
        self.repo = PortfolioRepository(self.path)
        self.addCleanup(lambda: self.repo.close())
        self.factory = replay_factory(self.data, data_manager=self.manager)
        self.client = self.client_for(self.repo)
        self.params = {'request_id': 'create', 'stocks': self.codes[:3], 'start_date': '2018-08-04',
                       'end_date': '2018-09-30', 'data_source': 'offline', 'initial_cash': '100000'}

    def client_for(self, repo):
        app = Flask(__name__)
        app.testing = True
        register_portfolio_routes(app, repo, self.users, self.factory)
        client = app.test_client()
        self.assertEqual(client.post('/api/portfolio/session', json={'user': 'tester'}).status_code, 200)
        return client

    def create(self, **params):
        response = self.client.post('/api/portfolio/trainings', json={**self.params, **params})
        self.assertEqual(response.status_code, 201, response.json)
        return response.json

    def mutate(self, state, action, **params):
        response = self.client.post(f"/api/portfolio/trainings/{state['training_id']}/{action}", json={
            'request_id': f'{action}-{state["version"]}', 'expected_version': state['version'], **params})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_arbitrary_stocks_old_dates_weekend_alignment_and_actual_fills(self):
        state = self.create()
        self.assertEqual(state['config']['stocks'], self.codes[:3])
        self.assertEqual(state['config']['start_date'], '2018-08-06')
        self.assertEqual(state['config']['end_date'], '2018-09-28')
        self.assertTrue(state['config']['practice'])
        self.assertFalse(state['synthetic'])
        self.assertEqual(state['quotes'][0]['bar']['volume'], '100025.00')
        for code in self.codes[:3]:
            state = self.mutate(state, 'orders', stock=code, side='buy', quantity=100)
        state = self.mutate(state, 'advance', expected_date=state['current_date'])
        self.assertEqual(len(state['fills']), 3)
        self.assertEqual({fill['stock'] for fill in state['fills']}, set(self.codes[:3]))
        self.assertEqual(state['config']['fee_periods'][0]['version'], 'practice-fixed-v1')

    def test_blind_box_chooses_unique_pool_common_random_dates_and_retries_identically(self):
        params = {**self.params, 'mode': 'random', 'stocks': [], 'stock_count': 3, 'training_days': 10, 'sector': 'all'}
        first = self.client.post('/api/portfolio/trainings', json=params)
        self.assertEqual(first.status_code, 201, first.json)
        state = first.json
        self.assertEqual(len(set(state['config']['stocks'])), 3)
        self.assertEqual(state['config']['mode'], 'random')
        self.assertTrue(self.params['start_date'] <= state['config']['start_date'] < state['config']['end_date'] <= self.params['end_date'])
        self.assertEqual(len(pd.bdate_range(state['config']['start_date'], state['config']['end_date'])), 10)
        with patch.object(self.manager, 'get_stock_data', side_effect=AssertionError('retry must not redraw')):
            self.assertEqual(self.client.post('/api/portfolio/trainings', json=params).json, state)

    def test_blind_box_sector_and_invalid_sizes(self):
        state = self.create(mode='random', stock_count=1, training_days=5, sector='gem')
        self.assertEqual(state['config']['stocks'], ['300750'])
        for override in [{'stock_count': 0}, {'stock_count': 21}, {'training_days': 1}, {'training_days': True}, {'sector': 'bad'}]:
            result = self.client.post('/api/portfolio/trainings', json={**self.params, 'request_id': 'invalid', 'mode': 'random', **override})
            self.assertEqual(result.status_code, 400)

    def test_source_outage_stops_blind_box_after_first_stock(self):
        with patch.object(self.manager, "get_stock_data_range", side_effect=TencentSourceUnavailable("501 WAF")) as fetch:
            response = self.client.post('/api/portfolio/trainings', json={**self.params,
                'data_source': 'tencent', 'mode': 'random', 'stock_count': 3, 'training_days': 10})
        self.assertEqual(response.status_code, 400, response.json)
        self.assertIn('已停止', response.json['error'])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[1:], ('2018-08-06', '2018-09-28'))
        self.assertEqual(self.client.get('/api/portfolio/trainings').json['trainings'], [])

    def test_online_portfolio_uses_snapped_range_without_legacy_download(self):
        frame = self.manager.get_stock_data('600519', source='offline')
        with patch.object(self.manager, 'get_stock_data_range', return_value=frame) as fetch, \
             patch.object(self.manager, 'get_stock_data', side_effect=AssertionError('must not download full history')):
            state = self.create(data_source='tencent')
        self.assertEqual(state['config']['stocks'], self.codes[:3])
        self.assertEqual(fetch.call_count, 3)
        for call in fetch.call_args_list:
            self.assertEqual(call.args[1:], ('2018-08-06', '2018-09-28'))
            self.assertEqual(call.kwargs, {'source': 'tencent'})

    def test_missing_bar_cannot_fill_but_does_not_block_clock_or_restart(self):
        file = Path(self.manager.offline_dir) / '600519.SZ.csv'
        frame = pd.read_csv(file)
        frame[frame.date != '2018-08-07'].to_csv(file, index=False)
        state = self.create(stocks=['600519'])
        state = self.mutate(state, 'orders', stock='600519', side='buy', quantity=100)
        state = self.mutate(state, 'advance', expected_date=state['current_date'])
        self.assertEqual(state['quotes'][0]['status'], 'unknown')
        self.assertEqual(state['fills'], [])
        self.assertEqual(state['orders'][0]['status'], 'rejected')
        self.repo.close()
        self.repo = PortfolioRepository(self.path)
        self.client = self.client_for(self.repo)
        restored = self.client.post(f"/api/portfolio/trainings/{state['training_id']}/restore", json={})
        self.assertEqual(restored.json, state)
        state = self.mutate(state, 'advance', expected_date=state['current_date'])
        self.assertEqual(state['current_date'], '2018-08-08')

    def test_return_resume_and_end_release_orders_and_produce_report(self):
        state = self.create()
        state = self.mutate(state, 'orders', stock='600519', side='buy', quantity=100)
        prefix = f"/api/portfolio/trainings/{state['training_id']}"
        self.assertEqual(self.client.get('/api/portfolio/trainings').json['trainings'][0]['training_id'], state['training_id'])
        self.assertEqual(self.client.post(prefix + '/restore', json={}).json, state)
        self.assertEqual(self.client.get(prefix + '/history/600519?through=2018-09-28').status_code, 400)
        state = self.mutate(state, 'end')
        self.assertEqual(state['status'], 'ended')
        self.assertEqual(state['orders'][0]['status'], 'cancelled')
        self.assertEqual(float(state['account']['frozen_cash']), 0)
        self.assertEqual(self.client.get(prefix + '/report').status_code, 200)
        self.assertEqual(self.client.post(prefix + '/advance', json={'request_id': 'late', 'expected_version': state['version'], 'expected_date': state['current_date']}).status_code, 409)

    def test_catalog_search_allows_other_stocks_without_reviewed_history(self):
        result = self.client.get('/api/portfolio/options?q=宁德&data_source=offline').json
        self.assertEqual(result['stocks'][0]['stock'], '300750')
        self.assertNotIn('核验', result['stocks'][0]['reason'])
        self.assertIsNone(result['stocks'][0]['coverage'])
        self.assertTrue({'tencent', 'akshare', 'offline'} <= {source['id'] for source in result['sources']})

    def test_no_quotes_and_too_short_blind_box_do_not_create_training(self):
        for extra in [{'stocks': ['600111']}, {'mode': 'random', 'stock_count': 3, 'training_days': 100}]:
            response = self.client.post('/api/portfolio/trainings', json={**self.params, **extra})
            self.assertEqual(response.status_code, 400, response.json)
        self.assertEqual(self.client.get('/api/portfolio/trainings').json['trainings'], [])


if __name__ == '__main__':
    unittest.main()
