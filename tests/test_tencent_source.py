import unittest
from unittest.mock import Mock, patch

import requests

from backend.tencent_source import (FALLBACK_URL, URL, TencentSourceUnavailable,
                                    _request_rows, fetch_raw_rows)


class TencentRawTests(unittest.TestCase):
    def payload(self, payload):
        response = Mock()
        response.json.return_value = payload
        session = Mock()
        session.get.return_value = response
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        return patch("backend.tencent_source.requests.Session", return_value=session), session

    def test_raw_fetch_never_falls_back_to_adjusted_key(self):
        manager, session = self.payload({"code": 0, "data": {"sh600000": {"qfqday": [["2024-01-02", "1", "1", "1", "1", "100"]]}}})
        with manager, patch("backend.tencent_source.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "未复权日线字段"):
                fetch_raw_rows("600000", "2024-01-02", "2024-01-02")
        self.assertEqual(session.get.call_count, 2)
        self.assertFalse(session.trust_env)

    def test_provider_rows_are_preserved_for_strict_validation(self):
        rows = [["not-a-date", "0", "nan", "2", "3", "-1"],
                ["2024-01-02", "10", "11", "12", "9", "1"],
                ["2024-01-02", "10", "12", "12", "9", "1"]]
        manager, session = self.payload({"code": 0, "data": {"sh600000": {"day": rows}}})
        with manager:
            self.assertEqual(fetch_raw_rows("600000", "2024-01-02", "2024-01-10"), rows)
        self.assertEqual(session.get.call_args.kwargs["params"]["param"], "sh600000,day,2024-01-02,2024-01-10,640,")
        self.assertEqual(session.get.call_args.kwargs["timeout"], (6, 15))

    def test_full_page_is_not_mistaken_for_complete_coverage(self):
        rows = [["2024-01-02", "10", "10", "10", "10", "100"]] * 640
        manager, _ = self.payload({"code": 0, "data": {"sh600000": {"day": rows}}})
        with manager, patch("backend.tencent_source.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "上限"):
                _request_rows("sh600000", "2024-01-02", "2024-01-10", "")

    def test_segment_boundaries_do_not_overlap(self):
        with patch("backend.tencent_source._request_rows", return_value=[]) as request:
            self.assertEqual(fetch_raw_rows("000001", "2020-01-01", "2024-01-10"), [])
        self.assertEqual([call.args for call in request.call_args_list], [
            ("sz000001", "2020-01-01", "2021-12-31", ""),
            ("sz000001", "2022-01-01", "2023-12-31", ""),
            ("sz000001", "2024-01-01", "2024-01-10", ""),
        ])

    def test_http_501_switches_official_endpoint_without_repeating_blocked_host(self):
        rows = [["2024-01-02", "10", "10", "11", "9", "123"]]
        manager, session = self.payload({"code": 0, "data": {"sh600918": {"day": rows}}})
        blocked, working = Mock(), session.get.return_value
        blocked.raise_for_status.side_effect = requests.HTTPError("501 WAF")
        session.get.side_effect = [blocked, working]
        with manager, patch("backend.tencent_source.time.sleep") as sleep:
            self.assertEqual(fetch_raw_rows("600918", "2024-01-02", "2024-01-10"), rows)
        self.assertEqual([call.args[0] for call in session.get.call_args_list], [URL, FALLBACK_URL])
        sleep.assert_not_called()

    def test_both_http_failures_are_reported_as_source_outage(self):
        manager, session = self.payload({})
        session.get.return_value.raise_for_status.side_effect = requests.HTTPError("501 WAF")
        with manager, self.assertRaises(TencentSourceUnavailable):
            fetch_raw_rows("600918", "2024-01-02", "2024-01-10")
        self.assertEqual(session.get.call_count, 2)

    def test_growth_and_star_stocks_use_correct_market(self):
        for code, symbol in [("300508", "sz300508"), ("688351", "sh688351")]:
            with self.subTest(code=code), patch("backend.tencent_source._request_rows", return_value=[]) as request:
                self.assertEqual(fetch_raw_rows(code, "2024-01-02", "2024-01-10"), [])
                request.assert_called_once_with(symbol, "2024-01-02", "2024-01-10", "")


if __name__ == "__main__":
    unittest.main()
