"""Versioned main-board daily simulation rules; no UI or legacy-account state."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from backend.replay_models import ReplayDataError, day, number, stock_code


CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value):
    return number(value).quantize(CENT, rounding=ROUND_HALF_UP)


def quantity(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReplayDataError("数量必须是正整数股数")
    return value


@dataclass(frozen=True)
class FeePeriod:
    start: date
    end: date
    version: str
    stamp_sell: Decimal
    transfer: Decimal
    commission: Decimal = Decimal("0.0003")
    minimum: Decimal = Decimal("5.00")

    def __post_init__(self):
        object.__setattr__(self, "start", day(self.start))
        object.__setattr__(self, "end", day(self.end))
        for field in ("stamp_sell", "transfer", "commission", "minimum"):
            value = number(getattr(self, field))
            if value < 0:
                raise ReplayDataError("费率及最低佣金不能为负数")
            object.__setattr__(self, field, value)
        if self.start > self.end or not self.version:
            raise ReplayDataError("费用规则版本或日期无效")


class FeeSchedule:
    """Commission includes handling/regulatory charges; transfer is separate.

    Broker commission is a configurable simulation assumption. Dates outside
    the reviewed tax/transfer range fail closed, rather than extrapolating.
    """

    def __init__(self, periods=None):
        self.periods = tuple(sorted(periods if periods is not None else (
            FeePeriod("2022-04-29", "2023-08-27", "cn-a-20220429-v1", "0.001", "0.00001"),
            FeePeriod("2023-08-28", "2026-10-27", "cn-a-20230828-v1", "0.0005", "0.00001"),
        ), key=lambda p: p.start))
        if not self.periods or any(a.end >= b.start for a, b in zip(self.periods, self.periods[1:])):
            raise ReplayDataError("费用规则为空或覆盖日期重叠")

    def for_date(self, value):
        value = day(value)
        for period in self.periods:
            if period.start <= value <= period.end:
                return period
        raise ReplayDataError("该日期缺少已核验费用规则")

    def calculate(self, value, side, gross):
        if side not in {"buy", "sell"}:
            raise ReplayDataError("买卖方向无效")
        rule, gross = self.for_date(value), money(gross)
        if gross <= 0:
            raise ReplayDataError("成交金额必须大于零")
        result = {
            "commission": money(max(gross * rule.commission, rule.minimum)),
            "transfer": money(gross * rule.transfer),
            "stamp": money(gross * rule.stamp_sell) if side == "sell" else ZERO,
        }
        return {**result, "total": sum(result.values(), ZERO), "version": rule.version}


@dataclass(frozen=True)
class MarketRules:
    version: str = "cn-mainboard-daily-v1"
    buy_unit: int = 100
    max_quantity: int = 1000000
    tick: Decimal = CENT
    limit: Decimal = Decimal("0.10")
    reserve_buffer: Decimal = Decimal("0.10")
    practice: bool = False

    def __post_init__(self):
        for field in ("tick", "limit", "reserve_buffer"):
            object.__setattr__(self, field, number(getattr(self, field)))
        if (not self.version or type(self.buy_unit) is not int or self.buy_unit <= 0
                or type(self.max_quantity) is not int or self.max_quantity < self.buy_unit
                or self.tick != CENT or not 0 < self.limit < 1 or self.reserve_buffer < 0):
            raise ReplayDataError("市场规则配置无效（当前价格单位仅支持分）")

    def validate_quantity(self, side, qty, available):
        quantity(qty)
        if qty > self.max_quantity:
            raise ReplayDataError("超过单笔最大申报数量")
        if side == "buy" and qty % self.buy_unit:
            raise ReplayDataError(f"买入数量必须是 {self.buy_unit} 股的整数倍")
        if side == "sell":
            if qty > available:
                raise ReplayDataError("可卖股数不足，包含 T+1 或已冻结股数限制")
            if qty % self.buy_unit and qty % self.buy_unit != available % self.buy_unit:
                raise ReplayDataError("不足一手的零股必须一次全部卖出，不得拆分零股")

    def execution_reason(self, state, previous, side, security, calendar, reference=None):
        if not state.execution_data_ready or state.bar.volume <= 0:
            return state.reason or "无有效成交，不能成交"
        bar = state.bar
        if any(value != money(value) for value in (bar.open, bar.high, bar.low, bar.close)):
            return "行情价格不符合 0.01 元报价单位"
        stock_code(state.stock)
        if self.practice:
            # Free-selection practice uses observed bars, without claiming an
            # independently reviewed ST/IPO/ex-right history. No missing-bar fills.
            if bar.high == bar.low:
                return "一字行情，日线练习保守拒绝成交"
            return None
        # Registration-system IPOs have five unrestricted public sessions.
        # Older IPO first sessions have extra pricing rules, outside v1 scope.
        unrestricted = False
        if (security.listed_on < calendar.evidence.start
                and (state.date - security.listed_on).days <= 30):
            return "公共日历未覆盖近期上市日，不能确定上市初期交易规则"
        if security.listed_on >= calendar.evidence.start:
            sessions = calendar.between(security.listed_on, state.date)
            if security.listed_on >= date(2023, 4, 10):
                unrestricted = len(sessions) <= 5
            elif len(sessions) == 1:
                return "旧制上市首日价格规则尚未支持"
        if unrestricted:
            if bar.high == bar.low:
                return "无涨跌幅限制日的一字行情，保守拒绝成交"
            return None
        if reference is None:
            if previous is None:
                return "缺少前收盘价，无法核验涨跌停"
            reference = number(previous["price"])
        upper, lower = money(reference * (1 + self.limit)), money(reference * (1 - self.limit))
        if bar.high > upper or bar.low < lower:
            return "行情超出已支持涨跌幅范围，需核验历史状态或除权参考价"
        if bar.high == bar.low and ((side == "buy" and bar.open >= upper)
                                     or (side == "sell" and bar.open <= lower)):
            return "一字涨停买入或一字跌停卖出，日线保守拒绝成交"
        return None
