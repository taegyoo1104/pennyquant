"""확정 5분·15분봉에 기반한 네 가지 장중 롱 전략."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import math

from ..models import utc
from .bars import Indicators, contiguous, session_bounds


@dataclass(frozen=True)
class TechnicalSignal:
    symbol: str
    strategy: str
    timestamp: datetime
    reference_price: float
    stop_price: float
    reason: str
    allow_add: bool = False

    @property
    def id(self) -> str:
        return sha256(f"sessionquant:{self.symbol}:{self.strategy}:{utc(self.timestamp).isoformat()}".encode()).hexdigest()

    def to_dict(self) -> dict:
        return {"id": self.id, "symbol": self.symbol, "strategy": self.strategy,
                "timestamp": utc(self.timestamp).isoformat(), "reference_price": self.reference_price,
                "stop_price": self.stop_price, "reason": self.reason, "allow_add": self.allow_add}


def rising_structure(data: Indicators) -> bool:
    bars = list(data.fifteen[-3:])
    return (len(bars) == 3 and contiguous(bars, 15) and bars[0].close < bars[1].close < bars[2].close
            and bars[0].low <= bars[1].low <= bars[2].low)


def technical_signal(symbol: str, strategy: str, data: Indicators, regime: str, now: datetime,
                     *, allow_neutral: bool = False, max_chase_atr: float = 2.0,
                     opening_range_minutes: int = 15,
                     diagnostics: list[str] | None = None) -> TechnicalSignal | None:
    """Evaluate the unchanged entry rules and optionally explain missing conditions.

    Diagnostics describe strategy conditions only; eligibility and order/risk gates
    belong to their callers. A supplied list is appended to, never used as input.
    """
    def reject(code):
        if diagnostics is not None:
            diagnostics.append(code)
        return None

    def meets(requirements):
        missing = [code for passed, code in requirements if not passed]
        if diagnostics is not None:
            diagnostics.extend(missing)
        return not missing

    if strategy not in {"momentum", "pullback", "gap_breakout", "vwap"}:
        raise ValueError("지원하지 않는 전략입니다")
    if opening_range_minutes not in (15, 30):
        raise ValueError("Opening Range는 15분 또는 30분이어야 합니다")
    if isinstance(max_chase_atr,bool) or not isinstance(max_chase_atr,(float,int)) or not math.isfinite(max_chase_atr) or max_chase_atr <= 0:
        raise ValueError('추격 제한 ATR 배수는 양의 유한 수여야 합니다.')
    now = utc(now)
    bounds = session_bounds(now)
    if not bounds or not bounds[0] < now < bounds[1]:
        return reject("strategy_outside_regular_session")
    if regime != "BULLISH" and not (allow_neutral and regime == "NEUTRAL"):
        return reject("strategy_market_direction_blocked")
    bars = list(data.five)
    # 상위 주기와 이전 봉도 해당 시점에 수신·확정된 같은 종목이어야 한다.
    for group,period in ((bars,5),(data.fifteen,15)):
        if any(b.symbol != symbol or not bounds[0] < b.timestamp <= b.received_at <= now
               or (b.timestamp-bounds[0]).total_seconds() % (period*60) for b in group):
            return reject("strategy_bar_identity_or_time_invalid")
    if (data.calculated_at is None or not bars or utc(data.calculated_at) > now
            or data.vwap_through != bars[-1].timestamp
            or any(b.timestamp > bars[-1].timestamp for b in data.fifteen)):
        return reject("strategy_indicator_cutoff_mismatch")
    if any(isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value <= 0
           for value in (data.vwap,data.ema9,data.atr14,data.volume_ratio)):
        return reject("strategy_indicators_unavailable")
    if len(bars) < 15 or not contiguous(bars, 5) or data.vwap is None or data.ema9 is None or not data.atr14:
        return reject("strategy_five_minute_history_incomplete")
    last, previous = bars[-1], bars[-2]
    if not (last.timestamp <= last.received_at <= utc(now)) or (utc(now) - last.timestamp).total_seconds() > 330:
        return reject("strategy_closed_bar_stale")
    atr_value = data.atr14
    if last.close <= 0 or atr_value <= 0 or last.close - max(data.vwap, data.ema9) > max_chase_atr * atr_value:
        return reject("strategy_price_extended_or_invalid")
    if data.volume_ratio is None or data.volume_ratio < (1.2 if regime == "NEUTRAL" else 1.0):
        return reject("strategy_recent_volume_below_minimum")
    reason, support, allow_add = "", min(last.low, previous.low), False
    if strategy == "momentum":
        if meets(((rising_structure(data), "strategy_fifteen_minute_uptrend_missing"),
                  (last.close > max(data.vwap, data.ema9), "strategy_price_above_support_missing"),
                  (last.close > max(bar.high for bar in bars[-5:-1]), "strategy_recent_high_breakout_missing"),
                  (data.volume_ratio >= 1.2, "strategy_breakout_volume_missing"))):
            reason = "시장 강세·15분 상승 구조·5분 고점 돌파와 거래량 증가"
    elif strategy == "pullback":
        first, second, rebound = bars[-3:]
        prior = bars[-4]
        retracement = max(b.high for b in bars[-6:-3]) - min(first.low, second.low)
        if meets(((rising_structure(data), "strategy_fifteen_minute_uptrend_missing"),
                  (first.close < prior.close, "strategy_first_pullback_missing"),
                  (second.close <= first.close, "strategy_second_pullback_missing"),
                  (min(abs(min(first.low, second.low)-level) for level in (data.vwap,data.ema9)) <= atr_value,
                   "strategy_pullback_support_missing"),
                  (0 < retracement <= 2.5 * atr_value, "strategy_pullback_depth_invalid"),
                  (rebound.close > second.high, "strategy_rebound_high_breakout_missing"),
                  (rebound.close > data.vwap, "strategy_rebound_above_vwap_missing"),
                  (rebound.volume > second.volume, "strategy_rebound_volume_missing"))):
            reason = "15분 상승 추세에서 두 개 조정봉·지지 확인 후 재상승"
            support, allow_add = min(first.low, second.low), True
    elif strategy == "gap_breakout":
        bounds = session_bounds(now)
        opening_end = bounds[0] + timedelta(minutes=opening_range_minutes) if bounds else None
        opening = [bar for bar in bars if opening_end and bar.timestamp <= opening_end]
        opening_ready = len(opening) == opening_range_minutes // 5
        if meets(((opening_ready, "strategy_opening_range_incomplete"),
                  (opening_end is not None and last.timestamp > opening_end, "strategy_opening_range_not_finished"),
                  (bool(opening) and previous.close <= max(bar.high for bar in opening) < last.close,
                   "strategy_opening_range_breakout_missing"),
                  (last.close > data.vwap, "strategy_price_above_vwap_missing"),
                  (data.volume_ratio >= 1.2, "strategy_breakout_volume_missing"))):
            reason = f"장 시작 {opening_range_minutes}분 범위 상단 돌파·VWAP·거래량 확인"
            support = max(data.vwap, min(last.low, previous.low))
    elif strategy == "vwap":
        # 한 봉의 순간 돌파만으로는 진입하지 않고 재지지 또는 직전 고가 돌파를 확인한다.
        previous_vwap_ready = (data.previous_vwap is not None and math.isfinite(data.previous_vwap)
                               and data.previous_vwap > 0 and data.previous_vwap_through == previous.timestamp)
        if meets(((previous_vwap_ready, "strategy_previous_vwap_unavailable"),
                  (previous_vwap_ready and previous.close < data.previous_vwap and last.close > data.vwap,
                   "strategy_vwap_reclaim_missing"),
                  (last.close > previous.high, "strategy_rebound_high_breakout_missing"),
                  (last.close > data.ema9, "strategy_price_above_ema_missing"),
                  (rising_structure(data), "strategy_fifteen_minute_uptrend_missing"))):
            reason = "VWAP 재돌파와 직전 고가 돌파·상위 주기 상승 확인"
            support, allow_add = min(last.low, previous.low), True
    if not reason:
        return None
    stop = min(support - atr_value * .1, last.close - atr_value)
    if stop <= 0 or stop >= last.close:
        return reject("strategy_stop_price_invalid")
    return TechnicalSignal(symbol, strategy, last.timestamp, last.close, stop, reason, allow_add)
