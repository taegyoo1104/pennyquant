"""수신 시각 순서대로 시장 자료를 쌓고 기술적 매매 후보를 평가한다."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math

from ..events import parse_event
from ..models import Bar, Quote, jsonable, utc
from ..scanners.relative_volume import relative_volume
from .bars import available_minutes, indicators
from .regime import RegimeResult, classify_regime
from .scanner import InstrumentEligibility, scan, score_candidate
from .strategies import TechnicalSignal, technical_signal
from .universe import UniverseStore


@dataclass(frozen=True)
class MarketDecision:
    symbol: str
    eligible: bool
    blockers: tuple[str, ...]
    score: float
    regime: str
    strategy: str
    signal: TechnicalSignal | None
    indicators: dict

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "eligible": self.eligible, "blockers": list(self.blockers),
                "score": self.score, "regime": self.regime, "strategy": self.strategy,
                "signal": self.signal.to_dict() if self.signal else None, "indicators": self.indicators,
                "readiness": "READY" if self.signal and self.eligible else "WATCH"}


class MarketEngine:
    def __init__(self, universe: UniverseStore, strategy: str = "pullback", *,
                 max_quote_age_seconds: float = 30, max_eligibility_age_seconds: float = 60,
                 max_spread_pct: float = .003, min_dollar_volume: float = 2_000_000,
                 min_rvol: float = 1.0, allow_neutral: bool = False, use_market_filter: bool = True):
        if strategy not in {"momentum", "pullback", "gap_breakout", "vwap"}:
            raise ValueError("지원하지 않는 전략입니다")
        self.universe, self.strategy = universe, strategy
        for name, value in (("max_quote_age_seconds", max_quote_age_seconds),
                            ("max_eligibility_age_seconds", max_eligibility_age_seconds),
                            ("max_spread_pct", max_spread_pct), ("min_dollar_volume", min_dollar_volume),
                            ("min_rvol", min_rvol)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 설정은 양의 유한 수여야 합니다")
            setattr(self, name, value)
        self.allow_neutral = allow_neutral
        if type(use_market_filter) is not bool or type(allow_neutral) is not bool:
            raise ValueError("시장 필터·중립장 허용 설정은 불리언이어야 합니다")
        # 시장 필터 해제는 과거 자료 비교 실험에서만 호출자가 사용한다.
        self.use_market_filter = use_market_filter
        self.bars: dict[str, list[Bar]] = {}
        self.quotes: dict[str, Quote] = {}
        self.eligibility: dict[str, InstrumentEligibility] = {}
        self.rvol: dict[str, dict] = {}
        self.sector_strength: dict[str, tuple[float, datetime]] = {}

    def on_bar(self, bar: Bar) -> None:
        if bar.received_at < bar.timestamp:
            raise ValueError("확정봉의 수신 시각이 봉 종료 시각보다 빠릅니다")
        values = self.bars.setdefault(bar.symbol, [])
        signature = (bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume)
        for existing in reversed(values):
            if (existing.timestamp, existing.open, existing.high, existing.low, existing.close, existing.volume) == signature:
                return
        values.append(bar)
        # 20거래일 RVOL 비교 이력을 유지하되 오래된 원본은 DB에서 보존한다.
        cutoff = bar.received_at - timedelta(days=50)
        self.bars[bar.symbol] = [x for x in values if x.timestamp >= cutoff][-20000:]

    def on_quote(self, quote: Quote) -> None:
        if quote.received_at < quote.timestamp:
            raise ValueError("호가 수신 시각이 호가 발생 시각보다 빠릅니다")
        previous = self.quotes.get(quote.symbol)
        if previous is None or (quote.timestamp, quote.received_at) >= (previous.timestamp, previous.received_at):
            self.quotes[quote.symbol] = quote

    def set_eligibility(self, symbol: str, eligibility: InstrumentEligibility) -> None:
        if symbol != eligibility.symbol:
            raise ValueError("종목 적격성 티커가 일치하지 않습니다")
        previous = self.eligibility.get(symbol)
        if previous is None or eligibility.checked_at >= previous.checked_at:
            self.eligibility[symbol] = eligibility

    def set_rvol(self, symbol: str, value: float | None, observed_at: datetime, *, verified: bool = False) -> None:
        """외부 RVOL은 동일 시각·기업행동 비교 가능성을 확인한 경우만 사용한다."""
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or value < 0):
            raise ValueError("RVOL은 유한한 음이 아닌 수 또는 null이어야 합니다")
        if type(verified) is not bool:
            raise ValueError("RVOL 검증 여부는 불리언이어야 합니다")
        observed_at = utc(observed_at)
        previous = self.rvol.get(symbol)
        if previous is None or observed_at >= previous["observed_at"]:
            self.rvol[symbol] = {"value": value, "observed_at": observed_at, "verified": verified}

    def calculate_rvol(self, symbol: str, now: datetime, *, comparability_verified: bool = False,
                       min_sessions: int = 20) -> float | None:
        current = available_minutes(self.bars.get(symbol, []), now, symbol)
        data = indicators(self.bars.get(symbol, []), now)
        if not current or data.vwap is None:
            return None
        through = data.five[-1].timestamp
        total = sum(bar.volume for bar in current if bar.timestamp <= through)
        return relative_volume(total, through, self.bars.get(symbol, []), symbol=symbol,
                               min_sessions=min_sessions, corporate_actions_verified=comparability_verified)

    def regime(self, now: datetime) -> RegimeResult:
        return classify_regime(self.bars, self.quotes, now, max_quote_age_seconds=self.max_quote_age_seconds)

    def evaluate(self, symbol: str, now: datetime, *, news_score: float = .5,
                 news_blocked: bool = False) -> MarketDecision:
        now = utc(now)
        data = indicators(self.bars.get(symbol, []), now)
        regime = self.regime(now)
        observed = self.rvol.get(symbol, {})
        rvol = observed.get("value") if observed.get("verified") and (
            observed["observed_at"] <= now and (now - observed["observed_at"]).total_seconds() <= 330) else None
        eligibility, quote = self.eligibility.get(symbol), self.quotes.get(symbol)
        if rvol is None:
            rvol = self.calculate_rvol(symbol, now, comparability_verified=bool(
                eligibility and eligibility.corporate_action is False and eligibility.volume_history_verified is True
                and eligibility.checked_at <= now
                and (now - eligibility.checked_at).total_seconds() <= self.max_eligibility_age_seconds))
        blockers = scan(self.universe, symbol, quote, eligibility, data, now, relative_volume=rvol,
                        max_quote_age_seconds=self.max_quote_age_seconds,
                        max_eligibility_age_seconds=self.max_eligibility_age_seconds,
                        max_spread_pct=self.max_spread_pct, min_dollar_volume=self.min_dollar_volume,
                        min_rvol=self.min_rvol)
        if self.use_market_filter and (regime.state in {"BEARISH", "UNKNOWN"}
                                       or (regime.state == "NEUTRAL" and not self.allow_neutral)):
            blockers.append(f"market_{regime.state.lower()}")
        if news_blocked:
            blockers.append("confirmed_news_entry_restriction")
        sector = self.sector_strength.get(symbol)
        sector_value = sector[0] if sector and sector[1] <= now and (now - sector[1]).total_seconds() <= 330 else None
        score, components = score_candidate(data, quote, regime.state, rvol, sector_strength=sector_value,
                                             news_score=news_score, max_spread_pct=self.max_spread_pct)
        signal = technical_signal(symbol, self.strategy, data, regime.state if self.use_market_filter else "BULLISH", now,
                                  allow_neutral=self.allow_neutral) if not blockers else None
        indicator_values = data.to_dict() | {"relative_volume": rvol, "score_components": components,
                                            "sector_strength_known": sector_value is not None,
                                            "market_filter_enabled": self.use_market_filter}
        return MarketDecision(symbol, not blockers, tuple(blockers), score, regime.state,
                              self.strategy, signal, indicator_values)

    def to_state(self, *, include_bars: bool = True) -> dict:
        """복구 자료의 시각을 유지한다. DB의 원본 봉을 참조할 때 봉 내역을 생략할 수 있다."""
        return jsonable({"schema_version": 1, "bars": self.bars if include_bars else {},
                         "quotes": self.quotes, "eligibility": self.eligibility,
                         "rvol": self.rvol, "sector_strength": self.sector_strength})

    def restore(self, payload: dict) -> None:
        if payload.get("schema_version") != 1:
            raise ValueError("지원하지 않는 시장 상태 복구 형식입니다")
        self.bars, self.quotes, self.eligibility, self.rvol, self.sector_strength = {}, {}, {}, {}, {}
        for symbol, rows in payload.get("bars", {}).items():
            for row in rows:
                if row.get("symbol") != symbol:
                    raise ValueError("복구 봉의 티커가 일치하지 않습니다")
                self.on_bar(parse_event({"kind": "bar", "data": row}).data)
        for symbol, row in payload.get("quotes", {}).items():
            if row.get("symbol") != symbol:
                raise ValueError("복구 호가의 티커가 일치하지 않습니다")
            self.on_quote(parse_event({"kind": "quote", "data": row}).data)
        for symbol, row in payload.get("eligibility", {}).items():
            self.set_eligibility(symbol, InstrumentEligibility(**{**row, "checked_at": utc(row["checked_at"])}))
        for symbol, row in payload.get("rvol", {}).items():
            self.set_rvol(symbol, row["value"], utc(row["observed_at"]), verified=row["verified"])
        for symbol, value in payload.get("sector_strength", {}).items():
            self.sector_strength[symbol] = (float(value[0]), utc(value[1]))
