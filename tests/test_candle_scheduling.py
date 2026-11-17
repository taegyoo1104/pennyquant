"""Watchlist candles must progress independently of optional reference latency."""
from datetime import timedelta
from types import SimpleNamespace

from sessionquant.market.source import TossSessionSource
from watchlist_fixtures import NOW, context, observe
from test_watchlist_source import Market


class History:
    def __init__(self):
        self.received = []
        self.published = []
    def ingest(self, bars):
        self.received.extend(bars)
    def publish(self, symbols, now, *, updated=()):
        self.published.append(set(updated))
    def ready(self, symbol, now):
        return False


async def test_slow_optional_references_do_not_starve_background_and_candles_deliver_together(tmp_path, monkeypatch):
    import sessionquant.market.source as module
    repo,_,_,_,manager=context(tmp_path,size=25)
    seconds=SimpleNamespace(value=60.0)
    clock=lambda:NOW+timedelta(seconds=seconds.value)
    monkeypatch.setattr(module,'monotonic',lambda:seconds.value)
    events=[]
    class Client(Market):
        async def candles(self,symbol,**kwargs):
            events.append(('candle',symbol))
            return await super().candles(symbol,**kwargs)
    class References:
        async def poll(self,read,*,limit):
            events.append(('reference',limit))
            seconds.value+=20
    def deliver(batch):
        events.append(('delivery',{b.symbol for b in batch.bars}))
    try:
        await manager.refresh(NOW);observe(manager)
        client=Client(clock)
        source=TossSessionSource(client,clock=clock,watchlist=manager,
            history_store=History(),reference_monitor=References(),on_priority_batch=deliver)
        await source.poll([])
        reference=next(i for i,r in enumerate(events) if r[0]=='reference')
        candles=[s for kind,s in events[:reference] if kind=='candle' and s.startswith('T')]
        assert len(candles)==6  # Two focus + four distinct background slots.
        deliveries=[s for kind,s in events if kind=='delivery' and any(x.startswith('T') for x in s)]
        assert len(deliveries)==1 and deliveries[0]==set(candles)
        assert manager.state['last_cycle_calls']<=24
    finally:repo.close()


async def test_fresh_candles_do_not_consume_background_slots(tmp_path):
    repo,_,_,_,manager=context(tmp_path,size=25)
    now=NOW+timedelta(seconds=60)
    try:
        await manager.refresh(NOW)
        symbols=manager.light_symbols(now)
        # No focus yet. Recent successes used to occupy all four slots and skip.
        recent=sorted(symbols)[:4]
        manager.state['last_bars_fetch']={s:now.isoformat() for s in recent}
        client=Market(lambda:now)
        source=TossSessionSource(client,clock=lambda:now,watchlist=manager,history_store=History())
        await source.poll([])
        collected={s for name,s in client.calls if name=='candles' and s in symbols}
        assert len(collected)==4 and collected.isdisjoint(recent)
    finally:repo.close()


async def test_paced_full_sweep_includes_nonfocus_and_preserves_reference_slot(tmp_path, monkeypatch, record_property):
    import sessionquant.market.source as module
    repo,_,_,_,manager=context(tmp_path,size=25)
    seconds=SimpleNamespace(value=60.0)
    clock=lambda:NOW+timedelta(seconds=seconds.value)
    monkeypatch.setattr(module,'monotonic',lambda:seconds.value)
    reference_calls=[]
    class Client(Market):
        async def prices(self,*args,**kwargs):
            seconds.value+=1;return await super().prices(*args,**kwargs)
        async def rankings(self,*args,**kwargs):
            seconds.value+=1;return await super().rankings(*args,**kwargs)
        async def stocks(self,*args,**kwargs):
            seconds.value+=1;return await super().stocks(*args,**kwargs)
        async def candles(self,*args,**kwargs):
            seconds.value+=1;return await super().candles(*args,**kwargs)
        async def quote(self,*args,**kwargs):
            seconds.value+=1;return await super().quote(*args,**kwargs)
        async def orderbook(self,symbol):
            seconds.value+=1;reference_calls.append(symbol);return {}
        async def reference_candles(self,symbol,**kwargs):
            return {'candles':[]}
    class References:
        async def poll(self,read,*,limit):
            if limit:await read('orderbook','IEF')
    def deliver(batch):
        seconds.value+=4  # Synchronous engine/DB time, independent from HTTP budget.
    try:
        await manager.refresh(NOW);observe(manager)
        symbols=set(manager.light_symbols(clock()))
        client=Client(clock)
        source=TossSessionSource(client,clock=clock,watchlist=manager,history_store=History(),
            watchlist_candles_per_cycle=10,reference_monitor=References(),on_priority_batch=deliver)
        seen=set();began=seconds.value
        for _ in range(4):
            client.calls.clear()
            await source.poll([])
            seen.update(s for name,s in client.calls if name=='candles' and s in symbols)
            assert manager.state['last_cycle_calls']<=source.max_api_calls
            if seen==symbols:break
        record_property('sweep_seconds',seconds.value-began)
        record_property('sweep_cycles',_+1)
        record_property('sweep_symbols',len(seen))
        assert seen==symbols
        assert seconds.value-began<120
        assert reference_calls  # Background work cannot take every request slot.
        assert repo.all('orders')==[]
    finally:repo.close()


async def test_background_throttle_stops_following_reads_without_bypassing_budget(tmp_path):
    import httpx
    repo,_,_,_,manager=context(tmp_path,size=25)
    now=NOW+timedelta(seconds=60)
    class Client(Market):
        async def candles(self,symbol,**kwargs):
            if symbol.startswith('T'):
                self.calls.append(('candles',symbol))
                httpx.Response(429,headers={'Retry-After':'120'},
                    request=httpx.Request('GET','https://fake.test/candles')).raise_for_status()
            return await super().candles(symbol,**kwargs)
    try:
        await manager.refresh(NOW)
        client=Client(lambda:now)
        source=TossSessionSource(client,clock=lambda:now,watchlist=manager,
            history_store=History(),watchlist_candles_per_cycle=10)
        await source.poll([])
        assert len([s for name,s in client.calls if name=='candles' and s.startswith('T')])==1
        assert manager.state['backoff_until']==(now+timedelta(seconds=120)).isoformat()
        count=len(client.calls)
        await source.poll([])
        assert len(client.calls)==count
        assert manager.state['last_cycle_calls']==0
    finally:repo.close()


async def test_candle_due_follows_minute_boundary_and_does_not_refetch_same_minute(tmp_path):
    repo,_,_,_,manager=context(tmp_path,size=25)
    stamp=SimpleNamespace(now=NOW+timedelta(seconds=62))
    try:
        await manager.refresh(NOW)
        symbol=sorted(manager.light_symbols(stamp.now))[0]
        manager.state['last_bars_fetch']={symbol:(NOW+timedelta(seconds=59)).isoformat()}
        client=Market(lambda:stamp.now)
        source=TossSessionSource(client,clock=lambda:stamp.now,watchlist=manager,
            history_store=History(),watchlist_candles_per_cycle=10)
        # Give everyone else a success in the new minute, leaving this symbol due.
        manager.state['last_bars_fetch'].update({s:stamp.now.isoformat()
            for s in manager.light_symbols(stamp.now) if s!=symbol})
        await source.poll([])
        assert ('candles',symbol) in client.calls
        client.calls.clear()
        stamp.now+=timedelta(seconds=10)
        await source.poll([])
        assert ('candles',symbol) not in client.calls
    finally:repo.close()
