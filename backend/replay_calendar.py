"""Public Shanghai/Shenzhen trading dates, independent of stock price coverage."""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

from backend.replay_models import Evidence, ReplayDataError, day, utc_now
from backend.replay_storage import content_hash, read_cache, write_cache


CALENDAR_URL = "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"


@dataclass(frozen=True)
class ReplayCalendar:
    dates: tuple[date, ...]
    evidence: Evidence

    def __post_init__(self):
        dates = tuple(sorted({day(value) for value in self.dates}))
        if not dates or dates[0] < self.evidence.start or dates[-1] > self.evidence.end:
            raise ReplayDataError("公共交易日历为空或超出声明范围")
        object.__setattr__(self, "dates", dates)

    def require_range(self, start, end, *, synthetic=False):
        if not self.evidence.covers(start, end, synthetic=synthetic):
            raise ReplayDataError("公共交易日历未覆盖该区间，请刷新日历或缩小训练范围")

    def is_open(self, value):
        value = day(value)
        if not self.evidence.start <= value <= self.evidence.end:
            raise ReplayDataError("日期超出公共交易日历覆盖范围")
        index = bisect_right(self.dates, value)
        return index > 0 and self.dates[index - 1] == value

    def next_day(self, value):
        value = day(value)
        if not self.evidence.start <= value <= self.evidence.end:
            raise ReplayDataError("日期超出公共交易日历覆盖范围")
        index = bisect_right(self.dates, value)
        if index == len(self.dates):
            raise ReplayDataError("公共交易日历没有下一个交易日，请刷新或结束训练")
        return self.dates[index]

    def between(self, start, end):
        start, end = day(start), day(end)
        self.require_range(start, end, synthetic=True)
        return tuple(value for value in self.dates if start <= value <= end)


def load_public_calendar(cache_dir, *, refresh=False):
    """Explicit refresh, otherwise pinned cache; no weekday/stock intersection fallback.

    Use the same Sina decoder as AKShare, with a bounded, local HTTP session.
    Conservative coverage ends at the actual last returned trading date.
    """
    path = Path(cache_dir) / "portfolio_replay" / "calendar.json"
    if path.exists() and not refresh:
        try:
            payload = read_cache(path)
            if payload["provider"] != "sina-public-calendar-v1":
                raise ReplayDataError("公共交易日历来源版本不匹配")
            evidence = Evidence(**payload["evidence"])
            return ReplayCalendar(tuple(payload["dates"]), evidence)
        except (KeyError, TypeError, ReplayDataError) as exc:
            raise ReplayDataError("公共交易日历缓存无效，请显式刷新日历") from exc

    try:
        from akshare.stock.cons import hk_js_decode
        from backend.local_js import call_local_script

        with requests.Session() as session:
            session.trust_env = False
            response = session.get(CALENDAR_URL, timeout=(6, 15))
            response.raise_for_status()
        encoded = response.text.split("=", 1)[1].split(";", 1)[0].replace('"', "")
        values = call_local_script(hk_js_decode, "d", encoded, timeout=5000)
        dates = sorted({day(value[:10]) for value in values} | {date(1992, 5, 4)})
        if len(dates) < 1000 or dates[0] != date(1990, 12, 19):
            raise ReplayDataError("公共交易日历结果不完整")
        evidence = Evidence(dates[0], dates[-1], CALENDAR_URL, content_hash([d.isoformat() for d in dates]),
                            "Sina calendar, AKShare decoder; 1992-05-04 correction", utc_now())
        calendar = ReplayCalendar(tuple(dates), evidence)
        write_cache(path, {"provider": "sina-public-calendar-v1", "dates": [d.isoformat() for d in dates],
                           "evidence": {**evidence.__dict__, "start": evidence.start.isoformat(), "end": evidence.end.isoformat()}})
        return calendar
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("Public calendar acquisition failed")
        raise ReplayDataError("公共交易日历获取失败，未使用工作日或个股日期替代；请稍后重试") from exc
