"""Receipt-ordered JSONL interchange. Bar timestamps denote completed bars."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json

from .models import Bar, NewsEvent, Quote, StockSnapshot, jsonable, utc


@dataclass(frozen=True)
class Event:
    kind: str
    data: Bar | Quote | StockSnapshot | NewsEvent

    @property
    def available_at(self) -> datetime:
        return utc(self.data.quote.received_at if isinstance(self.data, StockSnapshot) else self.data.received_at)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "data": jsonable(self.data)}


def parse_quote(data: dict) -> Quote:
    data = dict(data)
    if not {"tradable", "halted", "exchange"}.issubset(data):
        data["tradable"] = False
    data.setdefault("halted", False)
    data.setdefault("exchange", "UNKNOWN")
    return Quote(**{**data, "timestamp": utc(data["timestamp"]), "received_at": utc(data["received_at"])})


def parse_event(raw: dict) -> Event:
    data = dict(raw["data"])
    match raw["kind"]:
        case "quote":
            obj = parse_quote(data)
        case "snapshot":
            if not {"corporate_action", "dilution_risk"}.issubset(data):
                raise ValueError("snapshot requires explicit corporate_action and dilution_risk checks")
            obj = StockSnapshot(**{**data, "quote": parse_quote(data["quote"])})
        case "bar":
            obj = Bar(**{**data, "timestamp": utc(data["timestamp"]), "received_at": utc(data["received_at"])})
        case "news":
            data.setdefault("verified", False)
            data.setdefault("real_time", False)
            obj = NewsEvent(**{**data, "symbols": tuple(data["symbols"]),
                               "published_at": utc(data["published_at"]), "received_at": utc(data["received_at"])})
        case _:
            raise ValueError(f"unknown event kind: {raw['kind']}")
    return Event(raw["kind"], obj)


def read_events(path: str | Path) -> list[Event]:
    events = []
    last = None
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = parse_event(json.loads(line))
            if last is not None and event.available_at < last:
                raise ValueError("events must be sorted by received_at")
            time = event.data.quote.timestamp if isinstance(event.data, StockSnapshot) else getattr(event.data, "timestamp", None)
            if time is not None and utc(time) > event.available_at:
                raise ValueError("market event occurs after receipt")
            last = event.available_at
            events.append(event)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"invalid event line {line_number}: {exc}") from exc
    return events


def write_events(path: str | Path, events: list[Event]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event.to_dict(), ensure_ascii=False, allow_nan=False) + "\n" for event in events), encoding="utf-8")
