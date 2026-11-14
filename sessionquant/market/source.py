"""토스 읽기 전용 API에서 시장 확인용 자료를 수집한다.

StockInfo.status=ACTIVE는 상장 상태만 뜻한다. 공식 응답으로 확인하지 못한
미국 거래정지·소수점 지원·계좌 주문 가능성을 생성하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from time import monotonic

import httpx

from ..collectors.toss_market import TossMarketClient
from ..models import Bar, Quote, utc
from .bars import session_bounds
from .scanner import InstrumentEligibility


@dataclass
class MarketBatch:
    bars: list[Bar] = field(default_factory=list)
    quotes: list[Quote] = field(default_factory=list)
    eligibility: list[InstrumentEligibility] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    observed_at: datetime | None = None


class TossSessionSource:
    def __init__(self, client: TossMarketClient | None = None, *, candidates_per_cycle: int = 2, clock=None,
                 watchlist=None, max_api_calls=24, light_refresh_seconds=30, ranking_refresh_seconds=60):
        if type(candidates_per_cycle) is not int or not 1 <= candidates_per_cycle <= 10:
            raise ValueError("한 주기에 상세 수집할 종목 수는 1~10개입니다")
        self.client = client or TossMarketClient()
        self.owns_client = client is None
        self.candidates_per_cycle = candidates_per_cycle
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.cursor = 0
        self.watchlist = watchlist
        self.max_api_calls = max_api_calls
        self.light_refresh_seconds = light_refresh_seconds
        self.ranking_refresh_seconds = ranking_refresh_seconds
        if type(max_api_calls) is not int or not 8 <= max_api_calls <= 100:
            raise ValueError('주기별 시세 조회 예산은 8~100회여야 합니다.')
        self._metadata = {}
        self._metadata_at = {}
        self._seen: dict[str, set[tuple]] = {}

    async def _legacy_poll(self, symbols: list[str], now: datetime | None = None, *,
                   priority_symbols: tuple[str, ...] | list[str] = ()) -> MarketBatch:
        now = utc(now or self.clock())
        candidates = list(dict.fromkeys(symbol for symbol in symbols if symbol not in {"SPY", "QQQ"}))
        count = min(self.candidates_per_cycle, len(candidates))
        chosen = [candidates[(self.cursor + i) % len(candidates)] for i in range(count)] if candidates else []
        if candidates:
            self.cursor = (self.cursor + count) % len(candidates)
        selected = list(dict.fromkeys(["SPY", "QQQ", *priority_symbols, *chosen]))
        batch, metadata = MarketBatch(), {}
        try:
            response = await self.client.stocks(selected)
            rows = response if isinstance(response, list) else response.get("stocks", [])
            metadata = {row["symbol"]: row for row in rows if isinstance(row, dict) and row.get("symbol") in selected}
        except Exception as exc:
            batch.errors.append({"symbol": "*", "source": "toss_stocks", "error_type": type(exc).__name__,
                                 "message": "종목 마스터를 확인하지 못했습니다"})
        bounds = session_bounds(now)
        for symbol in selected:
            try:
                bars, before = await self.client.candles(symbol, count=200)
                # 장중 늦게 시작해도 당일 VWAP에 필요한 개장 이후 봉을 확인한다.
                if bounds and bars and min(b.timestamp for b in bars) > bounds[0] and before:
                    previous, _ = await self.client.candles(symbol, count=200, before=utc(before))
                    bars = previous + bars
                seen = self._seen.setdefault(symbol, set())
                for bar in bars:
                    identity = (bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume)
                    if identity not in seen:
                        seen.add(identity)
                        batch.bars.append(bar)
                if len(seen) > 1000:
                    self._seen[symbol] = set(sorted(seen, key=lambda item: item[0])[-800:])
            except Exception as exc:
                batch.errors.append({"symbol": symbol, "source": "toss_candles", "error_type": type(exc).__name__,
                                     "message": "확정봉 수집에 실패했습니다"})
        # 오래 걸릴 수 있는 봉 조회를 마친 뒤 호가를 갱신한다.
        for symbol in selected:
            stock = metadata.get(symbol, {})
            exchange = stock.get("market", "UNKNOWN") if stock.get("currency") == "USD" else "UNKNOWN"
            try:
                batch.quotes.append(await self.client.quote(symbol, exchange=exchange, tradable=False))
            except Exception as exc:
                batch.errors.append({"symbol": symbol, "source": "toss_orderbook", "error_type": type(exc).__name__,
                                     "message": "호가 수집에 실패했습니다"})
            if symbol in chosen:
                batch.eligibility.append(InstrumentEligibility(symbol, utc(self.clock()), "toss:stock_info:incomplete",
                                                               exchange=exchange))
        batch.observed_at = utc(self.clock())
        batch.bars.sort(key=lambda bar: (bar.received_at, bar.timestamp, bar.symbol))
        return batch

    async def poll(self, symbols, now=None, *, priority_symbols=()):
        if self.watchlist is None:
            return await self._legacy_poll(symbols, now, priority_symbols=priority_symbols)
        return await self._managed_poll(now, priority_symbols=priority_symbols)

    def restore_seen(self, bars):
        """재시작해도 이미 보존한 분봉의 최초 수신 시각을 유지한다."""
        for symbol, values in bars.items():
            self._seen[symbol] = {(b.timestamp, b.open, b.high, b.low, b.close, b.volume) for b in values[-800:]}

    async def _managed_poll(self, now=None, *, priority_symbols=()):
        from ..watchlist.budget import ReadCallBudget, ReadBudgetExceeded, retry_budget, provider_retry_until
        now = utc(now or self.clock())
        started = monotonic()
        manager, batch = self.watchlist, MarketBatch()
        state = manager.state
        budget = ReadCallBudget(self.max_api_calls)
        token = retry_budget.set(budget)
        priority = list(dict.fromkeys([*priority_symbols, 'SPY', 'QQQ']))

        async def call(source, method, *args, symbol='*', reserve=0, **kwargs):
            budget.reserved = reserve
            if state.get('backoff_until') and utc(self.clock()) < utc(state['backoff_until']):
                return None
            try:
                budget.take()
                return await method(*args, **kwargs)
            except ReadBudgetExceeded:
                state['budget_exhausted'] = True
            except Exception as exc:
                error = {'symbol': symbol, 'source': source, 'error_type': type(exc).__name__,
                         'message': '시세 수집 실패: 미확인 자료는 매수 판단에 사용하지 않습니다.'}
                if isinstance(exc, httpx.HTTPStatusError):
                    error['http_status'] = exc.response.status_code
                    if exc.response.status_code == 429:
                        state['backoff_until'] = provider_retry_until(exc.response.headers.get('Retry-After'),utc(self.clock())).isoformat()
                batch.errors.append(error)
            return None

        try:
            state['budget_exhausted'] = False
            await manager.refresh(now)
            pool = manager.light_symbols(now)
            # 전체 100종목은 현재가 일괄 조회만 한다. 상세 분봉은 집중·위험 관리 목록에 한정한다.
            if not state.get('last_light_attempt_at') or (now-utc(state['last_light_attempt_at'])).total_seconds() >= self.light_refresh_seconds:
                state['last_light_attempt_at'] = now.isoformat()
                rows = await call('toss_prices', self.client.prices, list(dict.fromkeys([*pool, 'SPY', 'QQQ'])), reserve=len(priority))
                if rows is not None:
                    try:
                        manager.observe_prices(rows, utc(self.clock()))
                        state['last_light_at'] = utc(self.clock()).isoformat()
                    except (ValueError, TypeError, KeyError):
                        batch.errors.append({'source':'toss_prices','symbol':'*','error_type':'DataQualityError','message':'현재가 배치 검증 실패'})
            if pool and (not state.get('last_ranking_attempt_at') or (now-utc(state['last_ranking_attempt_at'])).total_seconds() >= self.ranking_refresh_seconds):
                state['last_ranking_attempt_at'] = now.isoformat()
                rows = await call('toss_rankings', self.client.rankings, ranking_type='MARKET_TRADING_AMOUNT', count=100, reserve=len(priority))
                if rows is not None:
                    try:
                        manager.observe_rankings(rows, utc(self.clock()))
                        state['last_ranking_at'] = utc(self.clock()).isoformat()
                    except (ValueError, TypeError, KeyError):
                        state['rankings'] = {}
                        batch.errors.append({'source':'toss_rankings','symbol':'*','error_type':'DataQualityError','message':'거래대금 랭킹 검증 실패'})
            focus = manager.focus(utc(self.clock()))
            chosen = manager.scheduler.choose([s for s in focus if s not in priority], self.candidates_per_cycle)
            selected = list(dict.fromkeys([*priority, *chosen]))
            missing = [s for s in selected if s not in self._metadata_at or (utc(self.clock())-self._metadata_at[s]).total_seconds() >= 86400]
            if missing:
                result = await call('toss_stocks', self.client.stocks, missing, reserve=len(selected))
                if result is not None:
                    values = result if isinstance(result, list) else result.get('stocks', [])
                    self._metadata.update({row['symbol']: row for row in values if isinstance(row, dict) and row.get('symbol') in selected})
                    self._metadata_at.update({row['symbol']: utc(self.clock()) for row in values if isinstance(row, dict) and row.get('symbol') in selected})
            bounds = session_bounds(utc(self.clock()))
            last_bars = state.setdefault('last_bars_fetch', {})
            # 집중 후보의 이력 준비용 슬롯을 남기고 위험 관리 분봉도 최장 미조회 순으로 갱신한다.
            bar_order = manager.scheduler.choose(priority, len(priority))
            jobs = list(dict.fromkeys([*bar_order, *chosen]))
            pending_focus = set(chosen)
            for symbol in jobs:
                pending_focus.discard(symbol)
                reserve = len(selected)+len(pending_focus)
                current = utc(self.clock())
                if last_bars.get(symbol) and (current-utc(last_bars[symbol])).total_seconds() < 60:
                    continue
                if budget.limit-budget.used <= reserve:
                    state['budget_exhausted'] = True
                    continue
                manager.scheduler.mark(symbol, current)
                response = await call('toss_candles', self.client.candles, symbol, count=200, symbol=symbol, reserve=reserve)
                if response is None:
                    continue
                bars, before = response
                last_bars[symbol] = utc(self.clock()).isoformat()
                if bounds and bars and min(b.timestamp for b in bars) > bounds[0] and before:
                    response = await call('toss_candles_history', self.client.candles, symbol, count=200,
                                          before=utc(before), symbol=symbol, reserve=reserve)
                    if response is not None:
                        bars = response[0]+bars
                seen = self._seen.setdefault(symbol, set())
                for bar in bars:
                    identity = (bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume)
                    if identity not in seen:
                        seen.add(identity)
                        batch.bars.append(bar)
                if len(seen) > 1000:
                    self._seen[symbol] = set(sorted(seen, key=lambda item: item[0])[-800:])
                if bars:
                    state.setdefault('last_detail_success', {})[symbol] = utc(self.clock()).isoformat()
            # 분봉 조회 완료 뒤 보유·미체결·ETF 호가를 먼저 갱신한다.
            for index, symbol in enumerate(selected):
                stock = self._metadata.get(symbol, {})
                exchange = stock.get('market', 'UNKNOWN') if stock.get('currency') == 'USD' else 'UNKNOWN'
                before_quote_calls = budget.used
                quote = await call('toss_orderbook', self.client.quote, symbol, exchange=exchange, tradable=False,
                                   symbol=symbol, reserve=len(selected)-index-1)
                if symbol in chosen and budget.used > before_quote_calls:
                    manager.scheduler.mark(symbol, utc(self.clock()))
                if quote is not None:
                    batch.quotes.append(quote)
                    state.setdefault('last_quote_success', {})[symbol] = utc(self.clock()).isoformat()
                if symbol in chosen:
                    batch.eligibility.append(InstrumentEligibility(symbol, utc(self.clock()), 'toss:stock_info:incomplete', exchange=exchange))
            batch.observed_at = utc(self.clock())
            batch.bars.sort(key=lambda bar: (bar.received_at, bar.timestamp, bar.symbol))
            return batch
        finally:
            retry_budget.reset(token)
            state.update(last_cycle_at=utc(self.clock()).isoformat(), last_cycle_calls=budget.used,
                         cycle_call_budget=budget.limit, last_cycle_duration_seconds=round(monotonic()-started, 3),
                         auth_calls_in_budget=False)
            manager.publish(utc(self.clock()), priority_symbols=priority_symbols)

    async def aclose(self):
        if self.owns_client:
            await self.client.aclose()
