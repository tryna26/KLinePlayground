"""Online acceptance for the reported Tencent 501 and portfolio range-download bug.

Uses fresh temporary data/users, the stocks from the failure report, actual
Tencent observations and a real localhost server. No existing training is touched.
"""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import requests

ROOT = Path(__file__).resolve().parents[1]


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="kline-range-online-") as temporary:
        root = Path(temporary)
        data = root / "data"
        data.mkdir()
        codes = ["600918", "002320", "300508", "688351", "301295", "603109", "688006"]
        # This is an acceptance-test pool drawn from the actual error report.
        # The production blind box continues to use the full user's stock list.
        (data / "stock_names.json").write_text(json.dumps(dict.fromkeys(codes, "联网验收")), encoding="utf-8")
        (data / "stock_list.csv").write_text("code,name\n" + "".join(f"{code},联网验收\n" for code in codes), encoding="utf-8")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {key: value for key, value in os.environ.items() if not key.startswith("KLINE_TRAINER_")}
        env["PYTHONIOENCODING"] = "utf-8"
        session = requests.Session()
        session.trust_env = False
        base = f"http://127.0.0.1:{port}"

        def api(method, path, body=None, expected=200):
            response = session.request(method, base + path, json=body, timeout=120)
            assert response.status_code == expected, (path, response.status_code, response.text)
            return response.json()

        with (root / "server.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, str(ROOT / "run_dev.py"), "--home", str(root),
                                        "--port", str(port)], cwd=ROOT, env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 40
                while True:
                    if process.poll() is not None:
                        raise RuntimeError((root / "server.log").read_text(encoding="utf-8"))
                    try:
                        api("GET", "/api/health")
                        break
                    except requests.ConnectionError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Local server did not start")
                        time.sleep(.2)
                api("POST", "/api/users", {"username": "range_online_check"})
                api("POST", "/api/portfolio/session", {"user": "range_online_check"})
                params = {"stocks": codes[:4], "start_date": "2024-01-01", "end_date": "2024-06-30",
                          "initial_cash": "100000", "data_source": "tencent", "request_id": "specified"}
                specified = api("POST", "/api/portfolio/trainings", params, expected=201)
                assert specified["config"]["start_date"] == "2024-01-02"
                assert specified["config"]["end_date"] == "2024-06-28"
                assert specified["config"]["stocks"] == codes[:4]
                print("指定组合：4 只报告中失败的股票均创建成功", flush=True)
                # Check all seven failures from the report, including IPO histories.
                all_stocks = api("POST", "/api/portfolio/trainings", {**params, "stocks": codes,
                    "request_id": "all-reported-stocks"}, expected=201)
                assert len(all_stocks["quotes"]) == 7
                blind = api("POST", "/api/portfolio/trainings", {**params, "stocks": [], "mode": "random",
                    "stock_count": 3, "training_days": 30, "request_id": "blind"}, expected=201)
                assert len(set(blind["config"]["stocks"])) == 3
                assert blind["config"]["mode"] == "random"
                print("随机盲盒：3 股、30 个交易日创建成功", flush=True)
                prefix = f"/api/portfolio/trainings/{specified['training_id']}"
                state = api("POST", prefix + "/orders", {"stock": codes[0], "side": "buy", "quantity": 100,
                    "request_id": "buy", "expected_version": specified["version"]})
                state = api("POST", prefix + "/advance", {"expected_date": state["current_date"],
                    "request_id": "next", "expected_version": state["version"]})
                assert len(state["fills"]) == 1
                assert api("POST", prefix + "/restore", {}) == state
                state = api("POST", prefix + "/end", {"request_id": "end", "expected_version": state["version"]})
                assert state["status"] == "ended"
                assert api("GET", prefix + "/report")["summary"]["fill_count"] == 1
                files = sorted((data / "daily_ranges" / "tencent").glob("*.csv"))
                assert len(files) == len(codes)
                assert all(path.name.endswith("_20240102_20240628.csv") for path in files)
                assert not list((data / "kline_raw").glob("*.csv"))
                print(json.dumps({"status": "passed", "synthetic": False, "reported_stocks": codes,
                    "download_range": ["2024-01-02", "2024-06-28"], "range_cache_files": len(files),
                    "blind_stocks": blind["config"]["stocks"], "blind_start": blind["config"]["start_date"],
                    "blind_end": blind["config"]["end_date"], "fills": len(state["fills"]),
                    "report_available": True, "full_history_downloads": 0}, ensure_ascii=False, indent=2))
            finally:
                process.terminate()
                process.wait(timeout=15)
                session.close()


if __name__ == "__main__":
    main()
