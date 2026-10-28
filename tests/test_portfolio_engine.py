from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.portfolio_engine import PortfolioEngine
from backend.portfolio_ledger_store import PortfolioLedgerStore
from backend.portfolio_replay import PortfolioReplay
from backend.portfolio_rules import FeePeriod, FeeSchedule, MarketRules, money
from backend.replay_models import CorporateAction, ReplayDataError, day
from tests.replay_fixture import fixture


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="portfolio-step3-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.data, self.calendar, self.securities, self.actions,
         self.source, self.quotes) = fixture(self.root)

    def engine(self, initial="100000", *, store=None, rules=None, fees=None, **kwargs):
        args = dict(stocks=list(self.securities), start=self.data["start"], end=self.data["end"],
                    calendar=self.calendar, quotes=self.quotes, securities=self.securities,
                    actions=self.actions, synthetic=True, accounting=True)
        args.update(kwargs)
        replay = PortfolioReplay(**args)
        result = PortfolioEngine(replay, initial, store=store, rules=rules, fees=fees)
        self.addCleanup(result.close)
        return result

    def buy(self, engine, qty=200, code="600000", key="buy", **kwargs):
        return engine.submit(code, "buy", qty, request_id=key, **kwargs)

    def advance(self, engine, key=None):
        return engine.advance(operation_id=key or f"advance-{engine.current_date}",
                              expected_date=engine.current_date)

    def position(self, engine, code="600000"):
        return next(row for row in engine.snapshot()["positions"] if row["stock"] == code)

    def action(self, kind="cash_dividend", **kwargs):
        fields = dict(event_id="synthetic-action", kind=kind, effective_on="2024-01-05",
                      reference="synthetic reviewed NET/credit-date example", record_on="2024-01-03",
                      reference_price="10.40")
        fields.update(cash_per_share="0.20" if kind == "cash_dividend" else "0",
                      share_ratio="0.25" if kind == "bonus_shares" else "0")
        if kind == "bonus_shares":
            fields["available_on"] = "2024-01-08"
        fields.update(kwargs)
        event = CorporateAction(**fields)
        self.actions["600000"] = replace(self.actions["600000"], events=(event,))
        return event

    def test_shared_cash_freezes_and_cancel_releases(self):
        engine = self.engine("1500")
        first = self.buy(engine, 100)
        self.assertEqual(first["frozen_cash"], "1127.01")
        self.assertEqual(engine.snapshot()["account"]["available_cash"], "372.99")
        second = self.buy(engine, 100, "000001", "second")
        self.assertEqual(second["status"], "rejected")
        self.assertIn("资金不足", second["reason"])
        engine.cancel(first["order_id"])
        self.assertEqual(engine.snapshot()["account"]["available_cash"], "1500.00")
        self.assertEqual(self.buy(engine, 100, "000001", "third")["status"], "pending")

    def test_next_open_fee_and_exact_partial_sale(self):
        engine = self.engine()
        self.buy(engine)
        self.assertEqual(engine.snapshot()["fills"], [])
        first = self.advance(engine)
        self.assertEqual(first["account"]["cash"], "97934.98")
        self.assertEqual(first["fills"][0]["price"], "10.30")
        self.assertEqual(first["fills"][0]["fees"]["total"], "5.02")
        self.assertEqual(first["daily_assets"][-1]["total_assets"], "100014.98")
        engine.submit("600000", "sell", 100, request_id="sell")
        sold = self.advance(engine)
        self.assertEqual(sold["account"]["cash"], "98979.44")
        self.assertEqual(sold["account"]["realized_pnl"], "11.95")
        self.assertEqual(sold["fills"][-1]["cost_released"], "1032.51")
        self.assertEqual(sold["fills"][-1]["fees"]["total"], "5.54")
        self.assertEqual(self.position(engine)["cost"], "1032.51")
        self.assertEqual(sold["daily_assets"][-1]["unrealized_pnl"], "27.49")
        self.assertEqual(sold["daily_assets"][-1]["total_assets"], "100039.44")
        self.assertEqual(engine.reconcile()["cash"], "98979.44")

    def test_jump_cannot_expand_order_budget_even_with_extra_account_cash(self):
        engine = self.engine()
        order = self.buy(engine, 100, budget="1030")
        self.assertEqual(order["status"], "pending")
        result = self.advance(engine)
        self.assertEqual(result["orders"][0]["status"], "rejected")
        self.assertIn("冻结预算", result["orders"][0]["reason"])
        self.assertEqual(result["account"]["cash"], "100000.00")
        self.assertEqual(result["account"]["frozen_cash"], "0.00")
        self.assertEqual(result["fills"], [])

    def test_duplicate_submit_and_conflicting_id(self):
        engine = self.engine()
        first = self.buy(engine)
        before = engine.snapshot()
        self.assertEqual(self.buy(engine), first)
        self.assertEqual(engine.snapshot(), before)
        with self.assertRaisesRegex(ReplayDataError, "不同参数"):
            self.buy(engine, 100)
        self.advance(engine)
        self.assertEqual(self.buy(engine)["status"], "filled")
        self.assertEqual(len(engine.snapshot()["orders"]), 1)

    def test_fifo_cost_and_final_cent_remainder(self):
        engine = self.engine()
        self.buy(engine)
        self.advance(engine)
        self.buy(engine, 100, key="second")
        self.advance(engine)
        engine.submit("600000", "sell", 200, request_id="sell")
        self.advance(engine)
        snapshot = engine.snapshot()
        self.assertEqual(snapshot["fills"][-1]["cost_released"], "2065.02")
        self.assertEqual(snapshot["fills"][-1]["realized_pnl"], "68.89")
        self.assertEqual(self.position(engine)["quantity"], 100)
        self.assertEqual(self.position(engine)["cost"], "1055.01")
        engine.submit("600000", "sell", 100, request_id="last")
        self.advance(engine)
        self.assertEqual(self.position(engine)["quantity"], 0)
        self.assertEqual(self.position(engine)["cost"], "0.00")

    def test_t_plus_one_and_frozen_shares_at_actual_execution(self):
        engine = self.engine()
        rejected = engine.submit("600000", "sell", 100, request_id="empty")
        self.assertEqual(rejected["status"], "rejected")
        self.buy(engine)
        self.advance(engine)
        position = self.position(engine)
        self.assertEqual(position["sellable_now"], 0)
        self.assertEqual(position["orderable_next_open"], 200)
        sell = engine.submit("600000", "sell", 100, request_id="sell")
        self.assertEqual(sell["status"], "pending")
        duplicate = engine.submit("600000", "sell", 200, request_id="oversell")
        self.assertEqual(duplicate["status"], "rejected")
        self.assertEqual(self.position(engine)["frozen_shares"], 100)
        engine.cancel(sell["order_id"])
        self.assertEqual(self.position(engine)["frozen_shares"], 0)
        engine.submit("600000", "sell", 200, request_id="all")
        self.advance(engine)
        self.assertEqual(self.position(engine)["quantity"], 0)

    def test_cancel_idempotent_and_terminal_order_cannot_cancel(self):
        engine = self.engine()
        order = self.buy(engine)
        cancelled = engine.cancel(order["order_id"])
        self.assertEqual(engine.cancel(order["order_id"]), cancelled)
        self.buy(engine, key="actual")
        self.advance(engine)
        with self.assertRaisesRegex(ReplayDataError, "只有尚未成交"):
            engine.cancel(engine.snapshot()["orders"][-1]["order_id"])
        with self.assertRaisesRegex(ReplayDataError, "不存在"):
            engine.cancel("missing")

    def test_submission_order_is_fill_order(self):
        engine = self.engine()
        self.buy(engine, 100, key="one")
        self.buy(engine, 200, key="two")
        self.advance(engine)
        self.buy(engine, 100, key="three")
        self.advance(engine)
        snapshot = engine.snapshot()
        self.assertEqual([fill["order_id"] for fill in snapshot["fills"]],
                         [order["order_id"] for order in snapshot["orders"]])

    def test_suspension_and_not_listed_never_fill(self):
        engine = self.engine()
        self.buy(engine, 100, "000001", "suspended")
        order = self.buy(engine, 100, "600036", "unlisted")
        self.assertEqual(order["status"], "rejected")
        result = self.advance(engine)
        self.assertEqual(result["fills"], [])
        self.assertIn("停牌", result["orders"][0]["reason"])
        self.assertEqual(self.position(engine, "000001")["valuation"]["price_date"], "2024-01-02")

    def test_suspended_holding_uses_dated_price(self):
        self.securities["600000"] = replace(self.securities["600000"], suspended=(day("2024-01-04"),))
        self.source.bars["600000"] = [row for row in self.source.bars["600000"] if row["date"] != "2024-01-04"]
        engine = self.engine()
        self.buy(engine)
        self.advance(engine)
        engine.submit("600000", "sell", 100, request_id="sell")
        self.advance(engine)
        self.assertEqual(engine.snapshot()["orders"][-1]["status"], "rejected")
        self.assertEqual(self.position(engine)["market_value"], "2080.00")
        self.assertTrue(self.position(engine)["valuation"]["stale"])
        self.assertEqual(self.position(engine)["valuation"]["price_date"], "2024-01-03")

    def test_zero_volume_rejects(self):
        self.source.bars["600000"][1]["volume"] = "0"
        engine = self.engine()
        self.buy(engine)
        self.advance(engine)
        self.assertEqual(engine.snapshot()["fills"], [])
        self.assertEqual(engine.snapshot()["orders"][0]["status"], "rejected")

    def test_one_price_limit_up_rejects_buy(self):
        self.source.bars["600000"][1].update(open="11.22", high="11.22", low="11.22", close="11.22")
        engine = self.engine()
        self.buy(engine)
        self.advance(engine)
        self.assertIn("一字涨停", engine.snapshot()["orders"][0]["reason"])
        self.assertEqual(engine.snapshot()["fills"], [])

    def test_one_price_limit_down_rejects_sell(self):
        self.source.bars["600000"][2].update(open="9.36", high="9.36", low="9.36", close="9.36")
        engine = self.engine()
        self.buy(engine)
        self.advance(engine)
        engine.submit("600000", "sell", 100, request_id="sell")
        self.advance(engine)
        self.assertIn("一字跌停", engine.snapshot()["orders"][-1]["reason"])
        self.assertEqual(self.position(engine)["quantity"], 200)

    def test_opposite_limit_direction_may_fill(self):
        self.source.bars["600000"][2].update(open="11.44", high="11.44", low="11.44", close="11.44")
        engine = self.engine()
        self.buy(engine)
        self.advance(engine)
        engine.submit("600000", "sell", 100, request_id="sell")
        self.advance(engine)
        self.assertEqual(engine.snapshot()["orders"][-1]["status"], "filled")

    def test_out_of_supported_limit_range_rejects(self):
        engine = self.engine()
        for _ in range(5):
            self.advance(engine)
        self.buy(engine, 100, budget="99999")
        self.advance(engine)
        self.assertIn("涨跌幅范围", engine.snapshot()["orders"][-1]["reason"])
        self.assertEqual(engine.snapshot()["fills"], [])

    def test_missing_data_rolls_back_everything(self):
        self.source.bars["600000"] = [row for row in self.source.bars["600000"] if row["date"] != "2024-01-03"]
        engine = self.engine()
        self.buy(engine)
        before = engine.snapshot()
        with self.assertRaises(ReplayDataError):
            self.advance(engine)
        self.assertEqual(engine.snapshot(), before)

    def test_sqlite_failure_after_writes_rolls_back_cursor_ledger_and_operation(self):
        store = PortfolioLedgerStore(self.root / "engine.sqlite3")
        self.addCleanup(store.close)
        engine = self.engine(store=store)
        self.buy(engine)
        before = engine.snapshot()
        saved = store.read(engine.training_id)
        with patch.object(store, "_before_commit", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.advance(engine, "retry")
        self.assertEqual(engine.snapshot(), before)
        self.assertEqual(store.read(engine.training_id), saved)
        self.assertIsNone(store.operation(engine.training_id, "retry"))
        result = self.advance(engine, "retry")
        self.assertEqual(len(result["fills"]), 1)
        # Persisted JSON contains date, cash, lots, orders, fills and journal at
        # the SAME version; a second connection sees the committed aggregate.
        other = PortfolioLedgerStore(self.root / "engine.sqlite3")
        self.addCleanup(other.close)
        persisted = other.read(engine.training_id)
        for key in ("current_date", "account", "lots", "orders", "fills", "ledger", "daily_assets", "version"):
            if key == "account":
                self.assertEqual(persisted[key], {field: result[key][field] for field in persisted[key]})
            else:
                self.assertEqual(persisted[key], result[key])

    def test_submission_and_cancel_storage_failure_leave_state_intact(self):
        store = PortfolioLedgerStore()
        self.addCleanup(store.close)
        engine = self.engine(store=store)
        before = engine.snapshot()
        with patch.object(store, "_before_commit", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.buy(engine)
        self.assertEqual(engine.snapshot(), before)
        order = self.buy(engine)
        before = engine.snapshot()
        with patch.object(store, "_before_commit", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                engine.cancel(order["order_id"])
        self.assertEqual(engine.snapshot(), before)

    def test_duplicate_advance_and_stale_second_click(self):
        engine = self.engine()
        self.buy(engine)
        result = self.advance(engine, "same")
        duplicate = engine.advance(operation_id="same", expected_date="2024-01-02")
        self.assertEqual(result, duplicate)
        self.assertEqual(len(engine.snapshot()["fills"]), 1)
        with self.assertRaisesRegex(ReplayDataError, "过期"):
            engine.advance(operation_id="different", expected_date="2024-01-02")
        with self.assertRaisesRegex(ReplayDataError, "其他日期"):
            engine.advance(operation_id="same", expected_date="2024-01-03")
        self.advance(engine, "next")
        self.assertEqual(engine.advance(operation_id="same", expected_date="2024-01-02"), result)
        self.assertEqual(str(engine.current_date), "2024-01-04")

    def test_concurrent_retries_do_not_double_fill(self):
        engine = self.engine()
        self.buy(engine)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: engine.advance(operation_id="parallel", expected_date="2024-01-02"), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(engine.snapshot()["fills"]), 1)

    def test_distinct_concurrent_clicks_require_same_date_precondition(self):
        engine = self.engine()
        def click(key):
            try:
                return engine.advance(operation_id=key, expected_date="2024-01-02")["current_date"]
            except ReplayDataError:
                return "stale"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(click, ["one", "two"]))
        self.assertCountEqual(results, ["2024-01-03", "stale"])

    def test_engine_owns_cursor_output_is_detached_and_no_future_quotes(self):
        args = dict(stocks=list(self.securities), start=self.data["start"], end=self.data["end"],
                    calendar=self.calendar, quotes=self.quotes, securities=self.securities,
                    actions=self.actions, synthetic=True)
        replay = PortfolioReplay(**args)
        engine = PortfolioEngine(replay)
        self.addCleanup(engine.close)
        replay.advance()
        self.assertEqual(str(engine.current_date), "2024-01-02")
        before = engine.snapshot()
        for code in replay.stocks:
            engine.switch_stock(code)
        self.assertEqual(engine.snapshot(), before)
        snapshot = engine.snapshot()
        self.assertNotIn("9999", json.dumps(snapshot))
        self.assertNotIn("987654300", json.dumps(snapshot))
        snapshot["account"]["cash"] = "0"
        self.assertEqual(engine.snapshot()["account"]["cash"], "100000.00")
        with self.assertRaisesRegex(ReplayDataError, "未来"):
            engine.history("600000", through="2024-01-10")
        with self.assertRaisesRegex(ReplayDataError, "复权"):
            engine.history("600000", adjustment="forward")

    def test_cash_dividend_uses_record_date_and_is_applied_once(self):
        self.action()
        engine = self.engine()
        self.buy(engine)
        self.advance(engine)  # record 200 shares
        engine.submit("600000", "sell", 100, request_id="sold-after-record")
        self.advance(engine)
        cash_before = Decimal(engine.snapshot()["account"]["cash"])
        result = self.advance(engine, "event")
        self.assertEqual(Decimal(result["account"]["cash"]), cash_before + 40)
        self.assertEqual(result["account"]["realized_pnl"], "51.95")
        self.assertEqual(self.position(engine)["cost"], "1032.51")
        dividends = [entry for entry in result["ledger"] if entry["kind"] == "cash_dividend"]
        self.assertEqual(len(dividends), 1)
        self.assertEqual(dividends[0]["cash_delta"], "40.00")
        self.assertEqual(engine.advance(operation_id="event", expected_date="2024-01-04"), result)
        engine.reconcile()

    def test_post_record_purchase_gets_no_dividend(self):
        self.action()
        engine = self.engine()
        self.advance(engine)  # record zero
        self.buy(engine)
        self.advance(engine)  # buy after record
        cash = engine.snapshot()["account"]["cash"]
        self.advance(engine)
        self.assertEqual(engine.snapshot()["account"]["cash"], cash)

    def test_bonus_shares_cost_t1_credit_and_odd_lot_sale(self):
        self.action("bonus_shares")
        engine = self.engine()
        self.buy(engine)
        self.advance(engine)
        self.advance(engine)
        self.advance(engine)  # 50 shares credited, sellable Jan 8
        position = self.position(engine)
        self.assertEqual(position["quantity"], 250)
        self.assertEqual(position["cost"], "2065.02")
        self.assertEqual(position["unit_cost"], "8.26")
        self.assertEqual(position["sellable_now"], 200)
        self.assertEqual(position["orderable_next_open"], 250)
        self.assertEqual(engine.snapshot()["lots"][0]["cost"], "1652.02")
        self.assertEqual(engine.snapshot()["lots"][1]["cost"], "413.00")
        bad = engine.submit("600000", "sell", 25, request_id="bad-odd")
        self.assertEqual(bad["status"], "rejected")
        good = engine.submit("600000", "sell", 250, request_id="all-odd")
        self.assertEqual(good["status"], "pending")
        self.advance(engine)
        self.assertEqual(self.position(engine)["quantity"], 0)
        self.assertEqual(self.position(engine)["cost"], "0.00")
        engine.reconcile()

    def test_entire_odd_remainder_can_sell_alone_without_splitting(self):
        self.action("bonus_shares")
        engine = self.engine()
        self.buy(engine)
        for _ in range(3):
            self.advance(engine)
        odd = engine.submit("600000", "sell", 50, request_id="odd-only")
        self.assertEqual(odd["status"], "pending")
        self.assertEqual(self.position(engine)["orderable_next_open"], 200)
        self.advance(engine)
        self.assertEqual(self.position(engine)["quantity"], 200)
        engine.reconcile()

    def test_advance_generates_uuid_when_caller_does_not_supply_one(self):
        from uuid import UUID
        engine = self.engine()
        result = engine.advance(expected_date="2024-01-02")
        self.assertEqual(str(UUID(result["operation_id"])), result["operation_id"])
        self.assertEqual(engine.advance(operation_id=result["operation_id"], expected_date="2024-01-02"), result)

    def test_ipo_first_five_sessions_and_later_ten_percent_rule(self):
        self.source.bars["600036"][1].update(open="25", high="26", low="24", close="25")
        engine = self.engine()
        self.advance(engine)
        self.advance(engine)  # first listed close Jan 4: 20.30
        order = self.buy(engine, 100, "600036", "new-ipo", budget="3000")
        self.assertEqual(order["status"], "pending")
        self.advance(engine)
        self.assertEqual(engine.snapshot()["orders"][-1]["status"], "filled")
        self.assertEqual(engine.snapshot()["fills"][-1]["price"], "25")

    def test_fee_configuration_is_pinned_and_does_not_follow_caller_mutation(self):
        fees = FeeSchedule()
        engine = self.engine(fees=fees)
        fees.periods = ()
        self.buy(engine)
        self.advance(engine)
        self.assertEqual(engine.snapshot()["fills"][0]["fees"]["total"], "5.02")

    def test_order_across_tax_boundary_uses_execution_date_version(self):
        dates = ["2023-08-24", "2023-08-25", "2023-08-28", "2023-08-29"]
        evidence = replace(self.calendar.evidence, start=day(dates[0]), end=day(dates[-1]))
        self.calendar = replace(self.calendar, dates=tuple(dates), evidence=evidence)
        self.data.update(start=dates[0], end=dates[-1])
        self.securities["600000"] = replace(self.securities["600000"], evidence=evidence)
        self.actions["600000"] = replace(self.actions["600000"], evidence=evidence)
        self.source.bars["600000"] = [dict(row, date=value) for row, value in zip(self.source.bars["600000"], dates)]
        engine = self.engine(stocks=["600000"])
        self.buy(engine, 100)
        self.advance(engine)
        sell = engine.submit("600000", "sell", 100, request_id="tax-boundary")
        self.assertEqual(sell["fee_version"], "cn-a-20230828-v1")
        result = self.advance(engine)
        fill = result["fills"][-1]
        self.assertEqual(fill["date"], "2023-08-28")
        self.assertEqual(fill["fees"]["stamp"], "0.53")
        self.assertEqual(fill["fees"]["version"], "cn-a-20230828-v1")
        self.assertEqual(result["fills"][0]["fees"]["version"], "cn-a-20220429-v1")

    def test_bonus_delayed_credit_prevents_early_reservation(self):
        self.action("bonus_shares", available_on="2024-01-09")
        engine = self.engine()
        self.buy(engine)
        for _ in range(3):
            self.advance(engine)
        self.assertEqual(self.position(engine)["orderable_next_open"], 200)
        self.assertEqual(engine.submit("600000", "sell", 250, request_id="early")["status"], "rejected")

    def test_corporate_action_cancels_pending_order_before_matching(self):
        self.action()
        engine = self.engine()
        self.buy(engine)
        self.advance(engine)
        self.advance(engine)
        pending = self.buy(engine, 100, key="before-ex")
        self.assertEqual(pending["status"], "pending")
        self.advance(engine)
        self.assertEqual(engine.snapshot()["orders"][-1]["status"], "cancelled")
        self.assertEqual(engine.snapshot()["account"]["frozen_cash"], "0.00")

    def test_suspended_ex_date_uses_explicit_reference_then_carries_it(self):
        self.action("bonus_shares", reference_price="8.48")
        self.securities["600000"] = replace(self.securities["600000"], suspended=(day("2024-01-05"), day("2024-01-08")))
        self.source.bars["600000"] = [row for row in self.source.bars["600000"] if row["date"] not in {"2024-01-05", "2024-01-08"}]
        engine = self.engine()
        self.buy(engine)
        for _ in range(3):
            self.advance(engine)
        self.assertEqual(self.position(engine)["market_value"], "2120.00")
        self.assertTrue(self.position(engine)["valuation"]["action_reference"])
        self.advance(engine)
        self.assertEqual(self.position(engine)["valuation"]["price"], "8.48")
        self.assertEqual(self.position(engine)["valuation"]["price_date"], "2024-01-05")

    def test_incomplete_unsupported_and_partial_window_actions_are_blocked(self):
        for options in [dict(kind="rights"), dict(record_on=None), dict(reference_price=None),
                        dict(record_on="2023-12-29"), dict(window_end="2024-01-08")]:
            with self.subTest(options=options):
                self.action(**options)
                with self.assertRaisesRegex(ReplayDataError, "公司行为"):
                    self.engine()

    def test_fractional_bonus_entitlement_blocks_atomically(self):
        self.action("bonus_shares", share_ratio="0.001")
        engine = self.engine()
        self.buy(engine)
        before = engine.snapshot()
        with self.assertRaisesRegex(ReplayDataError, "不足一股"):
            self.advance(engine)
        self.assertEqual(engine.snapshot(), before)

    def test_decimal_precision_and_invalid_inputs(self):
        self.assertEqual(money("1.005"), Decimal("1.01"))
        engine = self.engine()
        for value in (True, 0, -1, 100.0, "100"):
            with self.subTest(value=value), self.assertRaises(ReplayDataError):
                self.buy(engine, value)
        for value in ("NaN", "Infinity", "0", "-1", "100.001"):
            with self.subTest(value=value), self.assertRaises(ReplayDataError):
                self.engine(value)
        for qty in (1, 99, 1000001):
            self.assertEqual(self.buy(engine, qty, key=f"bad-{qty}")["status"], "rejected")
        for budget in ("NaN", "0", "-1", "1.001"):
            with self.subTest(budget=budget), self.assertRaises(ReplayDataError):
                self.buy(engine, key=f"bad-{budget}", budget=budget)

    def test_end_of_range_has_no_next_order_or_advance(self):
        engine = self.engine(end="2024-01-03")
        self.advance(engine)
        before = engine.snapshot()
        with self.assertRaisesRegex(ReplayDataError, "末日"):
            self.buy(engine)
        with self.assertRaisesRegex(ReplayDataError, "末日"):
            self.advance(engine)
        self.assertEqual(engine.snapshot(), before)

    def test_ledger_reconstruction_matches_each_symbol_and_daily_equity(self):
        engine = self.engine()
        self.buy(engine)
        self.advance(engine)
        self.buy(engine, 100, "000001", "second-stock")
        self.advance(engine)
        engine.submit("600000", "sell", 100, request_id="partial")
        self.advance(engine)
        state = engine.snapshot()
        # Independent arithmetic from the public journal, not engine helpers.
        cash = sum(Decimal(row["cash_delta"]) for row in state["ledger"])
        self.assertEqual(cash, Decimal(state["account"]["cash"]))
        for position in state["positions"]:
            rows = [row for row in state["ledger"] if row["stock"] == position["stock"]]
            self.assertEqual(sum(row["quantity_delta"] for row in rows), position["quantity"])
            self.assertEqual(sum(Decimal(row["cost_delta"]) for row in rows), Decimal(position["cost"]))
        for daily in state["daily_assets"]:
            rows = [row for row in state["ledger"] if row["date"] <= daily["date"]]
            daily_cash = sum(Decimal(row["cash_delta"]) for row in rows)
            market = Decimal("0")
            for code, mark in daily["marks"].items():
                shares = sum(row["quantity_delta"] for row in rows if row["stock"] == code)
                if shares:
                    market += Decimal(mark["price"]) * shares
            self.assertEqual(daily_cash + market, Decimal(daily["total_assets"]))
        engine.reconcile()


class FeeTests(unittest.TestCase):
    def test_historical_boundary_and_buy_has_no_stamp(self):
        fees = FeeSchedule()
        self.assertEqual(fees.calculate("2023-08-25", "sell", "10000")["total"], Decimal("15.10"))
        self.assertEqual(fees.calculate("2023-08-28", "sell", "10000")["total"], Decimal("10.10"))
        self.assertEqual(fees.calculate("2023-08-28", "buy", "10000")["total"], Decimal("5.10"))
        self.assertNotEqual(fees.for_date("2023-08-25").version, fees.for_date("2023-08-28").version)

    def test_configurable_commission_and_minimum(self):
        fees = FeeSchedule([FeePeriod("2024-01-01", "2024-12-31", "test-broker-v1", "0.0005", "0.00001", "0.001", "1")])
        self.assertEqual(fees.calculate("2024-01-02", "buy", "10000")["commission"], Decimal("10.00"))
        self.assertEqual(fees.calculate("2024-01-02", "buy", "100")["commission"], Decimal("1.00"))

    def test_unknown_dates_and_overlapping_rules_block(self):
        for value in ("2022-04-28", "2026-10-28"):
            with self.assertRaisesRegex(ReplayDataError, "缺少"):
                FeeSchedule().for_date(value)
        with self.assertRaises(ReplayDataError):
            FeeSchedule([FeePeriod("2024-01-01", "2024-01-31", "one", "0", "0"),
                         FeePeriod("2024-01-31", "2024-02-28", "two", "0", "0")])


if __name__ == "__main__":
    unittest.main()
