"""100개 일괄 관찰·20개 상세 순환·보유 우선·실제 재시도 예산을 검증한다."""
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from sessionquant.collectors.common import RateLimiter, get_readonly
from sessionquant.market.source import TossSessionSource
from sessionquant.models import Bar, Quote
from sessionquant.watchlist.budget import ReadBudgetExceeded, ReadCallBudget, retry_budget
from sessionquant.watchlist.selection import WatchlistManager
from watchlist_fixtures import NOW, context, observe


class Market:
    def __init__(self,clock):self.clock,self.calls,self.throttle=clock,[],False
    async def prices(self,symbols):
        self.calls.append(('prices',symbols))
        if self.throttle:
            httpx.Response(429,headers={'Retry-After':'120'},request=httpx.Request('GET','https://fake.test?apikey=PRIVATE')).raise_for_status()
        return [{'symbol':s,'lastPrice':'101.5' if s.startswith('T') else '100','currency':'USD','timestamp':self.clock().isoformat()} for s in symbols]
    async def rankings(self,**kwargs):
        self.calls.append(('rankings',kwargs))
        return {'rankedAt':self.clock().isoformat(),'rankings':[{'symbol':f'T{i:03}','currency':'USD',
            'tradingVolume':'100000','tradingAmount':str((1000-i)*1_000_000)} for i in range(400,500)]}
    async def stocks(self,symbols):
        self.calls.append(('stocks',symbols))
        return [{'symbol':s,'currency':'USD','market':'NASDAQ','status':'ACTIVE'} for s in symbols]
    async def candles(self,symbol,**kwargs):
        self.calls.append(('candles',symbol))
        stamp=self.clock()
        return [Bar(symbol,stamp,stamp,100,101,99,100,100000)],None
    async def quote(self,symbol,**kwargs):
        self.calls.append(('quote',symbol))
        if self.throttle:
            httpx.Response(429,headers={'Retry-After':'120'},request=httpx.Request('GET','https://fake.test?apikey=PRIVATE')).raise_for_status()
        return Quote(symbol,self.clock(),self.clock(),100,100.01,1000,1000,False,False,'NASDAQ')


async def test_light_all100_focus_rotates_after_restart_and_held_outsider_stays_managed(tmp_path):
    repo,universe,config,provider,manager=context(tmp_path)
    clock=SimpleNamespace(now=NOW+timedelta(seconds=60))
    try:
        await manager.refresh(NOW);observe(manager)
        client=Market(lambda:clock.now)
        source=TossSessionSource(client,clock=lambda:clock.now,watchlist=manager,max_api_calls=24)
        batch=await source.poll(list(universe.members(NOW)),priority_symbols=['T001'])
        assert len(next(values for method,values in client.calls if method=='prices'))==102
        assert [q.symbol for q in batch.quotes][:3]==['T001','SPY','QQQ']
        detail={s for method,s in client.calls if method=='candles'}
        assert detail=={'T001','SPY','QQQ','T400','T401'}
        assert len(manager.state['focus'])==20
        assert all(x.fractional is None and x.tradable is None for x in batch.eligibility)
        assert all(q.tradable is False for q in batch.quotes)
        assert manager.state['last_cycle_calls']==len(client.calls)<=24
        assert manager.news_targets(clock.now,['T001'])['risk_priority']==['T001']
        clock.now+=timedelta(seconds=30)
        restored=WatchlistManager(repo,universe,config,provider=provider,allow_synthetic=True,clock=lambda:clock.now)
        other=Market(lambda:clock.now)
        restarted=TossSessionSource(other,clock=lambda:clock.now,watchlist=restored)
        second=await restarted.poll([],priority_symbols=['T001'])
        assert [q.symbol for q in second.quotes][3:]==['T402','T403']
        assert 'T001' in {q.symbol for q in second.quotes}
        assert len(repo.load_state() or {})==0
    finally:repo.close()


async def test_small_budget_rotates_focus_without_inventing_missing_candles(tmp_path):
    repo,_,_,_,manager=context(tmp_path)
    clock=SimpleNamespace(now=NOW+timedelta(seconds=60))
    try:
        await manager.refresh(NOW);observe(manager)
        client=Market(lambda:clock.now)
        source=TossSessionSource(client,clock=lambda:clock.now,watchlist=manager,max_api_calls=8)
        first=await source.poll([],priority_symbols=['T001'])
        assert len(client.calls)<=8
        # 단계 02에서는 제한된 예산도 보유·ETF 자료가 먼저 사용한다.
        assert {b.symbol for b in first.bars}=={'T001','SPY','QQQ'} and manager.state['budget_exhausted']
        assert {q.symbol for q in first.quotes}=={'T001','SPY','QQQ'}
        visited=set()
        for _ in range(3):
            client.calls.clear();clock.now+=timedelta(seconds=30)
            following=await source.poll([],priority_symbols=['T001'])
            assert len(client.calls)<=8
            assert {'T001','SPY','QQQ'} <= {q.symbol for q in following.quotes}
            visited.update(q.symbol for q in following.quotes if q.symbol.startswith('T4'))
        assert {'T400','T401','T402','T403'} <= visited
    finally:repo.close()


async def test_429_applies_provider_backoff_to_whole_cycle_without_secret_output(tmp_path):
    repo,_,_,_,manager=context(tmp_path)
    try:
        await manager.refresh(NOW);observe(manager)
        now=NOW+timedelta(seconds=60)
        client=Market(lambda:now);client.throttle=True
        source=TossSessionSource(client,clock=lambda:now,watchlist=manager)
        batch=await source.poll([],priority_symbols=['T001'])
        assert client.calls==[('stocks',['T001','SPY','QQQ']),('quote','T001')] and not batch.quotes
        assert manager.state['backoff_until']==(now+timedelta(seconds=120)).isoformat()
        assert batch.errors[0]['http_status']==429 and 'PRIVATE' not in str(batch.errors)
        assert manager.entry_blockers('T400',now)==['market_provider_backoff']
        await source.poll([],priority_symbols=['T001'])
        assert len(client.calls)==2
    finally:repo.close()


async def test_actual_http_retries_consume_same_read_budget():
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(503,headers={'Retry-After':'0'})
    async def no_wait():pass
    budget=ReadCallBudget(2);budget.take()
    token=retry_budget.set(budget)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ReadBudgetExceeded):
                await get_readonly(client,'https://fake.test',limiter=SimpleNamespace(wait=no_wait))
        assert len(calls)==budget.used==2
    finally:retry_budget.reset(token)


async def test_managed_429_is_not_retried_inside_get_helper():
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(429,headers={'Retry-After':'120'})
    budget=ReadCallBudget(10);budget.take();token=retry_budget.set(budget)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await get_readonly(client,'https://fake.test',limiter=RateLimiter(20))
        assert len(calls)==1
    finally:retry_budget.reset(token)


async def test_unknown_market_caps_never_fall_back_to_first100_alphabetically(tmp_path):
    repo,universe,config,_,_=context(tmp_path)
    try:
        manager=WatchlistManager(repo,universe,config,clock=lambda:NOW)
        client=Market(lambda:NOW)
        source=TossSessionSource(client,watchlist=manager,clock=lambda:NOW)
        batch=await source.poll(list(universe.members(NOW)),priority_symbols=['T001'])
        assert {q.symbol for q in batch.quotes}=={'T001','SPY','QQQ'}
        assert manager.light_symbols(NOW)==[] and manager.focus(NOW)==[]
        assert manager.entry_blockers('T400',NOW)==['watchlist_unknown']
        assert repo.get_control('watchlist_dashboard_v1')['status']=='UNKNOWN'
    finally:repo.close()
