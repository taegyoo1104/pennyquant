"""종목 적격성의 미확인을 통과로 바꾸지 않는 S&P 500 스캐너."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math

from ..models import Quote, symbol_valid, utc
from .bars import Indicators
from .universe import UniverseStore

EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "NYSEAMERICAN", "NYSE AMERICAN", "ARCA", "NYSEARCA"}
SCORE_WEIGHTS = {"trend": .25, "rvol": .20, "market": .20, "sector": .15, "liquidity": .10, "news": .10}


@dataclass(frozen=True)
class InstrumentEligibility:
    symbol: str
    checked_at: datetime
    source: str
    tradable: bool | None = None
    fractional: bool | None = None
    halted: bool | None = None
    corporate_action: bool | None = None
    exchange: str = "UNKNOWN"
    volume_history_verified: bool | None = None
    valid_until: datetime | None = None
    account_scope: str = "UNKNOWN"
    field_evidence: dict = field(default_factory=dict)
    volume_evidence: dict = field(default_factory=dict)

    def __post_init__(self):
        symbol_valid(self.symbol)
        object.__setattr__(self, 'checked_at', utc(self.checked_at))
        if self.valid_until is not None:
            object.__setattr__(self, 'valid_until', utc(self.valid_until))
            if self.valid_until < self.checked_at:
                raise ValueError('적격성 만료 시각이 확인 시각보다 빠릅니다.')
        if not isinstance(self.field_evidence, dict) or not isinstance(self.volume_evidence, dict):
            raise ValueError('적격성·거래량 증거는 객체여야 합니다.')
        if not isinstance(self.account_scope, str) or not self.account_scope:
            raise ValueError('계좌 검증 범위가 필요합니다.')
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("종목 적격성 확인 출처가 필요합니다")
        for name in ("tradable", "fractional", "halted", "corporate_action", "volume_history_verified"):
            if getattr(self, name) is not None and type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} 값은 true·false·null이어야 합니다")


def scan(universe: UniverseStore, symbol: str, quote: Quote | None, eligibility: InstrumentEligibility | None,
         data: Indicators, now: datetime, *, relative_volume: float | None = None,
         max_quote_age_seconds: float = 30, max_eligibility_age_seconds: float = 60,
         max_spread_pct: float = .003, min_dollar_volume: float = 2_000_000,
         min_rvol: float = 1.0, require_evidence: bool = False, paper_assumptions: bool = False) -> list[str]:
    """진입 차단 사유를 모두 반환한다. 매도 가능 여부는 별도로 관리한다."""
    now = utc(now)
    blockers = []
    if not universe.contains(symbol, now):
        blockers.append("universe_unknown_stale_or_nonmember")
    if paper_assumptions:
        from .paper_policy import paper_eligibility
        blockers.extend(paper_eligibility(eligibility,now)['blockers'])
        if eligibility is not None and eligibility.symbol != symbol:
            blockers.append('instrument_symbol_mismatch')
    elif eligibility is None:
        blockers.append("instrument_eligibility_unknown")
    else:
        if eligibility.symbol != symbol:
            blockers.append('instrument_symbol_mismatch')
        if eligibility.checked_at > now or (now - eligibility.checked_at).total_seconds() > max_eligibility_age_seconds:
            blockers.append("instrument_eligibility_stale_or_future")
        if eligibility.valid_until is not None and eligibility.valid_until < now:
            blockers.append('instrument_eligibility_expired')
        if require_evidence and (eligibility.valid_until is None or eligibility.account_scope == 'UNKNOWN'):
            blockers.append('eligibility_unverified')
        for name, expected in (("tradable", True), ("fractional", True), ("halted", False), ("corporate_action", False)):
            if getattr(eligibility, name) is not expected:
                blockers.append(f"instrument_{name}_unverified_or_restricted")
            if require_evidence:
                from .eligibility import field_verified
                if not field_verified(eligibility, name, expected, now):
                    blockers.append('eligibility_unverified')
        if eligibility.exchange.upper() not in EXCHANGES:
            blockers.append("exchange_unverified")
    if quote is None:
        blockers.append("quote_missing")
    else:
        if quote.symbol != symbol or quote.timestamp > quote.received_at or quote.received_at > now:
            blockers.append("quote_time_or_symbol_invalid")
        elif any((now - t).total_seconds() > max_quote_age_seconds for t in (quote.timestamp, quote.received_at)):
            blockers.append("quote_stale")
        if quote.halted:
            blockers.append("quote_halted")
        if quote.bid_size <= 0 or quote.ask_size <= 0:
            blockers.append("quote_depth_missing")
        if quote.spread_pct > max_spread_pct:
            blockers.append("spread_too_wide")
    if not data.five or (now - data.five[-1].timestamp).total_seconds() > 330:
        blockers.append("closed_bars_missing_or_stale")
    if data.vwap is None or data.ema9 is None or data.atr14 is None:
        blockers.append("technical_history_incomplete")
    if data.cumulative_volume < 10_000 or data.dollar_volume < min_dollar_volume:
        blockers.append("insufficient_liquidity")
    if relative_volume is None or not math.isfinite(relative_volume) or relative_volume < min_rvol:
        blockers.append("relative_volume_unknown_or_low")
    if require_evidence and any(code.startswith('instrument_') or code == 'exchange_unverified' for code in blockers):
        blockers.append('eligibility_unverified')
    return list(dict.fromkeys(blockers))


def score_candidate(data: Indicators, quote: Quote | None, regime: str, relative_volume: float | None,
                    *, sector_strength: float | None = None, news_score: float = .5,
                    max_spread_pct: float = .003) -> tuple[float, dict]:
    """초기 가중치는 실험 설정이다. 업종 미확인은 점수를 임의 생성하지 않는다."""
    def bounded(value):
        return max(0., min(1., float(value))) if isinstance(value, (int, float)) and math.isfinite(value) else 0.
    last = data.five[-1].close if data.five else None
    trend = 0.
    if last and data.vwap and data.ema9 and data.atr14:
        trend = bounded((last - max(data.vwap, data.ema9)) / (2 * data.atr14))
    scores = {"trend": trend, "rvol": bounded((relative_volume or 0) / 2),
              "market": 1. if regime == "BULLISH" else .25 if regime == "NEUTRAL" else 0.,
              "sector": bounded(sector_strength),
              "liquidity": bounded(1 - quote.spread_pct / max_spread_pct) if quote else 0.,
              "news": bounded(news_score)}
    return round(sum(scores[key] * SCORE_WEIGHTS[key] for key in scores) * 100, 4), scores
