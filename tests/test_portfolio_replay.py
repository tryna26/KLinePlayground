from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.portfolio_replay import PortfolioReplay
from backend.replay_calendar import ReplayCalendar, load_public_calendar
from backend.replay_data import QuoteRepository, TencentDailySource
from backend.replay_models import (
    ActionHistory, CorporateAction, DataStatus, Evidence, ReplayDataError,
    day, stock_code, validate_bars,
)
from backend.replay_storage import read_cache, write_cache
from tests.replay_fixture import fixture, replay


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="portfolio-step2-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.data, self.calendar, self.securities, self.actions,
         self.source, self.quotes) = fixture(self.root)

    def session(self, **overrides):
        args = dict(stocks=list(self.securities), start=self.data["start"], end=self.data["end"],
                    calendar=self.calendar, quotes=self.quotes, securities=self.securities,
                    actions=self.actions, synthetic=True)
        args.update(overrides)
        return PortfolioReplay(**args)

    def quote(self, code="600000", date="2024-01-02", **kwargs):
        return self.quotes.quote(code, date, self.calendar, self.securities[code], synthetic=True, **kwargs)

    def test_three_stocks_same_date_have_distinct_states(self):
        session = self.session()
        snapshot = session.advance()
        self.assertEqual(snapshot["current_date"], "2024-01-03")
        self.assertEqual([state["status"] for state in snapshot["stocks"]],
                         ["trading", "suspended", "not_listed"])
        self.assertEqual(snapshot["stocks"][0]["bar"]["open"], "10.30")
        self.assertIsNone(snapshot["stocks"][1]["bar"])
        self.assertFalse(snapshot["stocks"][1]["execution_data_ready"])

    def test_switching_changes_neither_cursor_nor_account_observations(self):
        session = self.session()
        before = session.account_data()
        session.switch_stock("000001")
        session.switch_stock("600036")
        self.assertEqual(str(session.current_date), "2024-01-02")
        self.assertEqual(session.account_data(), before)

    def test_calendar_advances_through_suspension_and_skips_weekend(self):
        session = self.session()
        dates = [session.advance()["current_date"] for _ in range(4)]
        self.assertEqual(dates, ["2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"])

    def test_last_price_is_dated_and_only_for_valuation(self):
        session = self.session()
        state = session.advance()["stocks"][1]
        self.assertEqual(state["valuation"], {"price": "8.10", "price_date": "2024-01-02", "stale": True})
        self.assertIsNone(state["bar"])
        self.assertFalse(state["execution_data_ready"])

    def test_delisted_and_not_listed_never_download_fake_bars(self):
        self.assertEqual(self.quote("600036").status, DataStatus.NOT_LISTED)
        self.assertEqual(self.quote("600036", "2024-01-10").status, DataStatus.DELISTED)
        self.assertEqual(self.quote("000001", "2024-01-03").status, DataStatus.SUSPENDED)
        self.assertEqual(self.source.calls, [])

    def test_weekend_is_market_closed(self):
        self.assertEqual(self.quote(date="2024-01-06").status, DataStatus.MARKET_CLOSED)
        self.assertEqual(self.source.calls, [])

    def test_cache_missing_is_not_suspension(self):
        state = self.quote(repair=False)
        self.assertEqual(state.status, DataStatus.CACHE_MISSING)
        self.assertFalse(state.execution_data_ready)

    def test_missing_bar_is_repaired_once_and_then_available(self):
        self.assertEqual(self.quote().bar.close, Decimal("10.20"))
        self.assertEqual(self.quote().bar.close, Decimal("10.20"))
        self.assertEqual(len(self.source.calls), 1)

    def test_successful_but_empty_download_remains_unknown(self):
        self.source.bars["600000"] = []
        state = self.quote()
        self.assertEqual(state.status, DataStatus.UNKNOWN)
        self.assertIsNone(state.bar)
        self.assertIn("不能直接判为停牌", state.reason)
        self.assertEqual(self.quote().status, DataStatus.UNKNOWN)
        self.assertEqual(len(self.source.calls), 1)

    def test_download_failure_blocks_execution(self):
        self.source.fail = True
        state = self.quote()
        self.assertEqual(state.status, DataStatus.DOWNLOAD_FAILED)
        self.assertIsNone(state.bar)
        self.assertFalse(state.execution_data_ready)

    def test_failed_refresh_never_trades_at_old_price(self):
        self.quotes.prime("600000", "2024-01-02", "2024-01-10")
        self.source.fail = True
        self.quotes.prime("600000", "2024-01-02", "2024-01-10", refresh=True)
        self.assertEqual(self.quote().status, DataStatus.DOWNLOAD_FAILED)
        self.source.fail = False
        self.quotes.prime("600000", "2024-01-02", "2024-01-02", refresh=True)
        self.assertEqual(self.quote().status, DataStatus.TRADING)
        self.assertEqual(self.quote().status, DataStatus.TRADING)
        # A successful one-day repair does not validate the other failed dates.
        self.assertEqual(self.quote(date="2024-01-03", repair=False).status, DataStatus.DOWNLOAD_FAILED)

    def test_unknown_lifecycle_is_not_inferred_from_valid_prices(self):
        self.quotes.prime("600000", "2024-01-02", "2024-01-10")
        state = self.quotes.quote("600000", "2024-01-02", self.calendar, None, synthetic=True)
        self.assertEqual(state.status, DataStatus.UNKNOWN)
        self.assertIsNone(state.bar)

    def test_zero_volume_has_distinct_non_executable_state(self):
        self.source.bars["600000"][0]["volume"] = "0"
        state = self.quote()
        self.assertEqual(state.status, DataStatus.NO_TRADES)
        self.assertFalse(state.execution_data_ready)
        self.assertEqual(state.bar.close, Decimal("10.20"))

    def test_bad_data_never_enter_cache(self):
        self.source.bars["600000"][0]["low"] = "99"
        self.assertEqual(self.quote().status, DataStatus.INVALID)
        self.assertEqual(list(self.root.rglob("*.json")), [])

    def test_cache_provenance_and_identical_reload(self):
        first = self.quotes.prime("600000", "2024-01-02", "2024-01-10")
        self.assertEqual(len(self.source.calls), 1)
        path = next(self.root.rglob("*.json"))
        payload = read_cache(path)
        self.assertEqual(payload["actual_start"], "2024-01-02")
        self.assertEqual(payload["requested_end"], "2024-01-10")
        self.assertEqual(payload["price_basis"], "unadjusted")
        self.assertEqual(payload["volume_unit"], "share")
        other = QuoteRepository(self.root, self.source)
        cached = other.prime("600000", "2024-01-02", "2024-01-10", offline=True)
        self.assertEqual(cached, first)
        self.assertEqual(len(self.source.calls), 1)

    def test_cache_corruption_is_rejected_offline_and_repaired_online(self):
        self.quotes.prime("600000", "2024-01-02", "2024-01-10")
        path = next(self.root.rglob("*.json"))
        path.write_text('{"schema_version": 1}', encoding="utf-8")
        other = QuoteRepository(self.root, self.source)
        self.assertIsNone(other.prime("600000", "2024-01-02", "2024-01-10", offline=True))
        self.assertIsNotNone(other.prime("600000", "2024-01-02", "2024-01-10"))
        self.assertEqual(len(self.source.calls), 2)

    def test_expired_cache_must_refresh_or_explicitly_pin(self):
        self.quotes.prime("600000", "2024-01-02", "2024-01-10")
        path = next(self.root.rglob("*.json"))
        payload = read_cache(path)
        payload["updated_at"] = "2000-01-01T00:00:00+00:00"
        write_cache(path, payload)
        other = QuoteRepository(self.root, self.source)
        self.assertIsNone(other.prime("600000", "2024-01-02", "2024-01-10", offline=True))
        pinned = QuoteRepository(self.root, self.source, max_age=None)
        self.assertIsNotNone(pinned.prime("600000", "2024-01-02", "2024-01-10", offline=True))

    def test_wrong_price_basis_or_volume_unit_is_rejected(self):
        self.quotes.prime("600000", "2024-01-02", "2024-01-10")
        path = next(self.root.rglob("*.json"))
        original = read_cache(path)
        for field, value in [("price_basis", "forward_adjusted"), ("volume_unit", "lot")]:
            with self.subTest(field=field):
                write_cache(path, {**original, field: value})
                other = QuoteRepository(self.root, self.source)
                self.assertIsNone(other.prime("600000", "2024-01-02", "2024-01-10", offline=True))

    def test_cache_write_failure_is_explained(self):
        with patch("backend.replay_data.write_cache", side_effect=OSError("disk full")):
            self.assertEqual(self.quote().status, DataStatus.CACHE_MISSING)
            self.assertIsNone(self.quote().bar)

    def test_future_data_absent_from_every_public_output(self):
        session = self.session()
        for _ in range(6):
            current = str(session.current_date)
            outputs = [session.snapshot(), session.account_data()]
            for code in session.stocks:
                outputs.extend([session.history(code), session.chart_data(code), session.indicator_data(code)])
                self.assertTrue(all(row["date"] <= current for row in session.history(code)))
            serialized = json.dumps(outputs)
            # Match complete poison values; timestamps and digests may contain
            # the same digits without revealing a future quote.
            self.assertNotIn('"9999.00"', serialized)
            self.assertNotIn('"987654300"', serialized)
            session.advance()
        self.assertEqual(session.history("600000")[-1]["close"], "9999.00")

    def test_future_override_and_full_data_are_rejected(self):
        session = self.session()
        with self.assertRaisesRegex(ReplayDataError, "未来"):
            session.history("600000", through="2024-01-10")
        with self.assertRaisesRegex(ReplayDataError, "未来"):
            session.full_data("600000")
        with self.assertRaises(TypeError):
            session.history("600000", full=True)
        with self.assertRaises(AttributeError):
            session.current_date = day("2024-01-10")

    def test_adjustment_cannot_use_future_factors(self):
        session = self.session()
        with self.assertRaisesRegex(ReplayDataError, "复权"):
            session.history("600000", adjustment="forward")
        self.assertEqual(session.history("600000")[0]["open"], "10.00")

    def test_output_mutation_and_source_refresh_do_not_change_run(self):
        session = self.session()
        session.snapshot()["stocks"][0]["bar"]["close"] = "300"
        self.source.bars["600000"][0]["close"] = "10.40"
        self.quotes.prime("600000", "2024-01-02", "2024-01-10", refresh=True)
        self.assertEqual(session.chart_data("600000")[0]["close"], "10.20")

    def test_unknown_future_day_blocks_before_cursor_moves(self):
        self.source.bars["600000"] = [row for row in self.source.bars["600000"] if row["date"] != "2024-01-03"]
        session = self.session()
        with self.assertRaisesRegex(ReplayDataError, "原因不明"):
            session.advance()
        self.assertEqual(str(session.current_date), "2024-01-02")

    def test_unknown_initial_day_prevents_creation(self):
        self.source.fail = True
        with self.assertRaisesRegex(ReplayDataError, "下载失败"):
            self.session()

    def test_unknown_actions_prevent_creation_before_download(self):
        self.actions.clear()
        with self.assertRaisesRegex(ReplayDataError, "公司行为完整性"):
            self.session()
        self.assertEqual(self.source.calls, [])

    def test_action_coverage_must_include_whole_training_range(self):
        action = self.actions["600000"]
        self.actions["600000"] = replace(action, evidence=replace(action.evidence, end=day("2024-01-05")))
        with self.assertRaisesRegex(ReplayDataError, "完整性未核实"):
            self.session()

    def test_all_unimplemented_corporate_actions_block_overlap(self):
        for kind in ["cash_dividend", "bonus_shares", "split", "rights", "unknown"]:
            with self.subTest(kind=kind):
                event = CorporateAction("fixture-event", kind, "2024-01-03", "synthetic test", "2024-01-08")
                self.actions["600000"] = replace(self.actions["600000"], events=(event,))
                with self.assertRaisesRegex(ReplayDataError, "公司行为"):
                    self.session()
                # Starting after ex-date but before share credit is still unsafe.
                with self.assertRaisesRegex(ReplayDataError, "公司行为"):
                    self.session(start="2024-01-05")

    def test_past_corporate_action_does_not_block_later_event_free_range(self):
        event = CorporateAction("past-event", "rights", "2023-01-03", "synthetic test", "2023-01-09")
        self.actions["600000"] = replace(self.actions["600000"], events=(event,))
        self.assertEqual(str(self.session().current_date), "2024-01-02")

    def test_synthetic_evidence_cannot_enable_real_training(self):
        with self.assertRaises(ReplayDataError):
            self.session(synthetic=False)
        self.assertEqual(self.quotes.quote("600000", "2024-01-02", self.calendar,
                                          self.securities["600000"]).status, DataStatus.UNSUPPORTED)

    def test_verified_special_history_blocks_even_if_current_name_is_normal(self):
        self.securities["600000"] = replace(self.securities["600000"], special_periods=(("2024-01-03", "2024-01-05", "ST"),))
        with self.assertRaisesRegex(ReplayDataError, "特殊交易状态"):
            self.session()
        self.assertEqual(self.quote(date="2024-01-03").status, DataStatus.UNSUPPORTED)

    def test_lifecycle_proof_must_cover_whole_range(self):
        security = self.securities["600000"]
        self.securities["600000"] = replace(security, evidence=replace(security.evidence, end=day("2024-01-05")))
        with self.assertRaisesRegex(ReplayDataError, "历史上市"):
            self.session()

    def test_stock_scope_and_pool_validation(self):
        for code in ["300001", "688001", "830001", "920001", "600519", "301569"]:
            self.assertEqual(stock_code(code), code)
        for code in ["510050", "../000001", 1, "00001", "600１２３"]:
            with self.subTest(code=code), self.assertRaises(ReplayDataError):
                stock_code(code)
        for pool in [[], ["600000", "600000"]]:
            with self.assertRaises(ReplayDataError):
                self.session(stocks=pool)
        with self.assertRaisesRegex(ReplayDataError, "股票池"):
            self.session().chart_data("600001")

    def test_calendar_bounds_and_end_are_explicit(self):
        self.assertEqual(str(self.calendar.next_day("2024-01-05")), "2024-01-08")
        with self.assertRaises(ReplayDataError):
            self.calendar.next_day("2024-01-10")
        with self.assertRaises(ReplayDataError):
            self.calendar.is_open("2025-01-01")
        with self.assertRaises(ReplayDataError):
            self.session(end="2024-01-11")
        session = self.session()
        for _ in range(6):
            session.advance()
        with self.assertRaisesRegex(ReplayDataError, "末日"):
            session.advance()

    def test_bar_on_closed_market_day_is_rejected(self):
        row = deepcopy(self.source.bars["600000"][0])
        row["date"] = "2024-01-06"
        self.source.bars["600000"].append(row)
        with self.assertRaisesRegex(ReplayDataError, "休市日"):
            self.session()

    def test_public_calendar_cache_works_offline(self):
        path = self.root / "portfolio_replay" / "calendar.json"
        evidence = replace(self.calendar.evidence, scope="reviewed")
        write_cache(path, {"provider": "sina-public-calendar-v1", "dates": self.data["calendar"],
                           "evidence": {**evidence.__dict__, "start": str(evidence.start), "end": str(evidence.end)}})
        with patch("requests.Session.get", side_effect=AssertionError("unexpected network")):
            cached = load_public_calendar(self.root)
        self.assertEqual(cached.dates, self.calendar.dates)
        with patch("requests.Session.get", side_effect=OSError("offline")):
            with self.assertRaisesRegex(ReplayDataError, "公共交易日历获取失败"):
                load_public_calendar(self.root, refresh=True)
        self.assertEqual(load_public_calendar(self.root).dates, self.calendar.dates)

    def test_invalid_calendar_cache_has_actionable_message(self):
        write_cache(self.root / "portfolio_replay" / "calendar.json", {"provider": "sina-public-calendar-v1"})
        with self.assertRaisesRegex(ReplayDataError, "显式刷新"):
            load_public_calendar(self.root)

    def test_status_evidence_is_returned_even_without_price(self):
        state = self.quote("000001", "2024-01-03").to_dict()
        self.assertEqual(state["status_source"], "synthetic fixture")
        self.assertEqual(state["status_version"], "1")
        self.assertIsNone(state["bar"])


