"""Strict, unadjusted daily data contracts for portfolio replay (schema v1)."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import re


SCHEMA_VERSION = 1


class ReplayDataError(ValueError):
    """Safe user-facing validation failure; never include raw future records."""


def day(value) -> date:
    if type(value) is date:
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ReplayDataError("日期必须使用 YYYY-MM-DD 格式")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ReplayDataError("日期无效") from exc


def stock_code(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:60[0135]|68[89]|00[0123]|30[01]|43[0-9]|8[237][0-9]|92[0-9])[0-9]{3}", value):
        raise ReplayDataError("请输入六位 A 股股票代码")
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp(value) -> datetime:
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError()
        return result
    except (TypeError, ValueError) as exc:
        raise ReplayDataError("更新时间必须包含时区") from exc


def number(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ReplayDataError("价格或成交量不是有效数字") from exc
    if not result.is_finite():
        raise ReplayDataError("价格或成交量必须是有限数字")
    return result


class DataStatus(str, Enum):
    TRADING = "trading"
    SUSPENDED = "suspended"
    NOT_LISTED = "not_listed"
    DELISTED = "delisted"
    DOWNLOAD_FAILED = "download_failed"
    CACHE_MISSING = "cache_missing"
    UNKNOWN = "unknown"
    INVALID = "invalid"
    NO_TRADES = "no_trades"
    MARKET_CLOSED = "market_closed"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class DailyBar:
    stock: str
    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal  # shares, never lots

    def __post_init__(self):
        stock_code(self.stock)
        object.__setattr__(self, "date", day(self.date))
        for field in ("open", "high", "low", "close", "volume"):
            object.__setattr__(self, field, number(getattr(self, field)))
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ReplayDataError("原始价格必须大于零")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ReplayDataError("开高低收价格关系无效")
        if self.volume < 0 or self.volume != self.volume.to_integral_value():
            raise ReplayDataError("成交量必须是非负整数股数")

    def to_dict(self):
        return {"stock": self.stock, "date": self.date.isoformat(),
                **{field: str(getattr(self, field)) for field in ("open", "high", "low", "close", "volume")}}


def validate_bars(code, rows, start, end) -> tuple[DailyBar, ...]:
    """Sort rows; identical duplicates collapse, conflicting duplicates reject all."""
    code, start, end = stock_code(code), day(start), day(end)
    if end < start:
        raise ReplayDataError("结束日期不能早于开始日期")
    found = {}
    try:
        for row in rows:
            if row.get("stock", code) != code:
                raise ReplayDataError("行情股票代码不匹配")
            bar = DailyBar(code, row["date"], **{key: row[key] for key in ("open", "high", "low", "close", "volume")})
            if not start <= bar.date <= end:
                raise ReplayDataError("行情日期超出请求范围")
            if bar.date in found and found[bar.date] != bar:
                raise ReplayDataError("同一日期存在冲突行情，整批拒绝")
            found[bar.date] = bar
    except (KeyError, TypeError, AttributeError) as exc:
        raise ReplayDataError("行情缺少必要字段或格式无效") from exc
    return tuple(found[key] for key in sorted(found))


@dataclass(frozen=True)
class Evidence:
    """Coverage is an explicit reviewed assertion, never inferred from price rows.

    Only trusted backend importers construct this; it is not a client parameter.
    'reviewed' requires externally checked source records and reviewer reference.
    """
    start: date
    end: date
    source: str
    version: str
    reference: str
    checked_at: str
    scope: str = "reviewed"  # or synthetic, which requires explicit test mode

    def __post_init__(self):
        object.__setattr__(self, "start", day(self.start))
        object.__setattr__(self, "end", day(self.end))
        timestamp(self.checked_at)
        if self.end < self.start or not all((self.source, self.version, self.reference)):
            raise ReplayDataError("覆盖证明缺少来源、版本、核验依据或有效日期范围")
        if self.scope not in {"reviewed", "synthetic", "practice"}:
            raise ReplayDataError("覆盖证明类型无效")

    def covers(self, start, end, *, synthetic=False):
        return self.start <= day(start) <= day(end) <= self.end and (synthetic or self.scope != "synthetic")


@dataclass(frozen=True)
class SecurityHistory:
    stock: str
    listed_on: date
    evidence: Evidence
    delisted_on: date | None = None  # first date no longer listed
    suspended: tuple[date, ...] = ()
    special_periods: tuple[tuple[date, date, str], ...] = ()

    def __post_init__(self):
        stock_code(self.stock)
        object.__setattr__(self, "listed_on", day(self.listed_on))
        if self.delisted_on is not None:
            object.__setattr__(self, "delisted_on", day(self.delisted_on))
            if self.delisted_on <= self.listed_on:
                raise ReplayDataError("上市和退市日期关系无效")
        object.__setattr__(self, "suspended", tuple(sorted({day(d) for d in self.suspended})))
        periods = tuple((day(a), day(b), str(reason)) for a, b, reason in self.special_periods)
        if any(a > b or not reason for a, b, reason in periods):
            raise ReplayDataError("特殊历史状态区间无效")
        object.__setattr__(self, "special_periods", periods)


@dataclass(frozen=True)
class CorporateAction:
    event_id: str
    kind: str
    effective_on: date
    reference: str
    # Event window includes record/ex/payment/share-credit dates where relevant.
    # Blocking the whole window also handles training starting after the ex-date.
    window_end: date | None = None
    # Trusted importer supplies NET dividend (tax treatment already reviewed),
    # explicit record/credit dates and the exchange's ex-right reference price.
    record_on: date | None = None
    cash_per_share: Decimal = Decimal("0")
    share_ratio: Decimal = Decimal("0")
    available_on: date | None = None
    reference_price: Decimal | None = None

    def __post_init__(self):
        object.__setattr__(self, "effective_on", day(self.effective_on))
        object.__setattr__(self, "window_end", day(self.window_end or self.effective_on))
        if not all((self.event_id, self.kind, self.reference)) or self.window_end < self.effective_on:
            raise ReplayDataError("公司行为缺少有效日期、类型或依据")
        if self.record_on is not None:
            object.__setattr__(self, "record_on", day(self.record_on))
        if self.available_on is not None:
            object.__setattr__(self, "available_on", day(self.available_on))
        for field in ("cash_per_share", "share_ratio", "reference_price"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, number(value))

    def require_accounting(self, start):
        if (self.kind not in {"cash_dividend", "bonus_shares"} or self.record_on is None
                or not day(start) <= self.record_on < self.effective_on
                or self.window_end != self.effective_on
                or self.reference_price is None or self.reference_price <= 0):
            raise ReplayDataError("公司行为核算需要支持类型、区间内登记日、同日生效入账和除权参考价")
        if self.kind == "cash_dividend":
            if self.cash_per_share <= 0 or self.share_ratio != 0:
                raise ReplayDataError("现金分红需要已核验税后每股金额")
        elif (self.share_ratio <= 0 or self.cash_per_share != 0 or self.available_on is None
              or self.available_on < self.effective_on):
            raise ReplayDataError("送转需要送股比例和明确的可卖日期")


@dataclass(frozen=True)
class ActionHistory:
    stock: str
    evidence: Evidence
    events: tuple[CorporateAction, ...] = ()

    def __post_init__(self):
        stock_code(self.stock)
        object.__setattr__(self, "events", tuple(self.events))
        if len({event.event_id for event in self.events}) != len(self.events):
            raise ReplayDataError("公司行为事件编号重复")

    def require_supported(self, start, end, *, synthetic=False, accounting=False):
        start, end = day(start), day(end)
        if not self.evidence.covers(start, end, synthetic=synthetic):
            raise ReplayDataError(f"{self.stock}：公司行为完整性未核实，无法创建或推进该区间")
        for event in self.events:
            if event.effective_on <= end and event.window_end >= start:
                if not accounting:
                    raise ReplayDataError(f"{self.stock}：区间包含尚未支持核算的公司行为，禁止跨越")
                event.require_accounting(start)


@dataclass(frozen=True)
class QuoteState:
    stock: str
    date: date
    status: DataStatus
    reason: str
    bar: DailyBar | None = None
    source: str | None = None
    updated_at: str | None = None
    data_version: str | None = None
    status_source: str | None = None
    status_version: str | None = None

    @property
    def execution_data_ready(self):
        # Actual order checks (limits, budget, T+1) belong to stage 3.
        return self.status == DataStatus.TRADING and self.bar is not None

    def to_dict(self):
        return {"stock": self.stock, "date": self.date.isoformat(), "status": self.status.value,
                "reason": self.reason, "execution_data_ready": self.execution_data_ready,
                "bar": self.bar.to_dict() if self.bar else None, "source": self.source,
                "updated_at": self.updated_at, "data_version": self.data_version,
                "status_source": self.status_source, "status_version": self.status_version,
                "price_basis": "unadjusted", "volume_unit": "share", "schema_version": SCHEMA_VERSION}
