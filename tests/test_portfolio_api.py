from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from flask import Flask

from backend.portfolio_data_service import replay_factory
from backend.portfolio_engine import PortfolioEngine
from backend.portfolio_ledger_store import PortfolioLedgerStore
from backend.portfolio_models import serialize
from backend.portfolio_repository import PortfolioAPIError, PortfolioRepository
from backend.portfolio_replay import PortfolioReplay
from backend.portfolio_routes import register_portfolio_routes
from backend.replay_models import CorporateAction, ReplayDataError
from backend.user_manager_enhanced import UserManagerEnhanced
from tests.replay_fixture import fixture


class PortfolioAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="portfolio-step4-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.users = UserManagerEnhanced(str(self.root / "users"))
        for user in ("alice", "bob"):
            self.assertTrue(self.users.create_user(user))
        self.path = self.root / "users" / "portfolio.sqlite3"
        self.repo = PortfolioRepository(self.path)
        self.addCleanup(lambda: self.repo.close())
        self.app = self.make_app(self.repo)
        self.client = self.app.test_client()
        self.select(self.client, "alice")
        self.create_body = {"request_id": "create-1", "stocks": ["600000", "000001", "600036"],
                            "start_date": "2024-01-02", "end_date": "2024-01-10",
                            "data_source": "synthetic-demo", "initial_cash": "100000"}

    def make_app(self, repository, factory=None):
        app = Flask(__name__)
        app.testing = True
        register_portfolio_routes(app, repository, self.users,
            factory or replay_factory(self.root / "data", allow_demo=True))
        return app

    def select(self, client, user):
        response = client.post("/api/portfolio/session", json={"user": user})
        self.assertEqual(response.status_code, 200, response.json)
        return response

    def create(self, **overrides):
        response = self.client.post("/api/portfolio/trainings", json={**self.create_body, **overrides})
        self.assertEqual(response.status_code, 201, response.json)
        result = response.json
        self.tid = result["training_id"]
        self.prefix = "/api/portfolio/trainings/" + self.tid
        return result

    def get(self, client=None):
        response = (client or self.client).get(self.prefix)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def mutate(self, action, state, key, expected=200, client=None, **kwargs):
        response = (client or self.client).post(self.prefix + "/" + action, json={
            "request_id": key, "expected_version": state["version"], **kwargs})
        self.assertEqual(response.status_code, expected, response.json)
        return response.json

    def buy(self, state, key="buy-1", **kwargs):
        return self.mutate("orders", state, key, stock="600000", side="buy", quantity=200, **kwargs)

    def advance(self, state, key="next-1", **kwargs):
        return self.mutate("advance", state, key, expected_date=state["current_date"], **kwargs)

    def test_create_uuid_configuration_and_shared_account(self):
        state = self.create()
        self.assertEqual(str(UUID(self.tid)), self.tid)
        self.assertEqual(state["account"]["cash"], "100000.00")
        self.assertEqual(state["config"]["market_rules"]["version"], "cn-mainboard-daily-v1")
        self.assertTrue(state["config"]["fee_periods"])
        bought = self.buy(state)
        self.assertEqual(bought["account"]["available_cash"], "97750.98")
        self.assertEqual(bought["account"]["frozen_cash"], "2249.02")
        self.assertEqual(bought["fills"], [])
        self.assertEqual(len(self.client.get("/api/portfolio/trainings").json["trainings"]), 1)

    def test_ui_catalog_search_coverage_and_explicit_demo_gate(self):
        response = self.client.get('/api/portfolio/options?data_source=synthetic-demo&q=合成甲')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r['stock'] for r in response.json['stocks']], ['600000'])
        self.assertEqual(response.json['stocks'][0]['coverage'], {'start':'2024-01-02', 'end':'2024-01-10'})
        self.assertNotIn('bars', json.dumps(response.json))
        other = self.make_app(self.repo, replay_factory(self.root / 'data')).test_client()
        self.select(other, 'alice')
        self.assertEqual(other.get('/api/portfolio/options?data_source=synthetic-demo').status_code, 400)
        self.assertEqual(self.app.test_client().get('/api/portfolio/options').status_code, 401)

    def test_real_data_update_requires_profile_and_accepts_no_evidence(self):
        factory=replay_factory(self.root/'data')
        app=self.make_app(self.repo,factory)
        anonymous=app.test_client()
        with patch.object(factory,'prepare_real_data',return_value={'source':'sina','start':'2024-01-02','end':'2024-06-12'}) as prepare:
            self.assertEqual(anonymous.post('/api/portfolio/prepare-data',json={}).status_code,401)
            self.select(anonymous,'alice')
            self.assertEqual(anonymous.post('/api/portfolio/prepare-data',json={'evidence':{'scope':'reviewed'}}).status_code,400)
            prepare.assert_not_called()
            result=anonymous.post('/api/portfolio/prepare-data',json={})
            self.assertEqual(result.status_code,200)
            self.assertNotIn('bars',result.json)
            prepare.assert_called_once_with()

    def test_real_catalog_has_real_names_without_opening_unreviewed_range(self):
        result=self.client.get('/api/portfolio/options?data_source=sina').json
        self.assertEqual({row['name'] for row in result['stocks']},{'平安银行','浦发银行','招商银行'})
        self.assertTrue(all(row['coverage'] is None for row in result['stocks']))
        self.assertIsNone(result['suggested_range'])
        result=self.client.post('/api/portfolio/preview',json={key:value for key,value in {**self.create_body,'data_source':'sina'}.items() if key!='request_id'})
        self.assertEqual(result.status_code,200)
        self.assertFalse(result.json['supported'])

    def test_multi_month_practice_is_explicit_synthetic_and_date_bounded(self):
        state = self.create(data_source='synthetic-practice', end_date='2024-02-09')
        self.assertTrue(state['synthetic'])
        self.assertEqual(len(self.client.get(self.prefix + '/history/600000').json['bars']), 1)
        state = self.buy(state)
        state = self.advance(state)
        self.assertEqual(state['fills'][0]['quantity'], 200)
        self.assertEqual(state['fills'][0]['price'], '12.00')
        for i in range(19):
            state = self.advance(state, key=f'practice-day-{i}')
        bars = self.client.get(self.prefix + '/history/600000').json['bars']
        self.assertEqual(len(bars), 21)
        self.assertEqual(bars[-1]['date'], state['current_date'])
        self.assertGreaterEqual(float(state['account']['cash']), 0)
        self.assertEqual(self.client.get(self.prefix + '/history/600000?through=2024-06-28').status_code, 400)

    def test_multi_month_practice_cannot_be_enabled_by_browser(self):
        client = self.make_app(self.repo, replay_factory(self.root / 'data')).test_client()
        self.select(client, 'alice')
        self.assertEqual(client.get('/api/portfolio/options?data_source=synthetic-practice').status_code, 400)
        params = {**self.create_body, 'data_source':'synthetic-practice','end_date':'2024-06-28'}
        self.assertEqual(client.post('/api/portfolio/trainings',json=params).status_code,400)

    def test_ui_preview_has_no_future_prices_or_persistent_training(self):
        params = {k:v for k,v in self.create_body.items() if k != 'request_id'}
        result = self.client.post('/api/portfolio/preview', json=params)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertTrue(result.json['supported'])
        self.assertEqual(len(result.json['stocks']), 3)
        self.assertNotIn('"9999.00"', json.dumps(result.json))
        self.assertNotIn('bars', json.dumps(result.json))
        self.assertEqual(self.client.get('/api/portfolio/trainings').json['trainings'], [])
        self.assertEqual(self.repo._connection.execute('SELECT count(*) FROM engine_state').fetchone()[0], 0)

    def test_ui_preview_collects_reasons_for_unavailable_stocks_and_dates(self):
        params = {k:v for k,v in self.create_body.items() if k != 'request_id'}
        params['stocks'] = ['600000', '600001']
        response = self.client.post('/api/portfolio/preview', json=params)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['supported'])
        self.assertTrue(response.json['stocks'][0]['supported'])
        self.assertFalse(response.json['stocks'][1]['supported'])
        self.assertTrue(response.json['stocks'][1]['reason'])
        params['start_date'] = '2024-01-01'
        response = self.client.post('/api/portfolio/preview', json=params)
        self.assertFalse(response.json['supported'])
        params['data_source'] = 'tencent'
        with patch('backend.portfolio_practice.load_rows', side_effect=ReplayDataError('该数据源暂无行情')):
            response = self.client.post('/api/portfolio/preview', json=params)
        self.assertFalse(response.json['supported'])
        self.assertIn('暂无行情', response.json['stocks'][0]['reason'])

    def test_ui_preview_cannot_upload_evidence_or_use_invalid_cash(self):
        params = {k:v for k,v in self.create_body.items() if k != 'request_id'}
        for extra in ({'synthetic':True}, {'initial_cash':'0'}, {'stocks':[]}, {'end_date':'2023-01-01'}):
            self.assertEqual(self.client.post('/api/portfolio/preview', json={**params, **extra}).status_code, 400)

    def test_ui_preview_rejects_missing_data_later_in_requested_range(self):
        def incomplete(cache):
            result = fixture(cache)
            result[4].bars['600000'] = [b for b in result[4].bars['600000'] if b['date'] != '2024-01-08']
            return result
        params = {k:v for k,v in self.create_body.items() if k != 'request_id'}
        params['stocks'] = ['600000']
        with patch('tests.replay_fixture.fixture', side_effect=incomplete):
            response = self.client.post('/api/portfolio/preview', json=params)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['supported'])
        self.assertTrue(response.json['stocks'][0]['reason'])
        self.assertNotIn('bars', json.dumps(response.json))

    def test_ui_estimate_rejects_malformed_order_fields(self):
        before = self.create()
        params = {'stock':'600000','side':'buy','quantity':100}
        for extra in ({'side':[]}, {'quantity':True}, {'quantity':1.5}, {'stock':'300001'}, {'budget':'100'}):
            self.assertEqual(self.client.post(self.prefix + '/estimate', json={**params, **extra}).status_code, 400)
        self.assertEqual(self.get(), before)

    def test_ui_estimate_exact_reservation_without_mutation(self):
        before = self.create()
        result = self.client.post(self.prefix + '/estimate', json={'stock':'600000','side':'buy','quantity':200})
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json['version'], before['version'])
        self.assertEqual(result.json['order']['frozen_cash'], '2249.02')
        self.assertEqual(result.json['order']['estimated_fees']['total'], '5.02')
        self.assertEqual(result.json['order']['execute_on'], '2024-01-03')
        self.assertEqual(self.get(), before)
        actual = self.buy(before)['orders'][-1]
        for key in ('estimated_price','estimated_fees','frozen_cash','execute_on','status'):
            self.assertEqual(result.json['order'][key], actual[key])

    def test_ui_estimate_respects_frozen_cash_t1_and_ownership(self):
        state = self.create()
        state = self.buy(state)
        response = self.client.post(self.prefix + '/estimate', json={'stock':'000001','side':'buy','quantity':100000})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['order']['status'], 'rejected')
        self.assertIn('资金不足', response.json['order']['reason'])
        response = self.client.post(self.prefix + '/estimate', json={'stock':'600000','side':'sell','quantity':100})
        self.assertIn('T+1', response.json['order']['reason'])
        self.assertEqual(self.get(), state)
        bob = self.app.test_client()
        self.select(bob, 'bob')
        self.assertEqual(bob.post(self.prefix + '/estimate', json={'stock':'600000','side':'buy','quantity':100}).status_code, 404)

    def test_ui_estimate_handles_suspension_and_terminal_session(self):
        state = self.advance(self.buy(self.create()))
        response = self.client.post(self.prefix + '/estimate', json={'stock':'000001','side':'buy','quantity':100})
        self.assertEqual(response.json['order']['status'], 'rejected')
        self.assertIn('停牌', response.json['order']['reason'])
        response = self.client.post(self.prefix + '/estimate', json={'stock':'600000','side':'sell','quantity':100})
        self.assertEqual(response.json['order']['status'], 'pending')
        position = state['positions'][0]
        self.assertEqual(position['sellable_now'], 0)
        self.assertEqual(position['orderable_next_open'], 200)
        self.assertEqual(self.get(), state)
        self.mutate('end', state, 'end-ui')
        self.assertEqual(self.client.post(self.prefix + '/estimate', json={'stock':'600000','side':'buy','quantity':100}).status_code, 400)

    def test_create_retry_persists_and_conflicting_id_rejected(self):
        first = self.create()
        self.assertEqual(self.create(), first)
        response = self.client.post("/api/portfolio/trainings", json={**self.create_body, "initial_cash": "90000"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.repo.list_trainings(self.repo.session_owner(self.client.get_cookie("kline_portfolio_profile", path="/api/portfolio").value))), 1)

    def test_cookie_required_and_unknown_profiles_rejected(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get("/api/portfolio/trainings").status_code, 401)
        for user in ("missing", "../alice", ["alice"], ".."):
            self.assertEqual(anon.post("/api/portfolio/session", json={"user": user}).status_code, 401)

    def test_all_endpoints_check_owner_before_read_or_retry(self):
        state = self.create()
        state = self.buy(state)
        bob = self.app.test_client()
        self.select(bob, "bob")
        self.assertEqual(bob.get("/api/portfolio/trainings").json, {"trainings": []})
        for path in ("", "/history/600000", "/report"):
            self.assertEqual(bob.get(self.prefix + path).status_code, 404)
        for path, body in (("restore", {}), ("advance", {"request_id": "buy-1", "expected_version": 0, "expected_date": state["current_date"]}),
                           ("orders", {"request_id": "buy-1", "expected_version": 0, "stock": "600000", "side": "buy", "quantity": 200}),
                           ("cancel", {"request_id": "cancel", "expected_version": 1, "order_id": state["orders"][0]["order_id"]}),
                           ("end", {"request_id": "end", "expected_version": 1})):
            self.assertEqual(bob.post(self.prefix + "/" + path, json=body).status_code, 404)
        self.assertEqual(self.get(), state)

    def test_profile_rotation_and_recreated_name_cannot_inherit(self):
        self.create()
        old_cookie = self.client.get_cookie("kline_portfolio_profile", path="/api/portfolio").value
        self.select(self.client, "bob")
        with self.assertRaises(PortfolioAPIError):
            self.repo.session_owner(old_cookie)
        self.select(self.client, "alice")
        config = self.users.get_user_config("alice")
        config["created_at"] = "2099-01-01T00:00:00"
        self.users.update_user_config("alice", config)
        self.assertEqual(self.client.get(self.prefix).status_code, 401)
        self.select(self.client, "alice")
        self.assertEqual(self.client.get(self.prefix).status_code, 404)

    def test_cross_origin_host_and_cookie_flags(self):
        response = self.select(self.client, "alice")
        cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(self.client.get("/api/portfolio/trainings", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/portfolio/session", json={"user": "alice"}, headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/portfolio/trainings", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)

    def test_request_validation_and_no_client_evidence(self):
        for override in ({"stocks": []}, {"stocks": ["600000", "600000"]}, {"stocks": ["300001"]},
                         {"initial_cash": "NaN"}, {"initial_cash": "1.001"}, {"start_date": "bad"},
                         {"synthetic": True}, {"evidence": {"scope": "reviewed"}}, {"initial_cash": "1e99"}):
            response = self.client.post("/api/portfolio/trainings", json={**self.create_body, **override})
            self.assertEqual(response.status_code, 400, (override, response.json))
        self.assertEqual(self.client.post("/api/portfolio/trainings", data="{", content_type="application/json").status_code, 400)

    def test_missing_quotes_and_demo_disabled_fail_closed(self):
        with patch('backend.portfolio_practice.load_rows', side_effect=ReplayDataError('该数据源暂无行情')):
            response = self.client.post("/api/portfolio/trainings", json={**self.create_body, "data_source": "tencent"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("暂无行情", response.json["error"])
        client = self.make_app(self.repo, replay_factory(self.root / "data")).test_client()
        self.select(client, "alice")
        self.assertEqual(client.post("/api/portfolio/trainings", json=self.create_body).status_code, 400)

    def test_future_data_isolation_and_switch_by_history_keeps_date(self):
        state = self.create()
        for code in self.create_body["stocks"]:
            response = self.client.get(self.prefix + "/history/" + code)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(all(row["date"] <= state["current_date"] for row in response.json["bars"]))
        self.assertEqual(self.get(), state)
        for suffix in ("?through=2024-01-10", "?full=true", "?adjustment=forward"):
            self.assertEqual(self.client.get(self.prefix + "/history/600000" + suffix).status_code, 400)
        self.assertEqual(self.client.get(self.prefix + "/report").status_code, 409)
        self.assertNotIn('"9999.00"', json.dumps(state))

    def test_submission_retry_cancel_retry_and_stale_version(self):
        original = self.create()
        state = self.buy(original)
        self.assertEqual(self.buy(original), state)
        self.buy(original, key="other", expected=409)
        self.mutate("orders", original, "buy-1", expected=409, stock="600000", side="buy", quantity=100)
        order = state["orders"][0]
        cancelled = self.mutate("cancel", state, "cancel", order_id=order["order_id"])
        self.assertEqual(cancelled["account"]["available_cash"], "100000.00")
        self.assertEqual(self.mutate("cancel", state, "cancel", order_id=order["order_id"]), cancelled)

    def test_advance_retry_stale_date_and_different_click(self):
        state = self.buy(self.create())
        advanced = self.advance(state)
        self.assertEqual(advanced["fills"][0]["price"], "10.30")
        self.assertEqual(advanced["account"]["cash"], "97934.98")
        self.assertEqual(self.advance(state), advanced)
        self.advance(state, key="different", expected=409)
        self.mutate("advance", advanced, "stale-date", expected=409, expected_date=state["current_date"])
        self.assertEqual(len(self.get()["fills"]), 1)

    def test_restore_after_close_is_exact_and_no_quote_download(self):
        state = self.advance(self.buy(self.create()))
        state = self.mutate("orders", state, "second-stock", stock="000001", side="buy", quantity=100)
        cookie = self.client.get_cookie("kline_portfolio_profile", path="/api/portfolio").value
        self.repo.close()
        self.repo = PortfolioRepository(self.path)
        client = self.make_app(self.repo, lambda _: self.fail("must not fetch on restore")).test_client()
        client.set_cookie("kline_portfolio_profile", cookie, path="/api/portfolio")
        restored = client.post(self.prefix + "/restore", json={})
        self.assertEqual(restored.status_code, 200, restored.json)
        self.assertEqual(restored.json, state)
        advanced = self.advance(state, key="next-restored", client=client)
        self.assertEqual(advanced["current_date"], "2024-01-04")
        self.assertEqual(len(advanced["fills"]), 1)
        self.assertEqual(advanced["orders"][-1]["status"], "rejected")
        self.assertIn("停牌", advanced["orders"][-1]["reason"])

    def test_sqlite_write_failure_rolls_back_state_and_retry_record(self):
        state = self.buy(self.create())
        with patch.object(self.repo, "_before_transaction_commit", side_effect=sqlite3.OperationalError("disk full")):
            self.advance(state, expected=503)
        self.assertEqual(self.get(), state)
        self.assertEqual(self.repo._connection.execute("SELECT COUNT(*) FROM portfolio_request WHERE request_id='next-1'").fetchone()[0], 0)
        advanced = self.advance(state)
        self.assertEqual(len(advanced["fills"]), 1)

    def test_creation_cancel_and_end_failures_roll_back_all_writes(self):
        with patch.object(self.repo, "_before_transaction_commit", side_effect=sqlite3.OperationalError("full")):
            response = self.client.post("/api/portfolio/trainings", json=self.create_body)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.repo._connection.execute("SELECT COUNT(*) FROM engine_state").fetchone()[0], 0)
        self.assertEqual(self.repo._connection.execute("SELECT COUNT(*) FROM portfolio_training").fetchone()[0], 0)
        state = self.buy(self.create())
        for action, key, params in (("cancel", "cancel-failed", {"order_id": state["orders"][0]["order_id"]}),
                                    ("end", "end-failed", {})):
            with patch.object(self.repo, "_before_transaction_commit", side_effect=sqlite3.OperationalError("full")):
                self.mutate(action, state, key, expected=503, **params)
            self.assertEqual(self.get(), state)
        cancelled = self.mutate("cancel", state, "cancel-failed", order_id=state["orders"][0]["order_id"])
        self.assertEqual(cancelled["account"]["frozen_cash"], "0.00")

    def test_invalid_or_huge_budget_does_not_change_account(self):
        state = self.create()
        for budget in ("1e99999", "NaN", "0", "10.001"):
            self.buy(state, key="invalid-budget", budget=budget, expected=400)
            self.assertEqual(self.get(), state)

    def test_v1_metadata_migration_preserves_training_and_adds_sessions(self):
        state = self.buy(self.create())
        self.repo._connection.execute("DROP TABLE portfolio_session")
        self.repo._connection.execute("PRAGMA user_version=1")
        self.repo.close()
        self.repo = PortfolioRepository(self.path)
        self.app = self.make_app(self.repo)
        self.client = self.app.test_client()
        self.select(self.client, "alice")
        self.assertEqual(self.get(), state)
        self.assertEqual(self.repo._connection.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_corrupt_ledger_or_payload_version_refuses_restore(self):
        self.create()
        raw = self.repo.read(self.tid)
        raw["account"]["cash"] = "1.00"
        self.repo._connection.execute("UPDATE engine_state SET payload=?", (json.dumps(raw),))
        self.assertEqual(self.client.post(self.prefix + "/restore", json={}).status_code, 503)

    def test_end_at_last_date_and_restore_does_not_advance_again(self):
        state = self.create()
        index = 0
        while state["current_date"] != self.create_body["end_date"]:
            index += 1
            state = self.advance(state, key=f"day-{index}")
        self.advance(state, key="over-end", expected=400)
        self.buy(state, key="no-next-open", expected=400)
        self.assertEqual(self.get(), state)
        ended = self.mutate("end", state, "end-last")
        self.assertEqual(ended["report"]["summary"]["total_pnl"], "0.00")
        self.assertEqual(ended["report"]["summary"]["final_assets"], "100000.00")

    def test_end_releases_reservations_report_reconciles_and_unlocks_full_history(self):
        state = self.advance(self.buy(self.create()))
        state = self.mutate("orders", state, "sell", stock="600000", side="sell", quantity=100)
        state = self.advance(state, key="next-sell")
        self.assertEqual(state["daily_assets"][-1]["total_assets"], "100039.44")
        pending = self.mutate("orders", state, "pending", stock="000001", side="buy", quantity=100)
        ended = self.mutate("end", pending, "end")
        self.assertEqual(ended["account"]["frozen_cash"], "0.00")
        self.assertEqual(ended["orders"][-1]["status"], "cancelled")
        report = ended["report"]
        self.assertEqual(report["summary"]["final_assets"], "100039.44")
        self.assertEqual(report["summary"]["total_pnl"], "39.44")
        self.assertEqual(report["summary"]["fill_count"], 2)
        self.assertIn('"9999.00"', json.dumps(report["history"]))
        self.assertEqual(self.client.get(self.prefix + "/report").json, report)
        self.assertEqual(self.mutate("end", pending, "end"), ended)
        self.buy(ended, key="after-end", expected=409)
        self.advance(ended, key="after-end-next", expected=409)
        # Normal history retains the boundary; only the ended report unlocks it.
        self.assertEqual(self.client.get(self.prefix + "/history/600000?through=2024-01-10").status_code, 400)

    def test_concurrent_repository_connections_only_one_advance(self):
        state = self.buy(self.create())
        owner = self.repo._connection.execute("SELECT owner FROM portfolio_training").fetchone()[0]
        second = PortfolioRepository(self.path)
        self.addCleanup(second.close)

        def attempt(pair):
            repository, key = pair
            try:
                return repository.mutate(owner, self.tid, "advance", {"request_id": key,
                    "expected_version": state["version"], "expected_date": state["current_date"]})
            except PortfolioAPIError as exc:
                return exc.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [(self.repo, "a"), (second, "b")]))
        self.assertEqual(sum(isinstance(item, dict) for item in results), 1)
        self.assertIn(409, results)
        self.assertEqual(len(self.get()["fills"]), 1)

    def test_concurrent_same_request_returns_identical_result(self):
        state = self.buy(self.create())
        owner = self.repo._connection.execute("SELECT owner FROM portfolio_training").fetchone()[0]
        second = PortfolioRepository(self.path)
        self.addCleanup(second.close)
        params = {"request_id": "same", "expected_version": state["version"], "expected_date": state["current_date"]}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda repo: repo.mutate(owner, self.tid, "advance", params), (self.repo, second)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.get()["fills"]), 1)

    def test_snapshot_tampering_and_future_schema_refused(self):
        self.create()
        self.repo._connection.execute("UPDATE portfolio_training SET replay_hash='bad'")
        self.assertEqual(self.client.get(self.prefix).status_code, 503)
        self.repo._connection.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(PortfolioAPIError, "版本较新"):
            PortfolioRepository(self.path)
        self.assertEqual(self.repo._connection.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_migration_from_stage3_database_preserves_ledger_and_legacy_files(self):
        legacy = self.root / "users" / "alice" / "training_history.db"
        before = legacy.read_bytes()
        path = self.root / "stage3.sqlite3"
        store = PortfolioLedgerStore(path)
        data, calendar, securities, actions, _, quotes = fixture(self.root / "old-data")
        replay = PortfolioReplay(list(securities), data["start"], data["end"], calendar, quotes,
                                 securities, actions, synthetic=True, accounting=True)
        engine = PortfolioEngine(replay, store=store)
        engine.submit("600000", "buy", 100, request_id="old-buy")
        saved = store.read(engine.training_id)
        # Actual stage 3 payload predates status/ended_at.
        old = {key: value for key, value in saved.items() if key not in {"status", "ended_at"}}
        store._connection.execute("UPDATE engine_state SET payload=?", (json.dumps(old),))
        store.close()
        migrated = PortfolioRepository(path)
        try:
            restored = PortfolioEngine.restore(replay, migrated, engine.training_id)
            self.assertEqual(restored.snapshot()["orders"][0]["request_id"], "old-buy")
            self.assertEqual(restored.snapshot()["status"], "active")
            self.assertEqual(migrated._connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(migrated.list_trainings("alice"), [])  # no invented ownership
        finally:
            migrated.close()
        self.assertEqual(legacy.read_bytes(), before)

    def test_pinned_corporate_action_survives_restore_without_reapplying(self):
        data, calendar, securities, actions, _, quotes = fixture(self.root / "action-data")
        actions["600000"] = replace(actions["600000"], events=(CorporateAction("div", "cash_dividend",
            "2024-01-05", "SYNTHETIC NET dividend", record_on="2024-01-03", cash_per_share="0.20", reference_price="10.40"),))
        replay = PortfolioReplay(list(securities), data["start"], data["end"], calendar, quotes,
                                 securities, actions, synthetic=True, accounting=True)
        client = self.make_app(self.repo, lambda _: replay).test_client()
        self.select(client, "alice")
        self.client = client
        state = self.advance(self.buy(self.create()))
        state = self.advance(state, key="day3")
        state = self.advance(state, key="div-day")
        self.assertEqual(sum(row["kind"] == "cash_dividend" for row in state["ledger"]), 1)
        self.assertEqual(state, self.client.post(self.prefix + "/restore", json={}).json)
        next_state = self.advance(state, key="after-div")
        self.assertEqual(sum(row["kind"] == "cash_dividend" for row in next_state["ledger"]), 1)


if __name__ == "__main__":
    unittest.main()
