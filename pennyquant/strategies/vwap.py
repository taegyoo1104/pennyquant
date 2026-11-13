from __future__ import annotations

from statistics import mean

from ..models import Signal
from .base import StrategyContext, prepare, session_vwap, signal


class VWAPStrategy:
    name = "vwap"

    def evaluate(self, context: StrategyContext) -> Signal | None:
        prepared = prepare(context, 6)
        if prepared is None:
            return None
        bars, news = prepared
        latest, previous = bars[-1], bars[-2]
        previous_vwap, current_vwap = session_vwap(bars[:-1]), session_vwap(bars)
        baseline_volume = mean(bar.volume for bar in bars[-4:-1])
        if previous_vwap is None or current_vwap is None or baseline_volume <= 0:
            return None
        prior_strength = max(bar.high for bar in bars[:-2]) / bars[0].open - 1
        if not (
            prior_strength >= 0.05
            and previous.close < previous_vwap
            and latest.close > current_vwap
            and latest.close > previous.high
            and latest.volume >= baseline_volume * 1.3
            and latest.close <= current_vwap * 1.05
        ):
            return None
        return signal(self.name, context, latest, news, "positive news; prior >=5% advance; VWAP reclaim and prior-high break; >=1.3x volume")
