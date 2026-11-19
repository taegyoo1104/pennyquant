import asyncio
from datetime import timedelta
import json

import pytest

from sessionquant.collectors.toss_quote_feed import TossQuoteFeed
from sessionquant.collectors.toss_websocket import TossWebSocket
from sessionquant.collectors.toss_market import parse_orderbook
from test_collectors import NOW, orderbook


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():await asyncio.sleep(.001)


class Stream:
    def __init__(self):self.connections=[]; self.active=0; self.maximum=0
    async def stream(self,symbols,**kwargs):
        assert kwargs=={'trades':False,'max_candidates':100}
        queue=asyncio.Queue(); self.connections.append((symbols,queue))
        self.active+=1; self.maximum=max(self.maximum,self.active)
        try:
            while True:
                frame=await queue.get()
                if isinstance(frame,Exception):raise frame
                yield frame
        finally:self.active-=1


class Rest:
    def __init__(self):self.calls=[]; self.release=asyncio.Event(); self.cancelled=0
    async def quote(self,symbol):
        self.calls.append(symbol)
        try:await self.release.wait()
        except asyncio.CancelledError:self.cancelled+=1;raise
        return parse_orderbook(symbol,orderbook(),NOW)


def ack(symbols):return {'type':'subscriptions','subscribed':[f'orderbook:us:{s}' for s in symbols],'rejected':[]}


def quote(symbol='A',at=NOW,price='2.00'):
    book={**orderbook(),'timestamp':at.isoformat(),'bids':[{'price':price,'volume':'30'}]}
    return {'type':'message','topic':'orderbook:us:'+symbol,'data':book,'received_at':at.isoformat()}


async def start(symbols=('A',),**kwargs):
    stream=Stream();rest=Rest()
    feed=TossQuoteFeed(rest,stream=stream,clock=lambda:NOW,retry_initial=.01,retry_max=.04,**kwargs)
    feed.set_symbols(symbols);task=asyncio.create_task(feed.run())
    await until(lambda:bool(stream.connections))
    return feed,rest,stream,task


async def close(feed,task):
    feed.stop();await asyncio.wait_for(task,1)
    assert feed.snapshot()['state']=='STOPPED' and feed.latest('A') is None


async def test_fifty_candidates_plus_risk_symbols_and_no_order_or_db_dependencies():
    feed,rest,stream,task=await start([f'T{i}' for i in range(50)])
    try:
        feed.set_symbols([f'T{i}' for i in range(50)],held=['HELD','T1'],open_orders=['PENDING','HELD'])
        await until(lambda:len(stream.connections)==2)
        symbols,queue=stream.connections[-1]
        assert len(symbols)==52 and {'HELD','PENDING'}<=set(symbols)
        await queue.put(ack(symbols)); await until(lambda:bool(rest.calls))
        assert rest.calls[0]=='HELD' and stream.maximum==1
        with pytest.raises(ValueError):feed.set_symbols([f'T{i}' for i in range(51)])
        with pytest.raises(ValueError):feed.set_symbols([],held=[f'T{i}' for i in range(101)])
        assert feed.snapshot()['desired_count']==52
    finally:await close(feed,task)


async def test_stream_receipt_does_not_wait_for_rest_and_older_bootstrap_cannot_overwrite():
    feed,rest,stream,task=await start()
    try:
        q=stream.connections[0][1];await q.put(ack(['A']))
        await until(lambda:bool(rest.calls))
        await q.put(quote());await until(lambda:feed.latest('A') is not None)
        rest.release.set();await until(lambda:feed.counts['ignored_quotes']==1)
        assert feed.latest('A').bid==2.
        # Thousands of updates retain one value, not an unbounded event queue.
        for _ in range(1000):await q.put(quote())
        await until(lambda:feed.counts['ignored_quotes']>=1001)
        assert feed.snapshot()['cached_count']==1
        assert feed.latest('A',NOW+timedelta(seconds=31)) is None
    finally:await close(feed,task)


async def test_disconnect_invalidates_old_data_and_reconnect_needs_ack_and_new_snapshot():
    feed,rest,stream,task=await start()
    try:
        q=stream.connections[0][1];await q.put(ack(['A']))
        await until(lambda:bool(rest.calls))
        await q.put(quote())
        await until(lambda:feed.latest('A') is not None)
        await q.put(ConnectionError('secret must not appear'))
        await until(lambda:len(stream.connections)==2)
        assert feed.latest('A') is None and 'secret' not in json.dumps(feed.snapshot())
        q=stream.connections[1][1];await q.put(ack(['A']))
        await until(lambda:feed.snapshot()['acknowledged'])
        assert feed.latest('A') is None
        await q.put(quote());await until(lambda:feed.latest('A') is not None)
        assert rest.cancelled>=1 and stream.maximum==1
    finally:await close(feed,task)


