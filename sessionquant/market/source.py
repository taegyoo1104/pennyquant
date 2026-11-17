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
from ..performance import capture, measure


@dataclass
class MarketBatch:
    bars: list[Bar] = field(default_factory=list)
    quotes: list[Quote] = field(default_factory=list)
    eligibility: list[InstrumentEligibility] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    observed_at: datetime | None = None
    cached_bars: list[Bar] = field(default_factory=list)


class TossSessionSource:
    def __init__(self, client: TossMarketClient | None = None, *, candidates_per_cycle: int = 2, clock=None,
                 watchlist=None, max_api_calls=24, light_refresh_seconds=30, ranking_refresh_seconds=60,
                 max_cycle_seconds=15, history_pages_per_cycle=1, on_priority_batch=None, reference_monitor=None,
                 history_store=None, watchlist_candles_per_cycle=4, warmup=None):
        if type(candidates_per_cycle) is not int or not 1 <= candidates_per_cycle <= 10:
            raise ValueError("한 주기에 상세 수집할 종목 수는 1~10개입니다")
        self.client = client or TossMarketClient()
        self.owns_client = client is None
        self.candidates_per_cycle = candidates_per_cycle
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.cursor = 0
        self.watchlist = watchlist
        if watchlist is not None:
            from ..watchlist.toss_capitalization import bind_provider
            bind_provider(watchlist,self.client)
        self.max_cycle_seconds = max_cycle_seconds
        self.history_pages_per_cycle = history_pages_per_cycle
        self.on_priority_batch = on_priority_batch
        self.reference_monitor = reference_monitor
        self.history_store = history_store
        self.warmup = warmup
        self.watchlist_candles_per_cycle = watchlist_candles_per_cycle
        if type(watchlist_candles_per_cycle) is not int or not 0 <= watchlist_candles_per_cycle <= 10:
            raise ValueError('감시목록 분봉 조회 수는 0~10개입니다.')
        if type(max_cycle_seconds) is not int or not 5 <= max_cycle_seconds <= 30:
            raise ValueError('후보 조회 마감 예산은 5~30초 정수여야 합니다.')
        if type(history_pages_per_cycle) is not int or not 0 <= history_pages_per_cycle <= 3:
            raise ValueError('주기별 과거 봉 보충은 0~3페이지여야 합니다.')
        self.max_api_calls = max_api_calls
        self.light_refresh_seconds = light_refresh_seconds
        self.ranking_refresh_seconds = ranking_refresh_seconds
        if type(max_api_calls) is not int or not 8 <= max_api_calls <= 100:
            raise ValueError('주기별 시세 조회 예산은 8~100회여야 합니다.')
        self._metadata = {}
        self._metadata_at = {}
        self._slow_cycles = []
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
        with capture() as timings:
            batch = await self._managed_poll(now, priority_symbols=priority_symbols)
            latest = {'observed_at':utc(self.clock()).isoformat(), **timings.snapshot()}
        self._slow_cycles = sorted([*self._slow_cycles,latest],
            key=lambda row:row['elapsed_seconds'],reverse=True)[:3]
        self.watchlist.repo.set_control('market_performance_v1',{
            'scope':'collector_task','backend':self.watchlist.repo.backend,
            'latest':latest,'slowest_this_process':self._slow_cycles})
        return batch

    def restore_seen(self, bars):
        """재시작해도 이미 보존한 분봉의 최초 수신 시각을 유지한다."""
        for symbol, values in bars.items():
            self._seen[symbol] = {(b.timestamp, b.open, b.high, b.low, b.close, b.volume) for b in values[-20000:]}

    async def _managed_poll(self, now=None, *, priority_symbols=()):
        from ..watchlist.budget import ReadCallBudget, ReadBudgetExceeded, retry_budget, provider_retry_until
        from .eligibility import toss_eligibility
        import asyncio
        now = utc(now or self.clock())
        started = monotonic()
        optional_deadline = started + self.max_cycle_seconds
        manager, batch = self.watchlist, MarketBatch()
        state = manager.state
        budget = ReadCallBudget(self.max_api_calls)
        token = retry_budget.set(budget)
        priority = list(dict.fromkeys([*priority_symbols, 'SPY', 'QQQ']))
        measurements = []
        updated_history = set()

        def skipped(source, symbol, optional, reason):
            measurements.append({'source':source,'symbol':symbol,'priority':not optional,'status':reason,
                'read_attempts':0,'duration_seconds':0.0,'observed_at':utc(self.clock()).isoformat()})

        async def call(source, method, *args, symbol='*', optional=False, **kwargs):
            budget.reserved = 0
            group = 'MARKET_DATA_CHART' if method.__name__ in {'candles','reference_candles'} else 'STOCK' if method.__name__ == 'stocks' else 'SECTOR' if method.__name__ in {'sectors','sector_stocks'} else 'MARKET_DATA'
            until = state.get('reference_backoff_groups',{}).get(group)
            if until and utc(self.clock()) < utc(until):
                skipped(source,symbol,optional,'provider_group_backoff')
                return None
            if state.get('backoff_until') and utc(self.clock()) < utc(state['backoff_until']):
                skipped(source,symbol,optional,'provider_backoff')
                return None
            remaining = optional_deadline-monotonic()
            if optional and remaining <= 0:
                state['optional_deadline_reached'] = True
                skipped(source,symbol,optional,'deadline_skipped')
                return None
            before, began = budget.used, monotonic()
            status = 'UNKNOWN'
            try:
                budget.take()
                result = await asyncio.wait_for(method(*args, **kwargs), timeout=min(5,remaining)) if optional else await method(*args, **kwargs)
                status = 'ok'
                return result
            except ReadBudgetExceeded:
                state['budget_exhausted'] = True
                status = 'budget_exhausted'
            except Exception as exc:
                status = type(exc).__name__
                error = {'symbol':symbol,'source':source,'error_type':status,
                         'message':'시세 수집 실패: 미확인 자료는 매수 판단에 사용하지 않습니다.'}
                if isinstance(exc,httpx.HTTPStatusError):
                    error['http_status'] = exc.response.status_code
                    if exc.response.status_code == 429:
                        until = provider_retry_until(exc.response.headers.get('Retry-After'),utc(self.clock())).isoformat()
                        if source.startswith(('market_reference:','market_cap:')):
                            state.setdefault('reference_backoff_groups',{})[group] = until
                        else:
                            state['backoff_until'] = until
                batch.errors.append(error)
            finally:
                measurements.append({'source':source,'symbol':symbol,'priority':not optional,'status':status,
                    'read_attempts':budget.used-before,'duration_seconds':round(monotonic()-began,3),
                    'observed_at':utc(self.clock()).isoformat()})
            return None

        async def metadata(symbols, *, optional=False):
            missing = [s for s in symbols if s not in self._metadata_at or (utc(self.clock())-self._metadata_at[s]).total_seconds() >= 86400]
            if not missing:
                return
            result = await call('toss_stocks',self.client.stocks,missing,optional=optional)
            if result is None:
                return
            values = result if isinstance(result,list) else result.get('stocks') if isinstance(result,dict) else None
            if not isinstance(values,list):
                batch.errors.append({'source':'toss_stocks','symbol':'*','error_type':'DataQualityError','message':'종목 마스터 응답 형식 미확인'})
                return
            duplicates = set()
            for row in values:
                symbol = row.get('symbol') if isinstance(row,dict) else None
                if symbol not in missing:
                    continue
                if symbol in duplicates:
                    self._metadata.pop(symbol,None); self._metadata_at.pop(symbol,None)
                    continue
                duplicates.add(symbol)
                self._metadata[symbol] = row
                self._metadata_at[symbol] = utc(self.clock())

        def eligibility(symbol):
            stamp = self._metadata_at.get(symbol,utc(self.clock()))
            return toss_eligibility(symbol,self._metadata.get(symbol),stamp)

        async def quotes(symbols, *, optional=False):
            for symbol in symbols:
                evidence = eligibility(symbol)
                before = budget.used
                quote = await call('toss_orderbook',self.client.quote,symbol,exchange=evidence.exchange,
                    tradable=False,symbol=symbol,optional=optional)
                if optional and budget.used > before:
                    manager.scheduler.mark(symbol,utc(self.clock()))
                if not optional and budget.used > before:
                    state.setdefault('last_priority_quote_attempt',{})[symbol] = utc(self.clock()).isoformat()
                if quote is not None:
                    batch.quotes.append(quote)
                    state.setdefault('last_quote_success',{})[symbol] = utc(self.clock()).isoformat()
                if symbol not in {'SPY','QQQ'}:
                    batch.eligibility.append(evidence)

        async def deliver(*, priority=True):
            nonlocal optional_deadline
            if self.on_priority_batch and (batch.bars or batch.quotes or batch.cached_bars):
                batch.observed_at = utc(self.clock())
                began = monotonic()
                self.on_priority_batch(batch)
                if not priority:
                    # Preserve only the unused request window across synchronous
                    # engine/DB work. Do not reset HTTP time, call budgets or backoff.
                    elapsed = monotonic()-began
                    optional_deadline += elapsed
                    state['candidate_delivery_seconds'] = round(
                        state.get('candidate_delivery_seconds',0)+elapsed,3)
                batch.bars, batch.quotes, batch.eligibility = [], [], []
                batch.cached_bars = []
                state['priority_delivered_at' if priority else 'candidate_delivered_at'] = utc(self.clock()).isoformat()

        def accept(symbol,bars, *, cached=False):
            if self.history_store is not None and bars and not cached:
                self.history_store.ingest(bars)
                updated_history.add(symbol)
            seen = self._seen.setdefault(symbol,set())
            for bar in bars:
                identity = (bar.timestamp,bar.open,bar.high,bar.low,bar.close,bar.volume)
                if identity not in seen:
                    seen.add(identity)
                    (batch.cached_bars if cached else batch.bars).append(bar)
            if len(seen) > 20000:
                self._seen[symbol] = set(sorted(seen,key=lambda x:x[0])[-20000:])
            if bars and not cached:
                state.setdefault('last_detail_success',{})[symbol] = utc(self.clock()).isoformat()

        async def candles(symbol, *, optional=False):
            last = state.setdefault('last_bars_fetch',{})
            stamp = utc(self.clock())
            if last.get(symbol) and (stamp-utc(last[symbol])).total_seconds() < 60:
                return
            before = budget.used
            response = await call('toss_candles',self.client.candles,symbol,count=200,symbol=symbol,optional=optional)
            if budget.used > before:
                manager.scheduler.mark(symbol,utc(self.clock()))
            if response is None:
                return
            bars, cursor = response
            accept(symbol,bars)
            last[symbol] = utc(self.clock()).isoformat()
            progress = state.setdefault('history_progress',{}).setdefault(symbol,{})
            if not progress.get('started') or (progress.get('status') == 'WAITING_DATA' and cursor):
                progress.update(started=True,cursor=cursor,done=not bool(cursor),pages=progress.get('pages',0),
                                status='PAGING' if cursor else 'EXHAUSTED' if bars else 'WAITING_DATA')
            # 재시작·중단 후 당일 구멍이 있으면 오래된 과거 커서보다 해당 구간 복구를 우선한다.
            bounds = session_bounds(stamp)
            if bounds and bars and cursor:
                oldest = min(b.timestamp for b in bars)
                minutes = {row[0] for row in self._seen[symbol]}
                elapsed = max(0,int((min(stamp,bounds[1])-bounds[0]).total_seconds()//60))
                gap = next((bounds[0]+timedelta(minutes=i) for i in range(1,elapsed+1)
                            if bounds[0]+timedelta(minutes=i) not in minutes),None)
                if (gap and oldest > gap and progress.get('current_gap_at') != gap.isoformat()
                        and (not progress.get('cursor') or utc(progress['cursor']) < gap)):
                    progress.update(cursor=cursor,done=False,status='PAGING',current_gap_at=gap.isoformat())

        try:
            state['budget_exhausted'] = False
            state['optional_deadline_reached'] = False
            state['pre_focus_phase_seconds'] = None
            state['focus_phase_seconds'] = None
            state['candidate_delivery_seconds'] = 0.0
            # 100개 관찰·공급자 갱신보다 먼저 위험 호가를 전달한다. 청산 시계는 별도 실행된다.
            await metadata(priority)
            quote_attempts = state.setdefault('last_priority_quote_attempt',{})
            priority_order = sorted(priority,key=lambda s:(quote_attempts.get(s,''),priority.index(s)))
            await quotes(priority_order)
            await deliver()
            bounds = session_bounds(utc(self.clock()))
            if bounds and utc(self.clock()) >= bounds[1]:
                # Keep risk quotes/exits alive, but do not start tomorrow's warmup
                # or perform background history/ETF work during shutdown grace.
                batch.observed_at = utc(self.clock())
                state['priority_phase_seconds'] = round(monotonic()-started,3)
                return batch
            for symbol in manager.scheduler.choose(priority,len(priority)):
                await candles(symbol)
            await deliver()
            state['priority_phase_seconds'] = round(monotonic()-started,3)
            from ..watchlist.toss_capitalization import TossMarketCapProvider
            if isinstance(manager.provider,TossMarketCapProvider):
                async def cap_read(stream,symbol):
                    method,args = (self.client.sectors,()) if stream=='sectors' else (self.client.sector_stocks,(symbol,))
                    return await call('market_cap:'+stream,method,*args,symbol=symbol or '*',optional=True)
                try:
                    await manager.provider.advance(cap_read,limit=1)
                except (ValueError,TypeError,KeyError):
                    batch.errors.append({'source':'market_cap','message':'토스 섹터 시총 형식 미확인; 기존 검증 목록을 보존합니다.'})
            current_snapshot=manager.store.at(utc(self.clock())) or {}
            force_cap=isinstance(manager.provider,TossMarketCapProvider) and manager.provider.state.get('complete',False) and current_snapshot.get('contract')!='toss_reported_security_krw_v1'
            await manager.refresh(utc(self.clock()),force=force_cap)
            if self.warmup is not None:
                async def warmup_read(symbol,cursor):
                    return await call('toss_startup_backfill',self.client.candles,symbol,count=200,
                                      before=cursor,symbol=symbol,optional=True)
                with measure('warmup'):
                    await self.warmup.advance(manager,warmup_read,accept,
                        lambda symbol:{row[0] for row in self._seen.get(symbol,set())},priority=priority_symbols,
                        hydrate=lambda symbol,bars:accept(symbol,bars,cached=True))
                await deliver()
                if not self.warmup.control.get('data_ready'):
                    batch.observed_at = utc(self.clock())
                    return batch
            # Risk delivery and startup work may include slow synchronous DB writes.
            # Give ready candidates one bounded window of their own, without resetting
            # the shared request/retry count or bypassing provider Retry-After.
            focus_started = monotonic()
            state['pre_focus_phase_seconds'] = round(focus_started-started,3)
            optional_deadline = focus_started + self.max_cycle_seconds
            pool = manager.light_symbols(utc(self.clock()))
            current = utc(self.clock())
            if not state.get('last_light_attempt_at') or (current-utc(state['last_light_attempt_at'])).total_seconds() >= self.light_refresh_seconds:
                before = budget.used
                rows = await call('toss_prices',self.client.prices,list(dict.fromkeys([*pool,'SPY','QQQ'])),optional=True)
                if budget.used > before:
                    state['last_light_attempt_at'] = utc(self.clock()).isoformat()
                if rows is not None:
                    try:
                        manager.observe_prices(rows,utc(self.clock())); state['last_light_at'] = utc(self.clock()).isoformat()
                    except (ValueError,TypeError,KeyError):
                        batch.errors.append({'source':'toss_prices','symbol':'*','error_type':'DataQualityError','message':'현재가 배치 검증 실패'})
            if pool and (not state.get('last_ranking_attempt_at') or (current-utc(state['last_ranking_attempt_at'])).total_seconds() >= self.ranking_refresh_seconds):
                before = budget.used
                rows = await call('toss_rankings',self.client.rankings,ranking_type='MARKET_TRADING_AMOUNT',count=100,optional=True)
                if budget.used > before:
                    state['last_ranking_attempt_at'] = utc(self.clock()).isoformat()
                if rows is not None:
                    try:
                        manager.observe_rankings(rows,utc(self.clock())); state['last_ranking_at'] = utc(self.clock()).isoformat()
                    except (ValueError,TypeError,KeyError):
                        state['rankings'] = {}
                        batch.errors.append({'source':'toss_rankings','symbol':'*','error_type':'DataQualityError','message':'거래대금 랭킹 검증 실패'})
            focus = manager.focus(utc(self.clock()))
            chosen = manager.scheduler.choose([s for s in focus if s not in priority],self.candidates_per_cycle)
            await metadata(chosen,optional=True)
            # Fresh candidate quotes reach the engine before slow candle persistence
            # or non-candidate ETF reference collection can consume the window.
            await quotes(chosen,optional=True)
            await deliver(priority=False)
            for symbol in chosen:
                await candles(symbol,optional=True)
            await deliver(priority=False)
            state['focus_phase_seconds'] = round(monotonic()-focus_started,3)
            if self.reference_monitor is not None:
                async def reference_read(stream,symbol):
                    from ..market_reference.contracts import REFERENCES
                    methods = {'stocks':(self.client.stocks,(list(REFERENCES),),{}),
                               'prices':(self.client.prices,(list(REFERENCES),),{}),
                               'orderbook':(self.client.orderbook,(symbol,),{}),
                               'candle':(self.client.reference_candles,(symbol,),{}),
                               'daily':(self.client.reference_candles,(symbol,),{'interval':'1d','count':5})}
                    method,args,kwargs = methods[stream]
                    before_errors = len(batch.errors)
                    value = await call('market_reference:'+stream,method,*args,symbol=symbol,optional=True,**kwargs)
                    error = None
                    if value is None:
                        last_error = batch.errors[-1] if len(batch.errors)>before_errors else {}
                        error = 'http_'+str(last_error['http_status']) if last_error.get('http_status') else last_error.get('error_type','budget_or_backoff_or_deadline')
                    return {'data':value,'error':error,'received_at':utc(self.clock()).isoformat()}
                # 후보 전달 후 남은 예산으로 참고군을 수집하며 배경 분봉 슬롯도 남긴다.
                allowance = max(0,budget.limit-budget.used-(self.candidates_per_cycle*2+1))
                await self.reference_monitor.poll(reference_read,limit=allowance)
            if self.history_store is not None:
                attempts = state.setdefault('last_watchlist_candle_attempt',{})
                remaining_symbols = [s for s in pool if s not in priority and s not in chosen]
                remaining_symbols.sort(key=lambda s:(attempts.get(s,''),s))
                for symbol in remaining_symbols[:self.watchlist_candles_per_cycle]:
                    if budget.used >= budget.limit or monotonic() >= optional_deadline:
                        break
                    before = budget.used
                    await candles(symbol,optional=True)
                    if budget.used > before:
                        attempts[symbol] = utc(self.clock()).isoformat()
            history = state.setdefault('history_progress',{})
            history_targets = [*priority,*focus,*(pool if self.history_store is not None else [])]
            order = sorted([s for s in dict.fromkeys(history_targets) if history.get(s,{}).get('cursor') and not history[s].get('done')],
                           key=lambda s:(history[s].get('last_attempt_at',''),s))
            for symbol in order[:self.history_pages_per_cycle]:
                progress = history[symbol]
                cursor = progress['cursor']
                bounds = session_bounds(utc(self.clock()))
                if (self.history_store is not None and utc(cursor) <= (bounds[0] if bounds else utc(self.clock()))
                        and self.history_store.ready(symbol,utc(self.clock()))):
                    progress.update(done=True,status='WINDOW_CACHED')
                    continue
                progress['last_attempt_at'] = utc(self.clock()).isoformat()
                response = await call('toss_history_backfill',self.client.candles,symbol,count=200,before=utc(cursor),symbol=symbol,optional=True)
                if response is None:
                    continue
                bars, following = response
                accept(symbol,bars)
                progress['pages'] += 1
                progress['last_received_at'] = utc(self.clock()).isoformat()
                if bars:
                    progress['oldest_at'] = min(b.timestamp for b in bars).isoformat()
                stalled = bool(following and utc(following) >= utc(cursor))
                reached = bool(bars and min(b.timestamp for b in bars) < utc(self.clock())-timedelta(days=45))
                progress['status'] = 'CURSOR_STALLED' if stalled else 'EXHAUSTED' if not following else 'WINDOW_REACHED' if reached else 'PAGING'
                progress['done'] = not following or stalled or reached
                progress['cursor'] = following
            batch.observed_at = utc(self.clock())
            if self.history_store is not None:
                self.history_store.publish(pool,batch.observed_at,updated=updated_history)
            batch.bars.sort(key=lambda bar:(bar.received_at,bar.timestamp,bar.symbol))
            return batch
        finally:
            retry_budget.reset(token)
            state.update(last_cycle_at=utc(self.clock()).isoformat(),last_cycle_calls=budget.used,
                cycle_call_budget=budget.limit,last_cycle_duration_seconds=round(monotonic()-started,3),
                auth_calls_in_budget=False,read_measurements=measurements,
                provider_read_observations=getattr(self.client,'read_observations',{}),
                candle_observations=getattr(self.client,'candle_observations',{}))
            with measure('watchlist_publish'):
                manager.publish(utc(self.clock()),priority_symbols=priority_symbols)

    async def aclose(self):
        if self.owns_client:
            await self.client.aclose()
