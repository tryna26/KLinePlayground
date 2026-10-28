"""Date-bounded data facade for portfolio charts, indicators and the engine.

Never register these sessions
in the legacy active_trainings map, whose full_data route has no replay guard.
"""

from copy import copy
from types import MappingProxyType

from backend.replay_models import (
    ActionHistory, CorporateAction, DailyBar, DataStatus, Evidence, QuoteState,
    ReplayDataError, SecurityHistory, day, stock_code,
)
from backend.portfolio_models import serialize
from backend.replay_calendar import ReplayCalendar


class PortfolioReplay:
    def __init__(self, stocks, start, end, calendar, quotes, securities, actions, *, synthetic=False,
                 accounting=False):
        self.__stocks = tuple(stock_code(code) for code in stocks)
        if not self.__stocks or len(set(self.__stocks)) != len(self.__stocks):
            raise ReplayDataError("股票池不能为空或包含重复代码")
        start, end = day(start), day(end)
        calendar.require_range(start, end, synthetic=synthetic)
        if not calendar.is_open(start) or not calendar.is_open(end):
            raise ReplayDataError("训练起止日期必须是公共交易日")
        if quotes.source.synthetic and not synthetic:
            raise ReplayDataError("合成行情不能用于真实历史训练")
        for code in self.__stocks:
            security = securities.get(code)
            if security is None or security.stock != code or not security.evidence.covers(start, end, synthetic=synthetic):
                raise ReplayDataError(f"{code}：历史上市、停牌及特殊状态覆盖未核实")
            if any(a <= end and b >= start for a, b, _ in security.special_periods):
                raise ReplayDataError(f"{code}：区间包含不支持的特殊交易状态")
            action = actions.get(code)
            if action is None or action.stock != code:
                raise ReplayDataError(f"{code}：缺少公司行为完整性证明，不能创建该训练")
            action.require_supported(start, end, synthetic=synthetic, accounting=accounting)
            if accounting:
                for event in action.events:
                    if start <= event.effective_on <= end:
                        if not calendar.is_open(event.record_on) or not calendar.is_open(event.effective_on):
                            raise ReplayDataError("公司行为登记日及生效日必须是公共交易日")
        self.__calendar = calendar
        self.__securities = MappingProxyType({code: securities[code] for code in self.__stocks})
        self.__actions = MappingProxyType({code: actions[code] for code in self.__stocks})
        dates = calendar.between(start, end)
        states = {}
        # Preload internally, then pin immutable daily states for this run. Cache
        # refreshes cannot revise an ongoing training's historical observations.
        for code in self.__stocks:
            bundle = quotes.prime(code, start, end)
            if bundle is not None and any(not calendar.is_open(bar.date) for bar in bundle.bars):
                raise ReplayDataError(f"{code}：行情包含公共日历休市日，数据版本被拒绝")
            for value in dates:
                state = quotes.quote(code, value, calendar, securities[code], synthetic=synthetic)
                states[(code, value)] = state
        self.__states = MappingProxyType(states)
        self.__dates = dates
        self.__index = 0
        self.__synthetic = synthetic
        self.__selected = self.__stocks[0]
        self.__require_date(start)

    @property
    def current_date(self):
        return self.__dates[self.__index]

    @property
    def stocks(self):
        return self.__stocks

    def __require_date(self, value):
        for code in self.__stocks:
            state = self.__states[(code, value)]
            if state.status not in {DataStatus.TRADING, DataStatus.SUSPENDED, DataStatus.NOT_LISTED,
                                    DataStatus.DELISTED, DataStatus.NO_TRADES}:
                raise ReplayDataError(f"{code}：{state.reason}")

    def __require_visible(self, code, through=None):
        if code not in self.__stocks:
            raise ReplayDataError("股票不在本次训练股票池中")
        through = day(through) if through is not None else self.current_date
        if through > self.current_date:
            raise ReplayDataError("训练尚未推进至该日期，禁止读取未来行情")
        return through

    def switch_stock(self, code):
        self.__require_visible(code)
        self.__selected = code
        return self.snapshot()

    def advance(self):
        """Data-only cursor; stage 3 must coordinate with the ledger transaction."""
        if self.__index + 1 == len(self.__dates):
            raise ReplayDataError("已到达本次训练区间末日")
        target = self.__dates[self.__index + 1]
        self.__require_date(target)  # fail BEFORE mutating the common cursor
        self.__index += 1
        return self.snapshot()

    # Internal engine adapter. It forks only the cursor: immutable pinned data
    # stay shared. Public chart/account methods retain their visibility guards.
    # The engine owns its cursor and never exposes a mutable replay to callers.
    def _engine_fork(self):
        return copy(self)

    def _engine_next_date(self):
        if self.__index + 1 == len(self.__dates):
            raise ReplayDataError("已到达本次训练区间末日")
        return self.__dates[self.__index + 1]

    def _engine_end_date(self):
        return self.__dates[-1]

    def _engine_seek(self, target, *, validate=True):
        target = day(target)
        if target not in self.__dates:
            raise ReplayDataError("恢复日期不在固定训练区间中")
        if validate:
            for value in self.__dates:
                if value <= target:
                    self.__require_date(value)
        self.__index = self.__dates.index(target)

    def _engine_export(self):
        """Backend persistence only. Contains future data; never an HTTP result."""
        return serialize({"schema_version": 1, "stocks": self.__stocks,
            "start": self.__dates[0], "end": self.__dates[-1], "synthetic": self.__synthetic,
            "calendar": self.__calendar,
            "securities": dict(self.__securities), "actions": dict(self.__actions),
            "states": [serialize(state) for state in self.__states.values()]})

    @classmethod
    def _engine_import(cls, payload):
        """Restore the pinned observations, without network or mutable caches."""
        try:
            if payload["schema_version"] != 1 or type(payload["synthetic"]) is not bool:
                raise ValueError("unsupported replay schema")
            calendar = ReplayCalendar(tuple(payload["calendar"]["dates"]), Evidence(**payload["calendar"]["evidence"]))
            securities = {code: SecurityHistory(**{**raw, "evidence": Evidence(**raw["evidence"])})
                          for code, raw in payload["securities"].items()}
            actions = {code: ActionHistory(code, Evidence(**raw["evidence"]),
                        tuple(CorporateAction(**item) for item in raw["events"]))
                       for code, raw in payload["actions"].items()}
            states = {}
            for raw in payload["states"]:
                fields = dict(raw)
                fields["date"], fields["status"] = day(raw["date"]), DataStatus(raw["status"])
                fields["bar"] = DailyBar(**raw["bar"]) if raw["bar"] is not None else None
                state = QuoteState(**fields)
                key = (state.stock, state.date)
                if key in states or (state.bar and (state.bar.stock, state.bar.date) != key):
                    raise ValueError("duplicate or mismatched observations")
                states[key] = state

            class PinnedQuotes:
                source = type("Source", (), {"synthetic": payload["synthetic"]})()

                def prime(self, code, start, end):
                    return None

                def quote(self, code, value, *args, **kwargs):
                    return states[(code, value)]

            result = cls(payload["stocks"], payload["start"], payload["end"], calendar,
                         PinnedQuotes(), securities, actions, synthetic=payload["synthetic"], accounting=True)
            if len(states) != len(result.__stocks) * len(result.__dates):
                raise ValueError("unexpected observations")
            return result
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ReplayDataError("固定行情快照损坏或结构版本不受支持") from exc

    def _engine_context(self):
        return self.__calendar, self.__securities, self.__actions

    def _engine_state(self, code):
        self.__require_visible(code)
        return self.__states[(code, self.current_date)]

    def history(self, code, *, through=None, adjustment="none"):
        through = self.__require_visible(code, through)
        if adjustment != "none":
            raise ReplayDataError("组合训练暂仅支持未复权图表；未采用全量末日复权因子")
        return [self.__states[(code, value)].bar.to_dict() for value in self.__dates
                if value <= through and self.__states[(code, value)].bar is not None]

    def chart_data(self, code):
        return self.history(code)

    def indicator_data(self, code):
        # Downstream indicator algorithms receive only visible bars.
        return self.history(code)

    def account_data(self):
        return self.snapshot()["stocks"]

    def full_data(self, code):
        self.__require_visible(code)
        raise ReplayDataError("组合训练未提供完整未来行情读取入口")

    def snapshot(self):
        result = []
        for code in self.__stocks:
            state = self.__states[(code, self.current_date)]
            # Last price is valuation only, carries its actual date, never an
            # executable bar for suspended/delisted/not-yet-listed securities.
            visible = self.history(code)
            last = visible[-1] if visible else None
            result.append({**state.to_dict(), "valuation": None if last is None else {
                "price": last["close"], "price_date": last["date"],
                "stale": last["date"] != self.current_date.isoformat(),
            }})
        return {"current_date": self.current_date.isoformat(), "selected_stock": self.__selected,
                "synthetic": self.__synthetic, "stocks": result}
