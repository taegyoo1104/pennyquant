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
                     opening_range_minutes: int = 15) -> TechnicalSignal | None:
    if strategy not in {"momentum", "pullback", "gap_breakout", "vwap"}:
        raise ValueError("지원하지 않는 전략입니다")
    if opening_range_minutes not in (15, 30):
        raise ValueError("Opening Range는 15분 또는 30분이어야 합니다")
    if isinstance(max_chase_atr,bool) or not isinstance(max_chase_atr,(float,int)) or not math.isfinite(max_chase_atr) or max_chase_atr <= 0:
        raise ValueError('추격 제한 ATR 배수는 양의 유한 수여야 합니다.')
    now = utc(now)
    bounds = session_bounds(now)
    if not bounds or not bounds[0] < now < bounds[1]:
        return None
    if regime != "BULLISH" and not (allow_neutral and regime == "NEUTRAL"):
        return None
    bars = list(data.five)
    # 상위 주기와 이전 봉도 해당 시점에 수신·확정된 같은 종목이어야 한다.
    for group,period in ((bars,5),(data.fifteen,15)):
        if any(b.symbol != symbol or not bounds[0] < b.timestamp <= b.received_at <= now
               or (b.timestamp-bounds[0]).total_seconds() % (period*60) for b in group):
            return None
    if (data.calculated_at is None or not bars or utc(data.calculated_at) > now
            or data.vwap_through != bars[-1].timestamp
            or any(b.timestamp > bars[-1].timestamp for b in data.fifteen)):
        return None
    if any(isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value <= 0
           for value in (data.vwap,data.ema9,data.atr14,data.volume_ratio)):
        return None
    if len(bars) < 15 or not contiguous(bars, 5) or data.vwap is None or data.ema9 is None or not data.atr14:
        return None
    last, previous = bars[-1], bars[-2]
    if not (last.timestamp <= last.received_at <= utc(now)) or (utc(now) - last.timestamp).total_seconds() > 330:
        return None
    atr_value = data.atr14
    if last.close <= 0 or atr_value <= 0 or last.close - max(data.vwap, data.ema9) > max_chase_atr * atr_value:
        return None
    if data.volume_ratio is None or data.volume_ratio < (1.2 if regime == "NEUTRAL" else 1.0):
        return None
    reason, support, allow_add = "", min(last.low, previous.low), False
    if strategy == "momentum":
        if (rising_structure(data) and last.close > max(data.vwap, data.ema9)
                and last.close > max(bar.high for bar in bars[-5:-1]) and data.volume_ratio >= 1.2):
            reason = "시장 강세·15분 상승 구조·5분 고점 돌파와 거래량 증가"
    elif strategy == "pullback":
        first, second, rebound = bars[-3:]
        prior = bars[-4]
        retracement = max(b.high for b in bars[-6:-3]) - min(first.low, second.low)
        if (rising_structure(data) and first.close < prior.close and second.close <= first.close
                and min(abs(min(first.low, second.low)-level) for level in (data.vwap,data.ema9)) <= atr_value
                and 0 < retracement <= 2.5 * atr_value and rebound.close > second.high
                and rebound.close > data.vwap and rebound.volume > second.volume):
            reason = "15분 상승 추세에서 두 개 조정봉·지지 확인 후 재상승"
            support, allow_add = min(first.low, second.low), True
    elif strategy == "gap_breakout":
        bounds = session_bounds(now)
        opening_end = bounds[0] + timedelta(minutes=opening_range_minutes) if bounds else None
        opening = [bar for bar in bars if opening_end and bar.timestamp <= opening_end]
        if (len(opening) == opening_range_minutes // 5 and last.timestamp > opening_end
                and previous.close <= max(bar.high for bar in opening) < last.close
                and last.close > data.vwap and data.volume_ratio >= 1.2):
            reason = f"장 시작 {opening_range_minutes}분 범위 상단 돌파·VWAP·거래량 확인"
            support = max(data.vwap, min(last.low, previous.low))
    elif strategy == "vwap":
        # 한 봉의 순간 돌파만으로는 진입하지 않고 재지지 또는 직전 고가 돌파를 확인한다.
        if (data.previous_vwap is not None and math.isfinite(data.previous_vwap) and data.previous_vwap > 0
                and data.previous_vwap_through == previous.timestamp
                and previous.close < data.previous_vwap and last.close > data.vwap
                and last.close > previous.high
                and last.close > data.ema9 and rising_structure(data)):
            reason = "VWAP 재돌파와 직전 고가 돌파·상위 주기 상승 확인"
            support, allow_add = min(last.low, previous.low), True
    if not reason:
        return None
    stop = min(support - atr_value * .1, last.close - atr_value)
    if stop <= 0 or stop >= last.close:
        return None
    return TechnicalSignal(symbol, strategy, last.timestamp, last.close, stop, reason, allow_add)
