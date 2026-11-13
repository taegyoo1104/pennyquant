from __future__ import annotations

from statistics import mean

from ..models import Signal
from .base import StrategyContext, prepare, session_vwap, signal


class PullbackStrategy:
    name = "pullback"

    def evaluate(self, context: StrategyContext) -> Signal | None:
        prepared = prepare(context, 7)
        if prepared is None:
            return None
        bars, news = prepared
        latest = bars[-1]
        # Two completed bars must follow the impulse peak before confirmation.
        peak_index = max(range(len(bars) - 3), key=lambda index: bars[index].high)
        peak = bars[peak_index]
        pullback = bars[peak_index + 1:-1]
        if len(pullback) < 2:
            return None
        trough = min(bar.low for bar in pullback)
        drawdown = (peak.high - trough) / peak.high
        impulse_low = min(bar.low for bar in bars[:peak_index + 1])
        vwap = session_vwap(bars)
        baseline_volume = mean(bar.volume for bar in pullback[-3:])
        if vwap is None or baseline_volume <= 0:
            return None
        # Reject a broken VWAP support at any observed pullback close, using only
        # the running VWAP available at that close.
        supports_hold = all(
            bar.close >= (session_vwap(bars[:index + 1]) or float("inf"))
            for index, bar in enumerate(bars)
            if peak_index < index < len(bars) - 1
        )
        if not (
            peak.high / impulse_low - 1 >= 0.05
            and 0.015 <= drawdown <= 0.08
            and supports_hold
            and latest.close > bars[-2].high
            and latest.close >= vwap
            and latest.close <= peak.high * 1.01
            and latest.volume >= baseline_volume * 1.3
        ):
            return None
        return signal(self.name, context, latest, news, "positive news; >=5% impulse; 1.5-8% pullback; VWAP support; rebound above prior high on >=1.3x volume")
