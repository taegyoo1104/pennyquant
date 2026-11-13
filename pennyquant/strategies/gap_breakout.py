from __future__ import annotations

from datetime import time
from statistics import mean

from ..models import Signal
from .base import NEW_YORK, StrategyContext, prepare, signal


class GapBreakoutStrategy:
    name = "gap_breakout"

    def evaluate(self, context: StrategyContext) -> Signal | None:
        prepared = prepare(context, 6)
        if prepared is None:
            return None
        bars, news = prepared
        opening = bars[:5]
        expected = [time(9, minute) for minute in range(31, 36)]
        if [bar.timestamp.astimezone(NEW_YORK).time().replace(tzinfo=None) for bar in opening] != expected:
            return None
        previous_close = context.snapshot.prev_close
        if previous_close <= 0 or opening[0].open / previous_close - 1 < context.settings.min_gain_pct:
            return None
        range_high = max(bar.high for bar in opening)
        baseline_volume = mean(bar.volume for bar in opening)
        latest, previous = bars[-1], bars[-2]
        if baseline_volume <= 0:
            return None
        if not (
            previous.close <= range_high < latest.close <= range_high * 1.02
            and latest.volume >= baseline_volume * 1.5
        ):
            return None
        return signal(self.name, context, latest, news, "positive news; >=10% opening gap; first-five-minute range breakout; >=1.5x volume")