async def test_subscription_change_keeps_holdings_and_rejects_old_generation():
    feed,rest,stream,task=await start()
    try:
        q=stream.connections[0][1];await q.put(ack(['A']));await q.put(quote())
        await until(lambda:feed.latest('A') is not None)
        generation=feed._generation
        feed.set_symbols(['B'],held=['A'])
        feed._accept(parse_orderbook('A',orderbook(),NOW),'rest',generation)
        assert feed.latest('A') is None
        await until(lambda:len(stream.connections)==2)
        assert stream.connections[-1][0]==['A','B']
        feed.set_symbols(['B'],held=['A'])
        await asyncio.sleep(.01)
        assert len(stream.connections)==2
        feed.set_symbols([])
        await until(lambda:feed.state=='IDLE' and stream.active==0)
    finally:await close(feed,task)


async def test_partial_rejection_and_repeated_failures_back_off_with_no_usable_quotes():
    feed,rest,stream,task=await start()
    try:
        for index in range(3):
            q=stream.connections[index][1]
            await q.put({'type':'subscriptions','subscribed':[],'rejected':[{'code':'denied'}]})
            await until(lambda:feed.state=='BACKOFF')
            assert feed.retry_seconds==min(.01*2**index,.04)
            assert feed.latest('A') is None and not rest.calls
            await until(lambda:len(stream.connections)>index+1)
    finally:await close(feed,task)


async def test_conflicting_same_time_or_future_book_cannot_be_used():
    feed,rest,stream,task=await start()
    try:
        q=stream.connections[0][1];await q.put(ack(['A']));await q.put(quote())
        await until(lambda:feed.latest('A') is not None)
        await q.put(quote(price='1.99'));await until(lambda:feed.counts['invalid_quotes']==1)
        assert feed.latest('A') is None
        await q.put(quote());await q.put(quote(at=NOW+timedelta(seconds=1)))
        await until(lambda:feed.counts['invalid_quotes']==2)
        assert feed.latest('A') is None
        feed.clock=lambda:NOW+timedelta(seconds=2)
        await q.put(quote(at=NOW+timedelta(seconds=2)))
        await until(lambda:feed.latest('A') is not None)
    finally:await close(feed,task)


async def test_bootstrap_failure_is_visible_but_does_not_disconnect_stream():
    feed,rest,stream,task=await start(rest_timeout=.01)
    try:
        q=stream.connections[0][1];await q.put(ack(['A']))
        await until(lambda:feed.counts['rest_failures']==1)
        assert feed.state=='SUBSCRIBED' and feed.latest('A') is None
        await q.put(quote());await until(lambda:feed.latest('A') is not None)
    finally:await close(feed,task)


@pytest.mark.parametrize('send_ack',[False,True])
async def test_real_adapter_timeout_and_fifty_orderbook_only_declaration(monkeypatch,send_ack):
    sent=[];queue=asyncio.Queue()
    class WS:
        async def send(self,payload):sent.append(json.loads(payload))
        def __aiter__(self):return self
        async def __anext__(self):return await queue.get()
    class Context:
        async def __aenter__(self):return WS()
        async def __aexit__(self,*args):pass
    monkeypatch.setattr('websockets.asyncio.client.connect',lambda *a,**k:Context())
    class Auth:
        async def access_token(self):return 'fake'
    symbols=[f'T{i}' for i in range(50)]
    if send_ack:await queue.put(json.dumps(ack(symbols)))
    stream=TossWebSocket(Auth()).stream(symbols,trades=False,max_candidates=100,ack_timeout=.02,idle_timeout=.02)
    try:
        if send_ack:assert (await anext(stream))['type']=='subscriptions'
        with pytest.raises(TimeoutError):await anext(stream)
        assert sent==[[{'type':'orderbook:us','codes':symbols}]]
    finally:await stream.aclose()


async def test_loopback_websocket_disconnect_reconnect_and_clean_shutdown(monkeypatch):
    from websockets.asyncio.server import serve
    disconnect=asyncio.Event(); second_quote=asyncio.Event(); connections=[]
    async def server(ws):
        declaration=json.loads(await ws.recv())
        assert declaration==[{'type':'orderbook:us','codes':['A']}]
        assert ws.request.headers['Authorization']=='Bearer fixture'
        connections.append(ws)
        await ws.send(json.dumps(ack(['A'])))
        if len(connections)>1:await second_quote.wait()
        await ws.send(json.dumps(quote()))
        if len(connections)==1:
            await disconnect.wait();await ws.close()
        else:await ws.wait_closed()
    class Client(Rest):
        async def access_token(self):return 'fixture'
    async with serve(server,'127.0.0.1',0) as listener:
        port=listener.sockets[0].getsockname()[1]
        monkeypatch.setattr('sessionquant.collectors.toss_websocket.WS_URL',f'ws://127.0.0.1:{port}')
        monkeypatch.setattr('sessionquant.collectors.toss_websocket.now_utc',lambda:NOW)
        feed=TossQuoteFeed(Client(),clock=lambda:NOW,retry_initial=.01,retry_max=.02)
        feed.set_symbols(['A']);task=asyncio.create_task(feed.run())
        try:
            await until(lambda:feed.latest('A') is not None)
            disconnect.set();await until(lambda:len(connections)==2 and feed.snapshot()['acknowledged'])
            assert feed.latest('A') is None
            second_quote.set();await until(lambda:feed.latest('A') is not None)
        finally:
            second_quote.set();disconnect.set();await close(feed,task)
