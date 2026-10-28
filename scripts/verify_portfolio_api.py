"""Step 4: real localhost HTTP, hard restart and interrupted transaction checks.

All prices and users are disposable synthetic acceptance data. The restarted
server has demo mode disabled, proving restore doesn't need a fixture loader.
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
sys.path.insert(0, str(ROOT))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="kline-step4-http-") as temp:
        root = Path(temp)
        data_dir, users_dir = root / "quotes", root / "people"
        data_dir.mkdir()
        (data_dir / "stock_list.csv").write_text("code,name\n600000,SYNTHETIC\n", encoding="utf-8")
        (data_dir / "stock_names.json").write_text('{"600000":"SYNTHETIC"}', encoding="utf-8")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {key: value for key, value in os.environ.items() if not key.startswith("KLINE_TRAINER_")}
        env["PYTHONIOENCODING"] = "utf-8"
        base = f"http://127.0.0.1:{port}"
        command = [sys.executable, str(ROOT / "run_dev.py"), "--data-dir", str(data_dir),
                   "--users-dir", str(users_dir), "--port", str(port)]
        process = None
        session = requests.Session()
        session.trust_env = False
        log_path = root / "http.log"

        def api(method, path, body=None, expected=200):
            response = session.request(method, base + path, json=body, timeout=20)
            assert response.status_code == expected, (path, response.status_code, response.text)
            return response.json()

        def start(log, *, demo=False):
            result = subprocess.Popen(command + (["--portfolio-demo"] if demo else []),
                                      cwd=temp, env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    if result.poll() is not None:
                        raise RuntimeError(log_path.read_text(encoding="utf-8", errors="replace"))
                    try:
                        api("GET", "/api/health")
                        return result
                    except requests.ConnectionError:
                        time.sleep(0.15)
                raise RuntimeError("HTTP server startup timeout")
            except BaseException:
                result.kill()
                result.wait(timeout=15)
                raise

        with log_path.open("w", encoding="utf-8") as log:
            try:
                process = start(log, demo=True)
                api("POST", "/api/users", {"username": "step4_check"})
                api("POST", "/api/portfolio/session", {"user": "step4_check"})
                create = {"request_id": "create", "stocks": ["600000", "000001", "600036"],
                          "start_date": "2024-01-02", "end_date": "2024-01-10",
                          "initial_cash": "100000", "data_source": "synthetic-demo"}
                state = api("POST", "/api/portfolio/trainings", create, expected=201)
                prefix = "/api/portfolio/trainings/" + state["training_id"]
                api("GET", "/api/training/" + state["training_id"] + "/full_data", expected=404)

                def mutate(action, key, **params):
                    nonlocal state
                    body = {"request_id": key, "expected_version": state["version"], **params}
                    state = api("POST", prefix + "/" + action, body)
                    assert api("POST", prefix + "/" + action, body) == state
                    return body

                mutate("orders", "buy", stock="600000", side="buy", quantity=200)
                mutate("advance", "day2", expected_date=state["current_date"])
                mutate("orders", "sell", stock="600000", side="sell", quantity=100)
                mutate("advance", "day3", expected_date=state["current_date"])
                assert state["account"]["cash"] == "98979.44"
                assert state["daily_assets"][-1]["total_assets"] == "100039.44"
                mutate("orders", "second-stock", stock="000001", side="buy", quantity=100)
                mutate("advance", "day4", expected_date=state["current_date"])
                pending_request = mutate("orders", "pending", stock="600036", side="buy", quantity=100)
                saved = state
                assert saved["orders"][-1]["status"] == "pending"
                assert len(saved["fills"]) == 3
                assert saved["account"]["cash"] == "98134.43"
                assert saved["daily_assets"][-1]["total_assets"] == "100064.43"
                api("GET", prefix + "/report", expected=409)
                api("GET", prefix + "/history/600000?through=2024-01-10", expected=400)
                assert "9999" not in json.dumps(saved)
                process.kill()  # hard process stop; no graceful state flush
                process.wait(timeout=15)
                process = None

                # Independent child dies INSIDE a transaction, after the engine
                # and HTTP retry record were written, before SQLite COMMIT.
                crash_code = """
import os, sys
from backend.portfolio_repository import PortfolioRepository
repo = PortfolioRepository(sys.argv[1])
owner = repo._connection.execute('SELECT owner FROM portfolio_training').fetchone()[0]
repo._before_transaction_commit = lambda: os._exit(73)
repo.mutate(owner, sys.argv[2], 'advance', {
    'request_id': 'interrupted', 'expected_version': int(sys.argv[3]), 'expected_date': sys.argv[4]})
"""
                crash = subprocess.run([sys.executable, "-c", crash_code, str(users_dir / "portfolio.sqlite3"),
                    saved["training_id"], str(saved["version"]), saved["current_date"]], cwd=ROOT, env=env,
                    capture_output=True, text=True, timeout=20)
                assert crash.returncode == 73, (crash.returncode, crash.stdout, crash.stderr)
                process = start(log, demo=False)
                restored = api("POST", prefix + "/restore", {})
                assert restored == saved, "hard restart or interrupted advance changed aggregate"
                assert api("POST", prefix + "/orders", pending_request) == saved
                assert api("POST", "/api/portfolio/trainings", create, expected=201)["version"] == 0
                assert len(api("GET", "/api/portfolio/trainings")["trainings"]) == 1
                mutate("advance", "interrupted", expected_date=state["current_date"])
                assert len(state["fills"]) == 4
                assert state["current_date"] == "2024-01-08"
                mutate("end", "end")
                report = api("GET", prefix + "/report")
                assert "9999" in json.dumps(report["history"])
                assert report == state["report"]
                ended = api("GET", prefix)
                process.kill()
                process.wait(timeout=15)
                process = start(log)
                assert api("POST", prefix + "/restore", {}) == ended
                assert api("GET", prefix + "/report") == report
                print(json.dumps({"status": "passed", "synthetic": True,
                    "description": "真实本机 HTTP；合成行情；重启后关闭合成创建入口",
                    "saved_date": saved["current_date"], "saved_cash": saved["account"]["cash"],
                    "saved_assets": saved["daily_assets"][-1]["total_assets"],
                    "saved_fills": len(saved["fills"]), "pending_orders": 1,
                    "restored_exactly": restored == saved, "crash_exit_code": crash.returncode,
                    "final_date": ended["current_date"], "report_assets": report["summary"]["final_assets"],
                    "checks": ["HTTP部分卖出与两股持仓", "原单股完整行情入口隔离", "重复请求原结果",
                        "硬重启逐字段一致", "事务中断无半笔交易", "中断请求重试一次成交",
                        "结束前未来隔离", "结束后完整报告", "结束报告重启不变"]}, ensure_ascii=False, indent=2))
            finally:
                session.close()
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=15)


if __name__ == "__main__":
    main()
