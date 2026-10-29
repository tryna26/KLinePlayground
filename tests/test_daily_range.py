"""Range downloads must not fall through to the legacy 2010 full-history path."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from backend.data_manager import DataManager
from backend.tencent_source import TencentSourceUnavailable


class DailyRangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = DataManager(self.temp.name)
        self.rows = [["2024-01-02", "10", "10.3", "10.8", "9.8", "1000.25"],
                     ["2024-01-03", "10.1", "10.4", "10.9", "9.9", "1200"]]

    def test_tencent_fetches_only_requested_raw_range_and_reuses_source_cache(self):
        with patch("backend.tencent_source.fetch_raw_rows", return_value=self.rows) as fetch, \
             patch.object(self.manager, "get_stock_data", side_effect=AssertionError("must not download from 2010")), \
             patch.object(self.manager, "download_stock_data", side_effect=AssertionError("legacy path")):
            first = self.manager.get_stock_data_range("600918", "2024-01-02", "2024-01-03")
            second = self.manager.get_stock_data_range("600918", "2024-01-02", "2024-01-03")
        fetch.assert_called_once_with("600918", "2024-01-02", "2024-01-03")
        pd.testing.assert_frame_equal(first, second, check_dtype=False)
        self.assertEqual(len(first), 2)
        self.assertEqual(first.iloc[0].volume, 1000.25)
        self.assertFalse((Path(self.manager.kline_dir) / "600918.csv").exists())

    def test_other_online_sources_receive_same_bounded_dates(self):
        frame = self.manager._normalize_daily_kline(pd.DataFrame(self.rows, columns=["date", "open", "close", "high", "low", "volume"]))
        with patch.object(self.manager, "_fetch_stock_bundle", return_value={"kline": frame}) as fetch:
            self.manager.get_stock_data_range("600918", "2024-01-02", "2024-01-03", source="akshare")
        fetch.assert_called_once_with("600918", "2024-01-02", "akshare", end_date="2024-01-03")
        # The AKShare result must not fill the Tencent cache for the same stock.
        with patch("backend.tencent_source.fetch_raw_rows", return_value=self.rows) as raw:
            self.manager.get_stock_data_range("600918", "2024-01-02", "2024-01-03")
        raw.assert_called_once()

    def test_new_range_has_its_own_request(self):
        with patch("backend.tencent_source.fetch_raw_rows", return_value=self.rows) as fetch:
            self.manager.get_stock_data_range("600918", "2024-01-02", "2024-01-03")
            self.manager.get_stock_data_range("600918", "2024-01-02", "2024-01-04")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args.args, ("600918", "2024-01-02", "2024-01-04"))

    def test_transport_failure_propagates_without_creating_cache(self):
        with patch("backend.tencent_source.fetch_raw_rows", side_effect=TencentSourceUnavailable("501")):
            with self.assertRaises(TencentSourceUnavailable):
                self.manager.get_stock_data_range("600918", "2024-01-02", "2024-01-03")
        self.assertFalse(list(Path(self.temp.name).rglob("*.csv")))

    def test_empty_pre_listing_response_is_not_cached_as_available_history(self):
        with patch("backend.tencent_source.fetch_raw_rows", return_value=[]):
            self.assertIsNone(self.manager.get_stock_data_range("600918", "2010-01-01", "2011-12-31"))
        self.assertFalse(list(Path(self.temp.name).rglob("*.csv")))


if __name__ == "__main__":
    unittest.main()
