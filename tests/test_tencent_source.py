import unittest
from unittest.mock import Mock, patch

from backend.tencent_source import _request_rows, fetch_raw_rows


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


if __name__ == "__main__":
    unittest.main()
