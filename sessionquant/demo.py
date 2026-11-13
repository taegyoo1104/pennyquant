"""Deterministic invented tape for integration checks, never market research data."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import json

from .events import Event, write_events
from .models import Bar, NewsEvent, Quote, StockSnapshot

NY = ZoneInfo("America/New_York")


def demo_events() -> list[Event]:
    events = []
    # Ten NY sessions across US DST change: UTC opening changes automatically.
    dates = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30",
             "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]
    for index, day in enumerate(dates):
        opening = datetime.fromisoformat(day + "T09:30:00").replace(tzinfo=NY).astimezone(ZoneInfo("UTC"))
        symbol = "DEMO"  # Deliberately fictional; no order routing exists.
        news_time = opening - timedelta(seconds=30)
        news = NewsEvent(id=f"synthetic-news-{day}", symbols=(symbol,),
            title="SYNTHETIC: Demo Company receives FDA approval for new treatment",
            published_at=news_time - timedelta(seconds=1), received_at=news_time,
            source="SYNTHETIC_FIXTURE", url="https://example.invalid/synthetic", verified=True, real_time=True)
        events.append(Event("news", news))
        prices = [2.00, 2.05, 2.12, 2.18, 2.24, 2.19, 2.16, 2.20, 2.25, 2.30,
                  2.18, 2.08, 2.12, 2.18, 2.24, 2.30, 2.36, 2.43, 2.51, 2.56]
        if index % 3 == 1:
            prices[9:] = [2.20, 2.10, 1.98, 1.92, 1.90, 1.88, 1.91, 1.90, 1.88, 1.86, 1.85]
        volumes = [100000, 140000, 190000, 220000, 250000, 90000, 75000, 200000, 350000, 410000,
                   170000, 120000, 160000, 240000, 380000, 420000, 440000, 460000, 480000, 490000]
        cumulative = 1_100_000
        dollars = 2_250_000.0
        high = prices[0]
        previous = prices[0] - 0.02
        for minute, (price, volume) in enumerate(zip(prices, volumes), 1):
            close = opening + timedelta(minutes=minute)
            spread = 0.004
            bid = round(price - spread / 2, 4)
            ask = round(price + spread / 2, 4)
            quote = Quote(symbol, close, close, bid, ask, 200, 200)
            cumulative += volume
            dollars += price * volume
            high = max(high, price)
            snap = StockSnapshot(quote, 1.75, cumulative, dollars, 4.2, high)
            events.append(Event("snapshot", snap))
            bar = Bar(symbol, close, close + timedelta(milliseconds=100), previous,
                round(max(previous, price) + 0.006, 4), round(min(previous, price) - 0.006, 4), price, volume)
            events.append(Event("bar", bar))
            # Future tradable quote at unchanged price permits a limit order to fill
            # only after the signal. A second update permits synthetic exit orders.
            for seconds in (1, 2):
                tick = close + timedelta(seconds=seconds)
                events.append(Event("quote", Quote(symbol, tick, tick, bid, ask, 200, 200)))
            previous = price
    return sorted(events, key=lambda event: event.available_at)


def write_demo(path: str | Path) -> list[Event]:
    path = Path(path)
    events = demo_events()
    write_events(path, events)
    path.with_suffix(".meta.json").write_text(json.dumps({"synthetic": True,
        "source_label": "Deterministic invented fixture; not market data", "events": len(events)}, indent=2), encoding="utf-8")
    return events
