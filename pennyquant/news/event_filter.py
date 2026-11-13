"""Time-aware news gating and deterministic deduplication."""
from __future__ import annotations

from datetime import datetime
from ..config.settings import Settings
from ..models import NewsEvent, utc
from .classifier import classify


def eligible_events(events: list[NewsEvent], symbol: str, now: datetime, settings: Settings) -> list[NewsEvent]:
    now = utc(now)
    deduplicated: dict[str, NewsEvent] = {}
    for event in sorted(events, key=lambda item: utc(item.received_at)):
        published, received = utc(event.published_at), utc(event.received_at)
        if symbol not in event.symbols or published > now or received > now:
            continue
        if received < published or (now - published).total_seconds() > settings.max_news_age_hours * 3600:
            continue
        if (received - published).total_seconds() > settings.max_news_delay_seconds:
            continue
        if settings.require_realtime_news and not event.real_time:
            continue
        if not event.verified or not event.source.strip():
            continue
        # First actual receipt wins. Re-polling an old item must not refresh it.
        deduplicated.setdefault(event.id, event)
    return sorted(deduplicated.values(), key=lambda item: (utc(item.received_at), item.id))


def positive_event(events: list[NewsEvent], symbol: str, now: datetime, settings: Settings) -> NewsEvent | None:
    now = utc(now)
    # Already-known hazards must remain effective even if delivered by a delayed
    # supplemental feed. Positive catalysts require stricter latency entitlement.
    for event in events:
        published, received = utc(event.published_at), utc(event.received_at)
        if symbol not in event.symbols or published > now or received > now or received < published:
            continue
        if (now - published).total_seconds() > settings.max_news_age_hours * 3600:
            continue
        result = classify(event)
        if result.sentiment == "negative" or result.exclude or result.dilution_risk:
            return None
    eligible = eligible_events(events, symbol, now, settings)
    classified = [(event, classify(event)) for event in eligible]
    positives = [event for event, result in classified if result.sentiment == "positive" and not result.exclude]
    return positives[-1] if positives else None
