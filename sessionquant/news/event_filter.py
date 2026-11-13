"""Time-aware news gating and deterministic deduplication."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from ..config.settings import Settings
from ..models import NewsEvent, utc
from .classifier import classify
from .analysis import event_digest


def _analysis(event: NewsEvent, symbol: str, analyses: dict | None) -> dict | None:
    value = (analyses or {}).get(f"{event.id}:{symbol}")
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, dict) or value.get("ticker") != symbol or value.get("news_id") != event.id:
        return None
    if value.get("input_digest") != event_digest(event, symbol):
        return None
    return value


def _completed(value: dict, event: NewsEvent, now: datetime) -> datetime | None:
    try:
        completed = utc(value["analyzed_at"])
        return completed if utc(event.received_at) <= completed <= now else None
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def eligible_events(events: list[NewsEvent], symbol: str, now: datetime, settings: Settings,
                    analyses: dict | None = None) -> list[NewsEvent]:
    now = utc(now)
    deduplicated: dict[str, NewsEvent] = {}
    for event in sorted(events, key=lambda item: utc(item.received_at)):
        published, received = utc(event.published_at), utc(event.received_at)
        if symbol not in event.symbols or published > now or received > now:
            continue
        max_age = settings.primary_news_max_age_hours if settings.news_policy == "primary" else settings.max_news_age_hours
        if received < published or (now - published).total_seconds() > max_age * 3600:
            continue
        if settings.news_policy == "primary":
            approved = _analysis(event, symbol, analyses)
            if not approved or not event.verified or not event.source.strip():
                continue
            kind = approved.get("source_kind")
            if kind not in {"sec", "rss"} or not event.id.startswith(kind + ":"):
                continue
            if (approved.get("primary_verified") is not True or approved.get("entry_eligible") is not True
                    or approved.get("sentiment") != "POSITIVE" or approved.get("dilution_risk") is not False):
                continue
            completed = _completed(approved, event, now)
            if completed is None:
                continue
            # Strategy bars must occur after the analysis was actually available.
            deduplicated.setdefault(event.id, replace(event, received_at=max(received, completed)))
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


def positive_event(events: list[NewsEvent], symbol: str, now: datetime, settings: Settings,
                   analyses: dict | None = None) -> NewsEvent | None:
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
        approved = _analysis(event, symbol, analyses)
        structured_risk = (approved is not None and _completed(approved, event, now) is not None
                           and (approved.get("sentiment") in {"NEGATIVE", "MIXED"}
                                or approved.get("dilution_risk") is True))
        if (result.sentiment == "negative" or result.dilution_risk or structured_risk
                or (settings.news_policy == "realtime" and result.exclude)):
            return None
    eligible = eligible_events(events, symbol, now, settings, analyses)
    if settings.news_policy == "primary":
        return eligible[-1] if eligible else None
    classified = [(event, classify(event)) for event in eligible]
    positives = [event for event, result in classified if result.sentiment == "positive" and not result.exclude]
    return positives[-1] if positives else None
