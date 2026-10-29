"""Free-selection portfolios using the same raw daily data as single-stock practice.

This is a simplified exercise, not a reviewed corporate-action backtest. Gaps
remain unknown/non-executable. Snapshots pin all observations for restart/replay.
"""

from datetime import date
import random

from backend.portfolio_replay import PortfolioReplay
from backend.replay_calendar import load_public_calendar
from backend.replay_models import (ActionHistory, DataStatus, Evidence, QuoteState,
                                   ReplayDataError, SecurityHistory, stock_code,
                                   number, utc_now, validate_bars)
from backend.replay_storage import content_hash
from backend.tencent_source import TencentSourceUnavailable


class QuoteSourceUnavailable(ReplayDataError):
    """A source-wide outage must not trigger another stock download loop."""


def load_rows(manager, code, source, start, end):
    try:
        frame = manager.get_stock_data_range(code, start, end, source=source)
    except TencentSourceUnavailable as exc:
        raise QuoteSourceUnavailable("腾讯行情的两个官方地址均暂不可用，已停止本次下载；请稍后重试或切换数据源") from exc
    except Exception as exc:
        raise ReplayDataError(f"{code}：行情加载失败，请稍后重试或切换数据源（{type(exc).__name__}）") from exc
    if frame is None or frame.empty:
        raise ReplayDataError(f"{code}：该数据源暂无行情，请切换数据源或补充离线数据")
    rows = []
    for row in frame.to_dict("records"):
        rows.append({"date": str(row["date"])[:10],
                     **{key: str(row[key]) for key in ("open", "high", "low", "close")},
                     "volume": str(number(row["volume"]) * (1 if source == "xtdata" else 100))})
    return validate_bars(code, rows, min(row["date"] for row in rows), max(row["date"] for row in rows))


def build_practice(data_dir, manager, params):
    calendar = load_public_calendar(data_dir)
    dates = calendar.between(params["start_date"], params["end_date"])
    # Today's incomplete daily candle is never a historical execution price.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    dates = tuple(value for value in dates if value < today)
    if len(dates) < 2:
        raise ReplayDataError("所选日期范围内至少需要两个已结束的交易日")
    random_mode = params.get("mode") == "random"
    if params["end_date"] >= str(today) and not random_mode:
        raise ReplayDataError("历史训练的结束日期须早于今天，请选择已结束的行情日期")
    source = params["data_source"]
    observations = {}
    if random_mode:
        codes = (manager._get_offline_stock_codes() if source == "offline"
                 else list(manager.stock_names))
        if not codes and source != "offline":
            manager.load_stock_list()
            codes = list(manager.stock_names)
        codes = manager._filter_stock_codes_by_sector(codes, params.get("sector", "all"))
        candidates = []
        for code in codes:
            try:
                candidates.append(stock_code(code))
            except ReplayDataError:
                continue
        random.shuffle(candidates)
        count, length = params["stock_count"], params["training_days"]
        common = dates
        for code in candidates[:max(30, count * 5)]:
            try:
                bars = load_rows(manager, code, source, str(dates[0]), str(dates[-1]))
            except QuoteSourceUnavailable:
                raise
            except (ReplayDataError, ValueError):
                continue
            available = tuple(value for value in common if bars[0].date <= value <= bars[-1].date)
            if len(available) < length:
                continue
            observations[code], common = bars, available
            if len(observations) == count:
                break
        if len(observations) != count:
            raise ReplayDataError("没有足够的股票覆盖本次盲盒时长，请扩大日期范围、减少股票数量或缩短训练天数")
        offset = random.randrange(len(common) - length + 1)
        dates = common[offset:offset + length]
    else:
        for code in params["stocks"]:
            bars = load_rows(manager, code, source, str(dates[0]), str(dates[-1]))
            if bars[0].date > dates[0] or bars[-1].date < dates[-1]:
                raise ReplayDataError(f"{code}：所选日期超出可用行情范围（{bars[0].date} 至 {bars[-1].date}）")
            observations[code] = bars

    start, end = dates[0], dates[-1]
    evidence = Evidence(start, end, f"single-stock/{source}", "free-selection-v1",
                        "日线简化练习；未独立核验历史特殊状态，不计分红送转", utc_now(), scope="practice")
    securities = {code: SecurityHistory(code, date(1990, 1, 1), evidence) for code in observations}
    actions = {code: ActionHistory(code, evidence) for code in observations}
    states = {}
    for code, bars in observations.items():
        visible = {bar.date: bar for bar in bars if start <= bar.date <= end}
        if not visible:
            raise ReplayDataError(f"{code}：所选区间没有有效行情")
        version = content_hash([bar.to_dict() for bar in visible.values()])
        for value in dates:
            bar = visible.get(value)
            status = DataStatus.UNKNOWN if bar is None else DataStatus.TRADING if bar.volume else DataStatus.NO_TRADES
            states[(code, value)] = QuoteState(code, value, status,
                "所选数据源的历史日线" if bar else "该日缺少行情，不执行订单；估值沿用最近有效收盘价",
                bar, source, evidence.checked_at, version, "日线简化练习", evidence.version)

    class PinnedQuotes:
        source = type("Source", (), {"synthetic": False})()

        def prime(self, *args):
            return None

        def quote(self, code, value, *args, **kwargs):
            return states[(code, value)]

    return PortfolioReplay(list(observations), start, end, calendar, PinnedQuotes(), securities,
                           actions, accounting=True, practice=True)
