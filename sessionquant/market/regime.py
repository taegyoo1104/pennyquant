"""SPY·QQQ의 실제 확정봉으로 시장 국면을 판단한다."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..models import Bar, Quote, utc
from .bars import contiguous, indicators, session_bounds


@dataclass(frozen=True)
class RegimeResult:
    state: str
    reasons: tuple[str, ...]
    components: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"state": self.state, "reasons": list(self.reasons), "components": self.components}


def classify_regime(bars: dict[str, list[Bar]], quotes: dict[str, Quote], now: datetime, *,
                    max_quote_age_seconds: float = 30, max_bar_age_seconds: float = 330) -> RegimeResult:
    now = utc(now)
    bounds = session_bounds(now)
    if not bounds or not bounds[0] <= now < bounds[1]:
        return RegimeResult("UNKNOWN", ("outside_regular_session",))
    directions, components, blockers = [], {}, []
    for symbol in ("SPY", "QQQ"):
        quote = quotes.get(symbol)
        if quote is None or not (quote.timestamp <= quote.received_at <= now) or any(
                (now - t).total_seconds() > max_quote_age_seconds for t in (quote.timestamp, quote.received_at)):
            blockers.append(f"{symbol}:quote_unknown_or_stale")
            continue
        data = indicators(bars.get(symbol, []), now)
        if len(data.five) < 3 or not contiguous(list(data.five[-3:]), 5) or data.vwap is None or (
                now - data.five[-1].timestamp).total_seconds() > max_bar_age_seconds:
            blockers.append(f"{symbol}:closed_bars_unknown_or_stale")
            continue
        last = data.five[-3:]
        rising = last[0].close < last[1].close < last[2].close
        falling = last[0].close > last[1].close > last[2].close
        midpoint = (quote.bid + quote.ask) / 2
        direction = "BULLISH" if midpoint > data.vwap and rising else (
            "BEARISH" if midpoint < data.vwap and falling else "NEUTRAL")
        directions.append(direction)
        components[symbol] = {"direction": direction, "price": midpoint, "vwap": data.vwap,
                              "last_closed_at": last[-1].timestamp.isoformat(),
                              "quote_at": quote.timestamp.isoformat()}
    if blockers:
        return RegimeResult("UNKNOWN", tuple(blockers), components)
    state = directions[0] if len(set(directions)) == 1 else "NEUTRAL"
    return RegimeResult(state, (), components)
