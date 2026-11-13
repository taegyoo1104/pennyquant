"""Causal, closed-bar strategy inputs shared by four experimental rules."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from hashlib import sha256
from typing import Protocol
from zoneinfo import ZoneInfo

from ..config.settings import Settings
from ..models import Bar, NewsEvent, Signal, StockSnapshot, utc
from ..news.event_filter import positive_event
from ..scanners.stock_scanner import scan


NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class StrategyContext:
    bars: list[Bar]
    snapshot: StockSnapshot
    news: list[NewsEvent]
    now: datetime
    settings: Settings


class Strategy(Protocol):
    name: str

    def evaluate(self, context: StrategyContext) -> Signal | None: ...


def session_vwap(bars: list[Bar]) -> float | None:
    """Volume-weighted typical price from available session bars only."""
    volume = sum(bar.volume for bar in bars)
    if volume <= 0:
        return None
    return sum((bar.high + bar.low + bar.close) / 3 * bar.volume for bar in bars) / volume


def prepare(context: StrategyContext, minimum_bars: int) -> tuple[list[Bar], NewsEvent] | None:
    """Fail closed on stale/inconsistent inputs; future observations are invisible.

    A one-minute bar is usable only after its declared close AND receipt. Missing
    bars are not reconstructed. Data from other sessions/symbols is never mixed.
    """
    now = utc(context.now)
    if not scan(context.snapshot, now, context.settings)[0]:
        return None
    symbol = context.snapshot.quote.symbol
    day = now.astimezone(NEW_YORK).date()
    observed: dict[datetime, Bar] = {}
    for bar in context.bars:
        if bar.symbol != symbol:
            continue
        timestamp, received = utc(bar.timestamp), utc(bar.received_at)
        if timestamp > now or received > now or received < timestamp:
            continue
        local = timestamp.astimezone(NEW_YORK)
        if local.date() != day or not time(9, 30) < local.time().replace(tzinfo=None) <= time(16):
            continue
        # First observation wins. A later revision may contain hindsight and must
        # not replace the original closed bar in a historical decision.
        observed.setdefault(timestamp, bar)
    bars = [observed[key] for key in sorted(observed)]
    if len(bars) < minimum_bars:
        return None
    if (now - utc(bars[-1].timestamp)).total_seconds() > 60 + context.settings.max_quote_age_seconds:
        return None
    # Strategies compare adjacent one-minute bars, not arbitrary missing periods.
    recent = bars[-minimum_bars:]
    if any((utc(right.timestamp) - utc(left.timestamp)).total_seconds() != 60 for left, right in zip(recent, recent[1:])):
        return None
    news = positive_event(context.news, symbol, now, context.settings)
    if news is None:
        return None
    # A trend that completed before the catalyst was received is not evidence
    # of the market's response to that catalyst.
    if utc(bars[-1].timestamp) <= utc(news.received_at):
        return None
    return bars, news


def signal(name: str, context: StrategyContext, bar: Bar, news: NewsEvent, reason: str) -> Signal:
    event_time = utc(context.now)
    key = f"{name}:{bar.symbol}:{utc(bar.timestamp).isoformat()}:{news.id}"
    return Signal(
        id=sha256(key.encode()).hexdigest()[:32],
        symbol=bar.symbol,
        strategy=name,
        timestamp=event_time,
        reference_price=bar.close,
        reason=reason,
        news_id=news.id,
    )
