"""Closed-bar position protection, independent of entry scanner eligibility."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from ..config.settings import Settings
from ..models import Bar, utc

NY = ZoneInfo("America/New_York")


def bearish_reversal(bars: list[Bar], symbol: str, opened_at: datetime,
                     now: datetime, settings: Settings) -> bool:
    if not settings.early_exit_enabled:
        return False
    now, opened_at = utc(now), utc(opened_at)
    day = now.astimezone(NY).date()
    observed: dict[datetime, Bar] = {}
    for bar in sorted(bars, key=lambda item: utc(item.received_at)):
        timestamp, received = utc(bar.timestamp), utc(bar.received_at)
        local = timestamp.astimezone(NY)
        if (bar.symbol != symbol or not timestamp <= received <= now or local.date() != day
                or not time(9, 30) < local.time().replace(tzinfo=None) <= time(16)):
            continue
        observed.setdefault(timestamp, bar)
    values = [observed[key] for key in sorted(observed)]
    if len(values) < 4:
        return False
    latest, previous = values[-1], values[-2]
    if (utc(latest.timestamp) <= opened_at
            or (now - utc(latest.timestamp)).total_seconds() > 60 + settings.max_quote_age_seconds):
        return False
    recent = values[-4:]
    if any((right.timestamp - left.timestamp).total_seconds() != 60
           for left, right in zip(recent, recent[1:])):
        return False
    volume = sum(bar.volume for bar in values)
    baseline = sum(bar.volume for bar in recent[:-1]) / 3
    if volume <= 0 or baseline <= 0:
        return False
    vwap = sum((bar.high + bar.low + bar.close) / 3 * bar.volume for bar in values) / volume
    return (latest.close < vwap and latest.close < previous.low and latest.close < previous.close
            and latest.volume >= baseline * settings.bearish_exit_volume_ratio)
