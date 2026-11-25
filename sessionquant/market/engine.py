"""수신 시각 순서대로 시장 자료를 쌓고 기술적 매매 후보를 평가한다."""
from __future__ import annotations

from contextlib import contextmanager
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
                 min_rvol: float = 1.0, allow_neutral: bool = False, use_market_filter: bool = True, strict_data: bool = False,
                 paper_data_policy='strict'):
        if strategy not in {"momentum", "pullback", "pullback_simple", "gap_breakout", "vwap"}:
            raise ValueError("지원하지 않는 전략입니다")
        self.universe, self.strategy = universe, strategy
        self.strict_data = strict_data
        from .paper_policy import validate_policy, OBSERVED
        self.paper_data_policy = validate_policy(paper_data_policy)
        if paper_data_policy == OBSERVED and not strict_data:
            raise ValueError('실데이터 Paper 정책은 엄격한 시세·분봉 검사를 유지해야 합니다.')
        self.quality_rows = {}
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
        # 시장 필터 적용 여부는 별도 Paper 설정 또는 과거 자료 비교에서 명시한다.
        self.use_market_filter = use_market_filter
        self.bars: dict[str, list[Bar]] = {}
        self.quotes: dict[str, Quote] = {}
        self.quote_conflicts: dict[str, datetime] = {}
        self.eligibility: dict[str, InstrumentEligibility] = {}
        self.rvol: dict[str, dict] = {}
        self.sector_strength: dict[str, tuple[float, datetime]] = {}
        self.history_provider = None
        self._regime_cache = None
        self._quote_scope = None

    def on_bar(self, bar: Bar) -> None:
        if bar.received_at < bar.timestamp:
            raise ValueError("확정봉의 수신 시각이 봉 종료 시각보다 빠릅니다")
        values = self.bars.setdefault(bar.symbol, [])
        from .candle_revisions import retain_observation
        if not retain_observation([b for b in values if b.timestamp == bar.timestamp], bar):
            return
        values.append(bar)
        if bar.symbol in {'SPY', 'QQQ'} and self._regime_cache is not None:
            self._regime_cache.clear()
        # 20거래일 RVOL 비교 이력을 유지하되 오래된 원본은 DB에서 보존한다.
        cutoff = bar.received_at - timedelta(days=50)
        self.bars[bar.symbol] = [x for x in values if x.timestamp >= cutoff][-20000:]

    def on_quote(self, quote: Quote) -> None:
        if quote.received_at < quote.timestamp:
            raise ValueError("호가 수신 시각이 호가 발생 시각보다 빠릅니다")
        if self.quote_is_conflicted(quote):
            return
        if self._quote_scope is not None:
            key = (quote.symbol, quote.timestamp)
            observed = self._quote_scope.get(key)
            if observed is not None and self._book_signature(quote) != self._book_signature(observed):
                self._invalidate_quote(quote)
                return
            self._quote_scope.setdefault(key, quote)
        previous = self.quotes.get(quote.symbol)
        if previous is not None:
            if quote.timestamp < previous.timestamp:
                return
            if quote.timestamp == previous.timestamp:
                if self._book_signature(quote) == self._book_signature(previous):
                    # A repeated book does not renew its original receipt time.
                    return
                self._invalidate_quote(quote)
                return
        self.quotes[quote.symbol] = quote
        if quote.symbol in {'SPY', 'QQQ'} and self._regime_cache is not None:
            self._regime_cache.clear()

    @staticmethod
    def _book_signature(quote: Quote):
        return quote.bid, quote.ask, quote.bid_size, quote.ask_size, quote.halted

    def _invalidate_quote(self, quote: Quote) -> None:
        conflict = max(quote.timestamp, self.quote_conflicts.get(quote.symbol, quote.timestamp))
        self.quote_conflicts[quote.symbol] = conflict
        current = self.quotes.get(quote.symbol)
        if current is not None and current.timestamp <= conflict:
            self.quotes.pop(quote.symbol, None)
        if quote.symbol in {'SPY', 'QQQ'} and self._regime_cache is not None:
            self._regime_cache.clear()

    def quote_is_conflicted(self, quote: Quote) -> bool:
        conflict = self.quote_conflicts.get(quote.symbol)
        return conflict is not None and quote.timestamp <= conflict

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

    @contextmanager
    def evaluation_scope(self):
        """Share benchmark work only within one serialized engine transaction."""
        previous, self._regime_cache = self._regime_cache, {}
        previous_quotes = self._quote_scope
        self._quote_scope = {} if previous_quotes is None else previous_quotes
        try:
            yield
        finally:
            if previous is not None:
                previous.clear()
            self._regime_cache = previous
            self._quote_scope = previous_quotes

    def regime(self, now: datetime) -> RegimeResult:
        key = (utc(now), self.max_quote_age_seconds)
        if self._regime_cache is not None and key in self._regime_cache:
            return self._regime_cache[key]
        result = classify_regime(self.bars, self.quotes, now, max_quote_age_seconds=self.max_quote_age_seconds)
        if self._regime_cache is not None:
            self._regime_cache.clear()
            self._regime_cache[key] = result
        return result

    def evaluate(self, symbol: str, now: datetime, *, news_score: float = .5,
                 news_blocked: bool = False) -> MarketDecision:
        now = utc(now)
        data = indicators(self.bars.get(symbol, []), now)
        regime = self.regime(now)
        observed = self.rvol.get(symbol, {})
        rvol = observed.get("value") if observed.get("verified") and (
            observed["observed_at"] <= now and (now - observed["observed_at"]).total_seconds() <= 330) else None
        eligibility, quote = self.eligibility.get(symbol), self.quotes.get(symbol)
        from .paper_policy import OBSERVED, paper_eligibility
        experimental = self.paper_data_policy == OBSERVED
        paper = paper_eligibility(eligibility,now) if experimental else None
        quality = None
        if self.strict_data:
            from .quality import bar_quality, rvol_report
            from .eligibility import field_verified
            bars = self.bars.get(symbol, [])
            if self.history_provider is not None:
                bars = [*self.history_provider(symbol,now),*bars]
            cutoff = data.five[-1].timestamp if data.five else None
            volume = rvol_report(bars,symbol,cutoff,now,
                                 evidence=eligibility.volume_evidence if eligibility else None)
            rvol = volume['value']
            if (experimental and rvol is None and volume['calculation_status']=='CALCULATED'
                    and set(volume['reasons']) == {'split_volume_comparability_unknown'}):
                rvol = volume['calculated_value']
            valid_eligibility = bool(eligibility and eligibility.symbol == symbol and
                eligibility.checked_at <= now and (now-eligibility.checked_at).total_seconds() <= self.max_eligibility_age_seconds
                and eligibility.valid_until and eligibility.valid_until >= now and
                all(field_verified(eligibility,name,expected,now) and getattr(eligibility,name) is expected
                    for name,expected in [('tradable',True),('fractional',True),('halted',False),('corporate_action',False)]))
            current_quality = bar_quality(bars,symbol,now)
            strategy_quality = (bar_quality(bars,symbol,now,through=cutoff) if cutoff is not None else
                {**current_quality,'status':'UNKNOWN','reasons':['current_closed_interval_missing']})
            quality = {'symbol':symbol,'observed_at':now.isoformat(), 'bars':current_quality,
                'strategy_bars':strategy_quality,
                'collection_warnings':list(current_quality['reasons']),
                'rvol':volume, 'eligibility_verified':valid_eligibility,
                'paper_data_policy':self.paper_data_policy, 'paper_eligibility':paper,
                'strategy_rvol':rvol, 'strategy_rvol_status':('EXPERIMENTAL_UNVERIFIED' if rvol is not None and volume['status']!='VERIFIED'
                                                             else volume['status']),
                'eligibility_max_age_seconds':self.max_eligibility_age_seconds,
                'eligibility':jsonable(eligibility) if eligibility else None,
                'quote_fresh':bool(quote and quote.symbol == symbol and quote.timestamp <= quote.received_at <= now
                    and all(0 <= (now-t).total_seconds() <= self.max_quote_age_seconds for t in [quote.timestamp,quote.received_at])),
                'rvol_required':self.strategy != 'pullback_simple',
                'quote_at':quote.timestamp.isoformat() if quote else None,
                'quote_received_at':quote.received_at.isoformat() if quote else None}
        elif rvol is None:
            rvol = self.calculate_rvol(symbol, now, comparability_verified=bool(
                eligibility and eligibility.corporate_action is False and eligibility.volume_history_verified is True
                and eligibility.checked_at <= now
                and (now - eligibility.checked_at).total_seconds() <= self.max_eligibility_age_seconds))
        blockers = scan(self.universe, symbol, quote, eligibility, data, now, relative_volume=rvol,
                        max_quote_age_seconds=self.max_quote_age_seconds,
                        max_eligibility_age_seconds=self.max_eligibility_age_seconds,
                        max_spread_pct=self.max_spread_pct, min_dollar_volume=self.min_dollar_volume,
                        min_rvol=self.min_rvol, require_evidence=self.strict_data, paper_assumptions=experimental, require_rvol=self.strategy != "pullback_simple")
        if quote is None and symbol in self.quote_conflicts:
            blockers.append('quote_timestamp_conflict')
        from ..market_reference.contracts import REFERENCE_SYMBOLS
        if symbol in REFERENCE_SYMBOLS:
            blockers.append('market_reference_not_trading_candidate')
        if quality and quality['strategy_bars']['status'] != 'COMPLETE':
            blockers.append('minute_history_incomplete_or_stale')
        if self.use_market_filter and (regime.state in {"BEARISH", "UNKNOWN"}
                                       or (regime.state == "NEUTRAL" and not self.allow_neutral)):
            blockers.append(f"market_{regime.state.lower()}")
        if news_blocked:
            blockers.append("confirmed_news_entry_restriction")
        sector = self.sector_strength.get(symbol)
        sector_value = sector[0] if sector and sector[1] <= now and (now - sector[1]).total_seconds() <= 330 else None
        score, components = score_candidate(data, quote, regime.state, rvol, sector_strength=sector_value,
                                             news_score=news_score, max_spread_pct=self.max_spread_pct)
        strategy_diagnostics = []
        signal = technical_signal(symbol, self.strategy, data, regime.state if self.use_market_filter else "BULLISH", now,
                                  allow_neutral=self.allow_neutral,
                                  diagnostics=strategy_diagnostics) if not blockers else None
        indicator_values = data.to_dict() | {"relative_volume": rvol, "score_components": components,
                                            'paper_data_policy':self.paper_data_policy, 'paper_eligibility':paper,
                                            "sector_strength_known": sector_value is not None,
                                            "market_filter_enabled": self.use_market_filter,
                                            'strategy_diagnostics':strategy_diagnostics,
                                            'strategy_status':'BLOCKED' if blockers else 'SIGNAL' if signal else 'WAITING'}
        if self.strategy == 'pullback_simple':
            from .simple_pullback import inspect_pattern, progress
            indicator_values['strategy_pattern'] = inspect_pattern(data)
            indicator_values['strategy_progress'] = progress(data)
            indicator_values['rvol_required'] = False
        if quality is not None:
            quality['blockers'] = list(dict.fromkeys(blockers))
            self.quality_rows[symbol] = quality
            indicator_values['data_quality'] = quality
        return MarketDecision(symbol, not blockers, tuple(blockers), score, regime.state,
                              self.strategy, signal, indicator_values)

    def to_state(self, *, include_bars: bool = True) -> dict:
        """복구 자료의 시각을 유지한다. DB의 원본 봉을 참조할 때 봉 내역을 생략할 수 있다."""
        return jsonable({"schema_version": 1, "bars": self.bars if include_bars else {},
                         "quotes": self.quotes, "quote_conflicts": self.quote_conflicts, "eligibility": self.eligibility,
                         "rvol": self.rvol, "sector_strength": self.sector_strength})

    def restore(self, payload: dict) -> None:
        if self._regime_cache is not None:
            self._regime_cache.clear()
        if payload.get("schema_version") != 1:
            raise ValueError("지원하지 않는 시장 상태 복구 형식입니다")
        self.bars, self.quotes, self.eligibility, self.rvol, self.sector_strength = {}, {}, {}, {}, {}
        self.quote_conflicts = {symbol: utc(stamp) for symbol, stamp in payload.get('quote_conflicts', {}).items()}
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
