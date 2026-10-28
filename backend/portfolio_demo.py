"""Deterministic multi-month UI practice data. Never real market history."""

from datetime import date, timedelta
from decimal import Decimal
import math

from backend.portfolio_replay import PortfolioReplay
from backend.replay_calendar import ReplayCalendar
from backend.replay_data import QuoteRepository
from backend.replay_models import ActionHistory, Evidence, SecurityHistory


START, END = "2024-01-02", "2024-06-28"
NAMES = {"600000": "练习甲", "000001": "练习乙", "600036": "练习丙"}


class PracticeSource:
    name = "synthetic-practice-not-real-history"
    version = "practice-v1"
    synthetic = True

    def fetch(self, code, start, end):
        index = list(NAMES).index(code)
        rows, current, i, previous = [], date.fromisoformat(START), 0, None
        while current.isoformat() <= END:
            if current.weekday() < 5:
                close = Decimal(str(12 + index * 7 + i * (0.025 - index * 0.008)
                                    + math.sin(i / (4 + index) + index) * (0.6 + index * 0.2))).quantize(Decimal(".01"))
                opening = previous if previous is not None else close - Decimal(".08")
                if start <= current.isoformat() <= end:
                    rows.append({"date": str(current), "open": str(opening),
                        "close": str(close), "high": str(max(opening, close) + Decimal(".15")),
                        "low": str(min(opening, close) - Decimal(".15")), "volume": str(100000 + i * 1200 + index * 35000)})
                i += 1
                previous = close
            current += timedelta(days=1)
        return rows


def practice_replay(data_dir, params):
    evidence = Evidence(START, END, "人工生成练习样本（日期也不代表真实交易日历）", "practice-v1",
                        "portfolio_demo.py", "2024-07-01T00:00:00+00:00", "synthetic")
    dates, current = [], date.fromisoformat(START)
    while str(current) <= END:
        if current.weekday() < 5:
            dates.append(str(current))
        current += timedelta(days=1)
    calendar = ReplayCalendar(tuple(dates), evidence)
    securities = {code: SecurityHistory(code, "2000-01-01", evidence) for code in NAMES}
    actions = {code: ActionHistory(code, evidence) for code in NAMES}
    return PortfolioReplay(params["stocks"], params["start_date"], params["end_date"], calendar,
                           QuoteRepository(data_dir, PracticeSource()), securities, actions,
                           synthetic=True, accounting=True)
