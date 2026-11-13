from __future__ import annotations

from statistics import mean

from ..models import Signal
from .base import StrategyContext, prepare, session_vwap, signal


class MomentumStrategy:
    name = "momentum"

    def evaluate(self, context: StrategyContext) -> Signal | None:
        prepared = prepare(context, 4)
        if prepared is None:
            return None
        bars, news = prepared
        latest, previous = bars[-1], bars[-2]
        vwap = session_vwap(bars)
        baseline_volume = mean(bar.volume for bar in bars[-4:-1])
        if vwap is None or baseline_volume <= 0:
            return None
        if not (
            latest.close > previous.high
            and previous.close > bars[-3].close
            and latest.volume >= baseline_volume * 1.5
            and latest.close >= vwap
            and latest.close <= vwap * 1.12
        ):
            return None
        return signal(self.name, context, latest, news, "positive news; rising closes; prior-high breakout; volume >= 1.5x; above VWAP")
