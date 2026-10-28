"""Step 3 acceptance demo: isolated synthetic portfolio, no live trading/API."""

import json
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.portfolio_engine import PortfolioEngine
from backend.portfolio_ledger_store import PortfolioLedgerStore
from backend.portfolio_replay import PortfolioReplay
from tests.replay_fixture import fixture


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="kline-step3-") as temp:
        data, calendar, securities, actions, source, quotes = fixture(temp)
        replay = PortfolioReplay(list(securities), data["start"], data["end"], calendar,
                                 quotes, securities, actions, synthetic=True, accounting=True)
        store = PortfolioLedgerStore(Path(temp) / "ledger.sqlite3")
        engine = PortfolioEngine(replay, store=store)
        try:
            first = engine.submit("600000", "buy", 200, request_id=str(uuid4()))
            initial = engine.snapshot()
            engine.switch_stock("600036")
            assert engine.snapshot() == initial
            assert initial["account"]["frozen_cash"] == "2249.02"
            op = str(uuid4())
            bought = engine.advance(operation_id=op, expected_date="2024-01-02")
            assert bought == engine.advance(operation_id=op, expected_date="2024-01-02")
            assert len(engine.snapshot()["fills"]) == 1
            engine.submit("600000", "sell", 100, request_id=str(uuid4()))
            sold = engine.advance(expected_date="2024-01-03")
            assert sold["account"]["cash"] == "98979.44"
            assert sold["daily_assets"][-1]["total_assets"] == "100039.44"
            # A second stock shares the SAME cash account.
            second = engine.submit("000001", "buy", 100, request_id=str(uuid4()))
            shared = engine.snapshot()
            assert shared["account"]["frozen_cash"] == "929.01"
            engine.advance(expected_date="2024-01-04")
            positions = {p["stock"]: p for p in engine.snapshot()["positions"]}
            assert positions["600000"]["quantity"] == positions["000001"]["quantity"] == 100
            book = engine.reconcile()
            persisted = store.read(engine.training_id)
            assert persisted["account"]["cash"] == book["cash"]
            assert len(persisted["fills"]) == 3
            assert "9999" not in json.dumps(engine.snapshot())
            print(json.dumps({
                "status": "passed", "synthetic": True,
                "description": "阶段三人工合成引擎验收；HTTP与恢复另见 verify_portfolio_api.py；页面在阶段五",
                "first_order_frozen_cash": first["frozen_cash"],
                "next_open_fill_price": bought["fills"][0]["price"],
                "cash_after_partial_sell": sold["account"]["cash"],
                "remaining_cost_after_partial_sell": sold["positions"][0]["cost"],
                "realized_pnl_after_partial_sell": sold["account"]["realized_pnl"],
                "assets_after_partial_sell": sold["daily_assets"][-1]["total_assets"],
                "second_stock_reservation": second["frozen_cash"],
                "final_date": str(engine.current_date),
                "final_shared_cash": book["cash"],
                "held_stocks": {code: p["quantity"] for code, p in positions.items()},
                "checks": ["共享现金", "切股日期不变", "下一开盘成交", "重复推进去重",
                           "T+1", "部分卖出及成本结转", "账本重算", "SQLite一致提交", "未来数据隔离"],
            }, ensure_ascii=False, indent=2))
        finally:
            engine.close()
            store.close()


if __name__ == "__main__":
    main()
