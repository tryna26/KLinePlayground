"""Decimal-valued portfolio aggregates and JSON-safe ledger serialization."""

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date
from decimal import Decimal


def serialize(value):
    if is_dataclass(value):
        return serialize(asdict(value))
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(item) for item in value]
    if isinstance(value, (Decimal, date)):
        return str(value)
    return value


@dataclass
class Account:
    cash: Decimal
    initial_cash: Decimal
    realized_pnl: Decimal = Decimal("0.00")


@dataclass
class PositionLot:
    lot_id: str
    stock: str
    acquired_on: date
    available_on: date
    quantity: int
    cost: Decimal


@dataclass
class Order:
    order_id: str
    request_id: str
    stock: str
    side: str
    quantity: int
    submitted_on: date
    execute_on: date
    sequence: int
    estimated_price: Decimal | None = None
    estimated_fees: dict = field(default_factory=dict)
    frozen_cash: Decimal = Decimal("0.00")
    frozen_shares: int = 0
    status: str = "pending"
    reason: str = "等待下一公共交易日开盘"
    fee_version: str = ""
    rule_version: str = ""


@dataclass
class Fill:
    fill_id: str
    order_id: str
    stock: str
    side: str
    date: date
    quantity: int
    price: Decimal
    gross: Decimal
    fees: dict
    cost_released: Decimal
    realized_pnl: Decimal
    data_source: str | None
    data_version: str | None
    rule_version: str
    model: str = "日线简化模拟：下一开盘价；不能还原盘口与排队"


@dataclass
class LedgerEntry:
    entry_id: str
    date: date
    kind: str
    cash_delta: Decimal
    stock: str | None = None
    quantity_delta: int = 0
    cost_delta: Decimal = Decimal("0.00")
    realized_delta: Decimal = Decimal("0.00")
    reference: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class DailyAsset:
    date: date
    cash: Decimal
    market_value: Decimal
    total_assets: Decimal
    daily_pnl: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    marks: dict


@dataclass
class PortfolioState:
    current_date: date
    account: Account
    version: int = 0
    lots: list[PositionLot] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)
    daily_assets: list[DailyAsset] = field(default_factory=list)
    requests: dict = field(default_factory=dict)
    entitlements: dict = field(default_factory=dict)
    applied_actions: list[str] = field(default_factory=list)
    status: str = "active"
    ended_at: str | None = None


def restore_state(payload):
    """Read aggregate v1, including pre-HTTP stage 3 records with no status."""
    from backend.replay_models import day, number, ReplayDataError

    def row(cls, raw, dates=(), decimals=()):
        value = dict(raw)
        for key in dates:
            value[key] = day(value[key])
        for key in decimals:
            if value.get(key) is not None:
                value[key] = number(value[key])
        if "fees" in value:
            value["fees"] = fees(value["fees"])
        if "estimated_fees" in value:
            value["estimated_fees"] = fees(value["estimated_fees"])
        if cls is LedgerEntry:
            value["details"] = dict(value["details"])
            if "lots" in value["details"]:
                value["details"]["lots"] = [{**item, "cost": number(item["cost"])}
                                             for item in value["details"]["lots"]]
        return cls(**value)

    def fees(raw):
        return {key: val if key == "version" else number(val) for key, val in raw.items()}

    try:
        value = dict(payload)
        value["current_date"] = day(value["current_date"])
        value["account"] = row(Account, value["account"], decimals=("cash", "initial_cash", "realized_pnl"))
        formats = (
            ("lots", PositionLot, ("acquired_on", "available_on"), ("cost",)),
            ("orders", Order, ("submitted_on", "execute_on"), ("estimated_price", "frozen_cash")),
            ("fills", Fill, ("date",), ("price", "gross", "cost_released", "realized_pnl")),
            ("ledger", LedgerEntry, ("date",), ("cash_delta", "cost_delta", "realized_delta")),
            ("daily_assets", DailyAsset, ("date",), ("cash", "market_value", "total_assets", "daily_pnl", "realized_pnl", "unrealized_pnl")),
        )
        for key, cls, dates, decimals in formats:
            value[key] = [row(cls, item, dates, decimals) for item in value[key]]
        state = PortfolioState(**value)
        if state.status not in {"active", "ended"} or type(state.version) is not int or state.version < 0:
            raise ValueError("invalid state")
        return state
    except (KeyError, TypeError, ValueError) as exc:
        raise ReplayDataError("组合账户结构损坏或版本不受支持") from exc
