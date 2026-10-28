import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend.runtime_paths import get_runtime_paths


class RuntimePathsTests(unittest.TestCase):
    def test_default_storage_is_local_to_checkout_even_from_other_cwd(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as cwd:
            env = {key: value for key, value in os.environ.items() if not key.startswith("KLINE_TRAINER_")}
            env["PYTHONPATH"] = str(root)
            output = subprocess.check_output(
                [sys.executable, "-c", "from backend.runtime_paths import get_runtime_paths; print(get_runtime_paths().home)"],
                cwd=cwd, env=env, text=True,
            )
        self.assertEqual(Path(output.strip()), root / ".runtime")

    def test_individual_overrides_include_trade_database_and_ai_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {"KLINE_TRAINER_HOME": str(root / "home"),
                   "KLINE_TRAINER_DATA_DIR": str(root / "quotes"),
                   "KLINE_TRAINER_USERS_DIR": str(root / "people")}
            with patch.dict(os.environ, env):
                from backend.trade_simulator_enhanced import TradeSimulatorEnhanced
                from backend.user_manager_enhanced import UserManagerEnhanced
                paths = get_runtime_paths()
                manager = UserManagerEnhanced(str(paths.users))
                self.assertTrue(manager.create_user("regression"))
                sim = TradeSimulatorEnhanced("regression", 100000, "000001")
                self.assertEqual(Path(sim.db_path), root / "people" / "regression" / "trade_records.db")
                self.assertTrue(Path(sim.db_path).is_file())
                # Check all backend writers in a fresh process, not a cached import.
                code = (
                    "from backend.app_enhanced import app, get_ai_config; "
                    "c=app.test_client(); "
                    "assert c.post('/api/system/api_info',json={}).status_code==200; "
                    "assert get_ai_config() is None"
                )
                subprocess.run([sys.executable, "-c", code], check=True)
                self.assertTrue((root / "quotes" / "ai_api_info.json").is_file())
                self.assertFalse((root / "home").exists())

    def test_overlapping_storage_is_rejected(self):
        with patch.dict(os.environ, {"KLINE_TRAINER_DATA_DIR": "same", "KLINE_TRAINER_USERS_DIR": "same/nested"}):
            with self.assertRaises(ValueError):
                get_runtime_paths()

    def test_installed_bin_layout_and_home_override(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            root = Path(temp)
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(root / "bin" / "app.exe")):
                self.assertEqual(get_runtime_paths().home, root)
                with patch.dict(os.environ, {"KLINE_TRAINER_HOME": str(root / "custom")}):
                    self.assertEqual(get_runtime_paths().data, root / "custom" / "data")


if __name__ == "__main__":
    unittest.main()
