"""Shared domain objects. All event/receipt times must be timezone-aware UTC."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from enum import StrEnum
from decimal import Decimal
from typing import Any
import math
import re


def utc(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError("timezone-aware timestamp required")
    return result.astimezone(timezone.utc)


def positive(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


def whole(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def symbol_valid(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9.\-]{1,20}", value):
        raise ValueError("invalid symbol")


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime  # bar CLOSE time; never the opening time
    received_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    source: str = "UNKNOWN"
    price_adjustment: str = "UNKNOWN"

    def __post_init__(self):
        symbol_valid(self.symbol)
        utc(self.timestamp); utc(self.received_at)
        whole(self.volume, "volume")
        if not isinstance(self.source, str) or not self.source or self.price_adjustment not in {"UNKNOWN", "raw", "adjusted"}:
            raise ValueError("봉 제공처·가격 조정 정책을 확인하세요.")
        for key in ("open", "high", "low", "close"):
            positive(getattr(self, key), key)
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close) or self.low > self.high or self.volume < 0:
            raise ValueError("invalid OHLCV")


@dataclass(frozen=True)
class Quote:
    symbol: str
    timestamp: datetime
    received_at: datetime
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    tradable: bool = True
    halted: bool = False
    exchange: str = "NASDAQ"

    def __post_init__(self):
        symbol_valid(self.symbol)
        utc(self.timestamp); utc(self.received_at)
        whole(self.bid_size, "bid_size"); whole(self.ask_size, "ask_size")
        if type(self.tradable) is not bool or type(self.halted) is not bool:
            raise ValueError("tradable and halted must be booleans")
        positive(self.bid, "bid"); positive(self.ask, "ask")
        if self.ask < self.bid or self.bid_size < 0 or self.ask_size < 0:
            raise ValueError("invalid quote")

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / ((self.ask + self.bid) / 2)


@dataclass(frozen=True)
class StockSnapshot:
    quote: Quote
    prev_close: float
    cumulative_volume: int
    dollar_volume: float
    relative_volume: float | None
    day_high: float
    corporate_action: bool = False
    dilution_risk: bool = False

    def __post_init__(self):
        # Scanner records explicit rejection reasons for absent/bad indicators.
        if type(self.corporate_action) is not bool or type(self.dilution_risk) is not bool:
            raise ValueError("risk flags must be booleans")


@dataclass(frozen=True)
class NewsEvent:
    id: str
    symbols: tuple[str, ...]
    title: str
    published_at: datetime
    received_at: datetime
    source: str
    url: str = ""
    body: str = ""
    verified: bool = True
    real_time: bool = False

    def __post_init__(self):
        utc(self.published_at); utc(self.received_at)
        for symbol in self.symbols:
            symbol_valid(symbol)
        if type(self.verified) is not bool or type(self.real_time) is not bool:
            raise ValueError("news source flags must be booleans")


@dataclass(frozen=True)
class Classification:
    sentiment: str  # positive / negative / neutral / uncertain
    event_type: str
    reason: str
    exclude: bool = False
    dilution_risk: bool = False


@dataclass(frozen=True)
class Signal:
    id: str
    symbol: str
    strategy: str
    timestamp: datetime
    reference_price: float
    reason: str
    news_id: str | None = None


class OrderStatus(StrEnum):
    PENDING = "pending"
    OPEN = "open"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass
class Order:
    client_id: str
    symbol: str
    side: str
    qty: int
    limit_price: float
    created_at: datetime
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    strategy: str = ""
    reason: str = ""


@dataclass(frozen=True)
class Fill:
    id: str
    client_id: str
    symbol: str
    side: str
    qty: int
    price: float
    fee: float
    timestamp: datetime


@dataclass
class Position:
    symbol: str
    qty: int
    avg_price: float
    entry_fee: float
    stop_price: float
    target_price: float
    opened_at: datetime
    strategy: str = ""


@dataclass
class Account:
    cash: float = 70.0
    equity: float = 70.0
    day_start_equity: float = 70.0
    realized_pnl: float = 0.0
    entries_today: int = 0
    consecutive_losses: int = 0
    positions: dict[str, Position] = field(default_factory=dict)
    reserved_cash: float = 0.0


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    qty: int = 0
    limit_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0


def jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        if isinstance(value, type):
            raise TypeError('jsonable requires a dataclass instance')
        # Convert immutable domain fields directly; asdict first deep-copies every
        # datetime/ZoneInfo before the same recursive JSON traversal.
        return {f.name: jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("금액과 수량은 유한한 Decimal이어야 합니다")
        return str(value)
    if isinstance(value, datetime):
        return utc(value).isoformat()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value
