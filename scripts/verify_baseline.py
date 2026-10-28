"""Exercise the development HTTP entry point using disposable data and users.

Default: synthetic bars only. --online additionally downloads real Tencent data.
No market data, user records, or AI credentials are distributed with this script.
"""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="kline-baseline-") as temp:
        root = Path(temp)
        data_dir = root / "quotes"
        users_dir = root / "people"
        offline = data_dir / "a_market_offline"
        offline.mkdir(parents=True)
        dates = pd.bdate_range("2024-01-01", periods=140)
        pd.DataFrame({"date": dates, "open": 10.0, "close": 10.0,
                      "high": 11.0, "low": 9.0, "volume": 10000,
                      "factor": 1.0}).to_csv(offline / "600000.SH.csv", index=False)
        names = {"600000": "SYNTHETIC FIXTURE"}
        online_summary = None
        if args.online:
            from backend.data_manager import DataManager
            manager = DataManager(str(data_dir))
            # A bounded historical request, isolated from the synthetic fixture.
            bundle = manager._fetch_stock_bundle("000001", "2024-01-01", "tencent", "2024-06-30")
            bars = bundle["kline"]
            assert bars is not None and len(bars) > 80, "腾讯行情不足"
            assert manager._save_bundle_to_cache("000001", bars, bundle["factor"])
            names["000001"] = "平安银行"
            online_summary = {"source": "tencent", "code": "000001", "rows": len(bars),
                              "first": str(bars.date.min().date()), "last": str(bars.date.max().date())}
        pd.DataFrame({"code": list(names), "name": list(names.values())}).to_csv(data_dir / "stock_list.csv", index=False)
        (data_dir / "stock_names.json").write_text(json.dumps(names), encoding="utf-8")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {key: value for key, value in os.environ.items() if not key.startswith("KLINE_TRAINER_")}
        command = [sys.executable, str(ROOT / "run_dev.py"), "--home", str(root / "unused-home"),
                   "--data-dir", str(data_dir), "--users-dir", str(users_dir), "--port", str(port)]
        log_path = root / "server.log"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=temp, env=env, stdout=log, stderr=log)
            try:
                with requests.Session() as session:
                    session.trust_env = False
                    base = f"http://127.0.0.1:{port}"

                    def api(method, path, body=None, expected=200):
                        response = session.request(method, base + path, json=body, timeout=45)
                        assert response.status_code == expected, (path, response.status_code, response.text)
                        return response.json()

                    deadline = time.monotonic() + 45
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            raise RuntimeError(log_path.read_text(encoding="utf-8", errors="replace"))
                        try:
                            api("GET", "/api/health")
                            break
                        except requests.ConnectionError:
                            time.sleep(0.2)
                    else:
                        raise RuntimeError("开发服务启动超时")
                    page = session.get(base, timeout=10)
                    assert page.status_code == 200 and 'id="data-source"' in page.text
                    for asset in ("/js/main_enhanced.js", "/js/lightweight-charts.standalone.production.js", "/css/style_enhanced.css"):
                        assert session.get(base + asset, timeout=10).status_code == 200
                    assert api("GET", "/api/users") == []
                    api("POST", "/api/users", {"username": "baseline_check"})
                    payload = {"user": "baseline_check", "mode": "specific", "stock_code": "600000",
                               "start_date": "2024-03-01", "data_source": "offline", "initial_capital": 100000}
                    training = api("POST", "/api/training/start", payload)
                    prefix = f"/api/training/{training['id']}"
                    assert api("GET", prefix + "/data")["kline_data"]
                    api("POST", prefix + "/trade", {"action": "buy", "quantity": 1})
                    account = api("GET", prefix + "/account")
                    assert account["available_cash"] == 98995.0, account
                    assert account["position_summary"]["total_shares"] == 100, account
                    api("POST", prefix + "/trade", {"action": "sell", "quantity": 1}, expected=400)
                    advanced = api("POST", prefix + "/next")
                    assert not advanced["finished"]
                    api("POST", prefix + "/trade", {"action": "sell", "quantity": 1})
                    account = api("GET", prefix + "/account")
                    assert account["position_summary"] is None, account
                    # Original single-stock default fees: 5 buy + 5 sell + 1 stamp tax.
                    assert account["available_cash"] == 99989.0, account
                    report = api("POST", prefix + "/end")
                    assert report["final_capital"] == 99989.0, report
                    with closing(sqlite3.connect(users_dir / "baseline_check" / "training_history.db")) as conn:
                        saved = conn.execute("SELECT final_capital FROM training_sessions WHERE session_id = ?",
                                             (training["id"],)).fetchone()
                        assert saved == (99989.0,), saved
                    api("POST", "/api/training/start", {**payload, "mode": "random", "sector": "all",
                        "date_start": "2024-02-01", "date_end": "2024-04-30"})
                    if args.online:
                        api("POST", "/api/users", {"username": "online_check"})
                        online_training = api("POST", "/api/training/start", {**payload,
                            "user": "online_check", "stock_code": "000001", "data_source": "tencent"})
                        online_prefix = f"/api/training/{online_training['id']}"
                        assert api("GET", online_prefix + "/data")["kline_data"]
                        assert not api("POST", online_prefix + "/next")["finished"]
                    assert (users_dir / "baseline_check" / "trade_records.db").is_file()
                    assert (users_dir / "baseline_check" / "training_history.db").is_file()
                    assert not (root / "unused-home").exists()
                    print(json.dumps({"status": "passed", "http_server": "127.0.0.1",
                        "checks": ["static assets", "isolated user", "specified training", "buy",
                                   "T+1 rejection", "next bar", "sell", "report persistence", "random training",
                                   "external working directory", "independent data/users directories"],
                        "online": online_summary}, ensure_ascii=False, indent=2))
            finally:
                process.terminate()
                process.wait(timeout=15)


if __name__ == "__main__":
    main()
