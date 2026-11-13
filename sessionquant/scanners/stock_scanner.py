"""Stateless, fail-closed scanner using same-time historical relative volume."""
from __future__ import annotations

from datetime import datetime
import math
from ..config.settings import Settings
from ..models import StockSnapshot, utc


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def scan(snapshot: StockSnapshot, now: datetime, settings: Settings) -> tuple[bool, list[str]]:
    now = utc(now)
    quote = snapshot.quote
    reasons: list[str] = []
    if quote.exchange.upper() not in {"NASDAQ", "NYSE", "AMEX", "NYSEAMERICAN", "NYSE ARCA", "ARCA", "NYSEARCA"}:
        reasons.append("unapproved_exchange")
    if not quote.tradable or quote.halted:
        reasons.append("not_tradable")
    if quote.received_at > now or quote.timestamp > now or quote.received_at < quote.timestamp:
        reasons.append("invalid_quote_time")
    elif (now - quote.timestamp).total_seconds() > settings.max_quote_age_seconds or (now - quote.received_at).total_seconds() > settings.max_quote_age_seconds:
        reasons.append("stale_quote")
    if not settings.min_price <= quote.ask <= settings.max_price:
        reasons.append("price_out_of_range")
    if not _finite(snapshot.prev_close) or snapshot.prev_close <= 0:
        reasons.append("unknown_previous_close")
    elif quote.bid / snapshot.prev_close - 1 < settings.min_gain_pct - 1e-12:
        reasons.append("insufficient_gain")
    if not _finite(snapshot.relative_volume):
        reasons.append("unknown_relative_volume")
    elif snapshot.relative_volume < settings.min_relative_volume:
        reasons.append("insufficient_relative_volume")
    if not _finite(snapshot.dollar_volume) or snapshot.dollar_volume < settings.min_dollar_volume:
        reasons.append("insufficient_dollar_volume")
    if not _finite(snapshot.cumulative_volume) or snapshot.cumulative_volume <= 0:
        reasons.append("insufficient_volume")
    if quote.spread_pct > settings.max_spread_pct + 1e-12:
        reasons.append("wide_spread")
    if not _finite(quote.bid_size) or not _finite(quote.ask_size) or quote.bid_size <= 0 or quote.ask_size <= 0:
        reasons.append("insufficient_depth")
    if not _finite(snapshot.day_high) or snapshot.day_high <= 0 or snapshot.day_high < quote.bid:
        reasons.append("invalid_day_high")
    elif 1 - quote.bid / snapshot.day_high > settings.max_drawdown_from_high:
        reasons.append("falling_from_high")
    if snapshot.corporate_action:
        reasons.append("corporate_action")
    if snapshot.dilution_risk:
        reasons.append("dilution_risk")
    return not reasons, reasons


def rank_candidates(snapshots: list[StockSnapshot], now: datetime, settings: Settings) -> list[StockSnapshot]:
    """Select at most max_candidates; stable ordering keeps subscriptions bounded."""
    passed = [snapshot for snapshot in snapshots if scan(snapshot, now, settings)[0]]
    return sorted(passed, key=lambda item: (-item.dollar_volume, item.quote.symbol))[:settings.max_candidates]
