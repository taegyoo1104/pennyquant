"""Observe market-wide rankings without inventing tradability or RVOL.

Ranking turnover is consolidated as documented for MARKET_/TOP_ rankings. A
short-window candle-volume ratio is a different measure and is never supplied
to the trading scanner as its same-time historical relative volume.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import math

from ..models import Bar, symbol_valid, utc
from ..pipeline_config import PipelineConfig
from .common import DataQualityError, now_utc


def _number(value, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a market number")
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError("invalid market number")
    return result


@dataclass
class Candidate:
    symbol: str
    last_price: float
    gain_pct: float | None
    trading_volume: float | None
    trading_amount: float | None
    ranked_at: datetime | None
    received_at: datetime
    triggers: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=lambda: ["verified_market_snapshot_required", "primary_news_analysis_required"])
    first_seen_at: datetime | None = None
    readiness: str = "WATCH"
    short_volume_ratio: float | None = None
    source: str = "toss_rankings"

    def to_dict(self) -> dict:
        result = asdict(self)
        result["id"] = self.symbol
        for name in ("ranked_at", "received_at", "first_seen_at"):
            result[name] = utc(result[name]).isoformat() if result[name] is not None else None
        return result


def parse_rankings(payload, ranking_type: str, received_at: datetime, config: PipelineConfig) -> list[Candidate]:
    received_at = utc(received_at)
    if not isinstance(payload, dict) or not isinstance(payload.get("rankings"), list):
        raise DataQualityError("Toss rankings response requires a rankings array")
    ranked_at = None
    if payload.get("rankedAt") is not None:
        try:
            ranked_at = utc(payload["rankedAt"])
        except (ValueError, TypeError, AttributeError):
            raise DataQualityError("Toss rankings timestamp is invalid") from None
    result = []
    for item in payload["rankings"]:
        try:
            if not isinstance(item, dict) or item.get("currency") != "USD":
                continue
            symbol = item["symbol"]
            symbol_valid(symbol)
            price = item["price"]
            last = _number(price["lastPrice"], minimum=0.000001)
            if not config.price_min <= last <= config.price_max:
                continue
            change = None if price.get("changeRate") is None else _number(price["changeRate"])
            volume = _number(item["tradingVolume"], minimum=0)
            amount = _number(item["tradingAmount"], minimum=0)
        except (ValueError, TypeError, KeyError):
            continue  # Retain independently valid rows; never guess malformed numbers.
        triggers = []
        if change is not None and change >= config.min_abs_change:
            triggers.append("price_rise")
        if change is not None and change <= -config.min_abs_change:
            triggers.append("price_fall_watch")
        if ranking_type == "MARKET_TRADING_VOLUME" and volume >= config.min_market_volume:
            triggers.append("high_market_volume")
        if ranking_type == "MARKET_TRADING_AMOUNT" and amount >= config.min_market_amount:
            triggers.append("high_market_turnover")
        if not triggers:
            continue
        candidate = Candidate(symbol, last, change, volume, amount, ranked_at, received_at, triggers,
                              first_seen_at=received_at)
        if ranked_at is None:
            candidate.blockers.append("ranking_time_unknown")
        elif ranked_at > received_at or (received_at - ranked_at).total_seconds() > config.rankings_max_age_seconds:
            candidate.blockers.append("ranking_stale_or_future")
        result.append(candidate)
    return result


def candle_observation(bars: list[Bar], config: PipelineConfig) -> dict:
    """Use only contiguous completed same-session bars; no zero-fill or RVOL claim."""
    if len(bars) < 6:
        return {"short_volume_ratio": None, "volume_spike": False}
    bars = sorted(bars, key=lambda bar: bar.timestamp)[-6:]
    if any((right.timestamp - left.timestamp).total_seconds() != 60 for left, right in zip(bars, bars[1:])):
        return {"short_volume_ratio": None, "volume_spike": False}
    if any(bar.timestamp.date() != bars[-1].timestamp.date() or bar.symbol != bars[-1].symbol for bar in bars):
        return {"short_volume_ratio": None, "volume_spike": False}
    baseline = sum(bar.volume for bar in bars[:-1]) / 5
    ratio = bars[-1].volume / baseline if baseline > 0 else None
    return {"short_volume_ratio": ratio, "volume_spike": ratio is not None and ratio >= config.volume_spike_ratio}


class MarketScanner:
    def __init__(self, client, config: PipelineConfig, *, clock=now_utc):
        self.client, self.config = client, config
        self.clock = clock
        self.candidates: dict[str, Candidate] = {}
        self.next_rankings_at: datetime | None = None
        self.candle_cursor = 0

    async def discover(self, now: datetime | None = None) -> list[Candidate]:
        now = utc(now or now_utc())
        merged: dict[str, Candidate] = {}
        for ranking_type in self.config.ranking_types:
            payload = await self.client.rankings(ranking_type=ranking_type, count=self.config.ranking_count)
            # A provider may update its ranking after the request began. Receipt
            # time is captured after the response, not at cycle startup.
            received_at = utc(self.clock())
            for candidate in parse_rankings(payload, ranking_type, received_at, self.config):
                previous = merged.get(candidate.symbol)
                if previous:
                    previous.triggers = list(dict.fromkeys(previous.triggers + candidate.triggers))
                    previous.blockers = list(dict.fromkeys(previous.blockers + candidate.blockers))
                    previous.trading_amount = max(previous.trading_amount or 0, candidate.trading_amount or 0)
                else:
                    merged[candidate.symbol] = candidate
        ordered = sorted(merged.values(), key=lambda item: (-(item.trading_amount or 0), -abs(item.gain_pct or 0), item.symbol))
        for candidate in ordered:
            if candidate.symbol in self.candidates:
                candidate.first_seen_at = self.candidates[candidate.symbol].first_seen_at
        self.candidates = {item.symbol: item for item in ordered[:self.config.max_candidates]}
        return list(self.candidates.values())

    async def candle_updates(self, symbols: list[str]) -> list[Bar]:
        if not symbols:
            return []
        limit = min(self.config.candle_symbols_per_cycle, len(symbols))
        result = []
        for offset in range(limit):
            symbol = symbols[(self.candle_cursor + offset) % len(symbols)]
            bars, _ = await self.client.candles(symbol, count=30)
            result.extend(bars)
            if symbol in self.candidates:
                observation = candle_observation(bars, self.config)
                self.candidates[symbol].short_volume_ratio = observation["short_volume_ratio"]
                if observation["volume_spike"] and "short_window_volume_spike" not in self.candidates[symbol].triggers:
                    self.candidates[symbol].triggers.append("short_window_volume_spike")
        self.candle_cursor = (self.candle_cursor + limit) % len(symbols)
        return result
