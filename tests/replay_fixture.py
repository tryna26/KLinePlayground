"""Fixture loading helpers; not part of the production data interface."""

from copy import deepcopy
import json
from pathlib import Path

from backend.portfolio_replay import PortfolioReplay
from backend.replay_calendar import ReplayCalendar
from backend.replay_data import QuoteRepository
from backend.replay_models import ActionHistory, Evidence, SecurityHistory, day


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "portfolio_replay_v1.json"


class SyntheticSource:
    name = "synthetic-acceptance-fixture"
    version = "portfolio-replay-v1"
    synthetic = True

    def __init__(self, bars):
        self.bars = deepcopy(bars)
        self.calls = []
        self.fail = False

    def fetch(self, code, start, end):
        self.calls.append((code, start, end))
        if self.fail:
            raise OSError("synthetic source failure")
        return deepcopy([row for row in self.bars.get(code, []) if start <= row["date"] <= end])


def fixture(cache_dir):
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert data["synthetic"] is True
    evidence = Evidence(data["start"], data["end"], "synthetic fixture", data["schema_version"],
                        str(FIXTURE_PATH.name), "2024-01-11T00:00:00+00:00", "synthetic")
    calendar = ReplayCalendar(tuple(data["calendar"]), evidence)
    securities = {code: SecurityHistory(code, evidence=evidence, **fields)
                  for code, fields in data["securities"].items()}
    actions = {code: ActionHistory(code, evidence) for code in securities}
    source = SyntheticSource(data["bars"])
    quotes = QuoteRepository(cache_dir, source)
    return data, calendar, securities, actions, source, quotes


def replay(cache_dir, **overrides):
    data, calendar, securities, actions, source, quotes = fixture(cache_dir)
    args = dict(stocks=list(securities), start=data["start"], end=data["end"], calendar=calendar,
                quotes=quotes, securities=securities, actions=actions, synthetic=True)
    args.update(overrides)
    return PortfolioReplay(**args)