class BarValidationTests(unittest.TestCase):
    def setUp(self):
        self.row = {"date": "2024-01-02", "open": "10.00", "close": "10.20", "high": "10.50", "low": "9.50", "volume": "10000"}

    def validate(self, rows):
        return validate_bars("600000", rows, "2024-01-02", "2024-01-10")

    def test_sort_and_deduplicate_only_identical_records(self):
        later = {**self.row, "date": "2024-01-03"}
        bars = self.validate([later, self.row, deepcopy(self.row)])
        self.assertEqual([str(bar.date) for bar in bars], ["2024-01-02", "2024-01-03"])
        self.assertIsInstance(bars[0].close, Decimal)
        with self.assertRaisesRegex(ReplayDataError, "冲突"):
            self.validate([self.row, {**self.row, "close": "10.40"}])

    def test_invalid_records_are_never_silently_dropped(self):
        variants = [dict(open="0"), dict(close="-1"), dict(high="9"), dict(low="12"),
                    dict(volume="-1"), dict(volume="0.5"), dict(close="NaN"), dict(high="Infinity"),
                    dict(date="20240102"), dict(date="2024-02-30"), dict(date="2024-01-11"),
                    dict(stock="000001"), dict(volume=None)]
        for replacement in variants:
            with self.subTest(replacement=replacement), self.assertRaises(ReplayDataError):
                self.validate([{**self.row, **replacement}])
        missing = dict(self.row)
        del missing["close"]
        with self.assertRaises(ReplayDataError):
            self.validate([missing])

    def test_tencent_adapter_uses_only_raw_prices_and_converts_lots(self):
        with patch("backend.replay_data.fetch_raw_rows", return_value=[["2024-01-02", "10", "10.2", "10.5", "9.5", "123.45"]]) as fetch:
            rows = TencentDailySource().fetch("000001", "2024-01-02", "2024-01-02")
        fetch.assert_called_once_with("000001", "2024-01-02", "2024-01-02")
        self.assertEqual(Decimal(rows[0]["volume"]), Decimal("12345"))
        self.assertEqual(rows[0]["open"], "10")


if __name__ == "__main__":
    unittest.main()
