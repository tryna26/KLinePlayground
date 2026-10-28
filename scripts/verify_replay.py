"""Step 2 acceptance: synthetic replay, optionally real calendar/Tencent ingestion.

All storage is temporary. No real prices or user data are written to the repo.
"""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.portfolio_replay import PortfolioReplay
from backend.replay_calendar import load_public_calendar
from backend.replay_data import QuoteRepository
from backend.replay_models import ReplayDataError
from tests.replay_fixture import fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="kline-step2-") as temp:
        data, calendar, securities, actions, source, quotes = fixture(temp)
        session = PortfolioReplay(list(securities), data["start"], data["end"], calendar,
                                  quotes, securities, actions, synthetic=True)
        initial = session.snapshot()
        session.switch_stock("600036")
        assert session.snapshot()["current_date"] == initial["current_date"]
        snapshot = session.advance()
        states = {item["stock"]: item["status"] for item in snapshot["stocks"]}
        assert states == {"600000": "trading", "000001": "suspended", "600036": "not_listed"}
        visible = [session.chart_data(code) for code in session.stocks]
        assert "9999" not in json.dumps(visible)
        try:
            session.full_data("600000")
        except ReplayDataError:
            pass
        else:
            raise AssertionError("future data was exposed")
        summary = {"synthetic": {"three_stock_date": snapshot["current_date"], "states": states,
                                  "future_data_blocked": True, "switch_preserves_date": True}}
        if args.online:
            public = load_public_calendar(temp, refresh=True)
            public.require_range("2024-01-02", "2024-01-10")
            expected = public.between("2024-01-02", "2024-01-10")
            assert [str(value) for value in expected] == data["calendar"]
            real = QuoteRepository(temp)
            summaries = []
            for code in ["600000", "000001", "600036"]:
                bundle = real.prime(code, "2024-01-02", "2024-01-10")
                assert bundle is not None and bundle.bars, f"{code}: online data unavailable"
                assert all(public.is_open(bar.date) for bar in bundle.bars)
                assert [bar.date for bar in bundle.bars] == list(expected)
                summaries.append({"stock": code, "rows": len(bundle.bars), "first": str(bundle.bars[0].date),
                                  "last": str(bundle.bars[-1].date), "source": bundle.source,
                                  "data_version": bundle.data_version})
            # Valid real prices are insufficient: no verified action/lifecycle
            # coverage was imported, so a real training must still be rejected.
            try:
                PortfolioReplay(["600000"], "2024-01-02", "2024-01-10", public, real, {}, {})
            except ReplayDataError:
                pass
            else:
                raise AssertionError("unverified real interval was enabled")
            # Independently prove the corporate-action gate using trusted,
            # reviewed-format lifecycle evidence, with NO action evidence.
            lifecycle = replace(securities["600000"], evidence=replace(securities["600000"].evidence, scope="reviewed"))
            # This lifecycle is test input, not a claim of real verification.
            try:
                PortfolioReplay(["600000"], "2024-01-02", "2024-01-10", public, real,
                                {"600000": lifecycle}, {})
            except ReplayDataError as exc:
                assert "公司行为" in str(exc)
            else:
                raise AssertionError("missing action coverage was accepted")
            summary["online"] = {"calendar_first": str(public.evidence.start), "calendar_last": str(public.evidence.end),
                                  "calendar_source": public.evidence.source, "calendar_version": public.evidence.version,
                                  "stocks": summaries, "unverified_real_training_blocked": True}
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
