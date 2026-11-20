"""Real trade-channel contract, bounded charts and disconnected display states."""
import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from sessionquant.market_reference.trade_feed import BenchmarkTradeFeed, MAX_POINTS, SYMBOLS, VIEW
from sessionquant.dashboard.home_benchmarks import benchmark_cards, benchmark_cards_html
from sessionquant.database.repository import Repository
from sessionquant.market_reference.collector import MarketReferenceMonitor

NOW = datetime(2026,11,19,16,tzinfo=timezone.utc)


def trade(symbol='SPY', *, at=NOW, price='99', received=None):
    return {'type':'message','topic':f'trade:us:{symbol}',
            'data':{'price':price,'volume':'8','currency':'USD','timestamp':at.isoformat()},
            'received_at':(received or at).isoformat()}


def ack(symbols=SYMBOLS):
    return {'type':'subscriptions','subscribed':[f'trade:us:{s}' for s in symbols],'rejected':[]}


def ready(*, now=NOW, restored=None):
    feed=BenchmarkTradeFeed(None,clock=lambda:now,restored=restored)
    feed.state='CONNECTED';feed.acknowledged=True
    return feed


def card(feed, now=NOW):
    view={'active_symbols':['SPY'],'items':{'SPY':{
        'previous_close':'100','previous_close_at':'2026-11-18T21:00:00Z'}}}
    return benchmark_cards(view,now,stream=feed.snapshot())[0]


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():await asyncio.sleep(.001)


def test_one_second_coalescing_keeps_the_actual_last_event_not_local_clock():
    feed=ready(now=NOW+timedelta(seconds=1))
    for ms in range(1000):
        feed.accept(trade(at=NOW+timedelta(milliseconds=ms),price=str(99+ms/10000)),feed.segment)
    rows=feed.snapshot()['points']['SPY']
    assert len(rows)==1 and rows[0][0]==int(NOW.timestamp()*1000)+999
    assert rows[0][1]=='99.0999'
    before=deepcopy(feed.latest)
    feed.accept(trade(at=NOW+timedelta(milliseconds=999),price='99.0999',received=NOW+timedelta(seconds=1)),feed.segment)
    assert feed.latest==before  # Receipt of a duplicate cannot renew event time.
    feed.accept(trade(at=NOW+timedelta(milliseconds=999),price='99.1',received=NOW+timedelta(seconds=1)),feed.segment)
    assert len(feed.points['SPY'])==1
    assert feed.latest['SPY']['received_at']==before['SPY']['received_at']


@pytest.mark.parametrize('change',[
    {'price':'NaN'}, {'price':'Infinity'}, {'price':'-1'}, {'price':True},
    {'volume':'0'}, {'currency':'KRW'}, {'timestamp':None},
    {'timestamp':NOW.replace(tzinfo=None).isoformat()},
    {'timestamp':(NOW+timedelta(seconds=1)).isoformat()},
    {'timestamp':(NOW-timedelta(seconds=31)).isoformat()},
])
def test_invalid_delayed_or_ambiguous_trade_does_not_add_or_refresh_a_point(change):
    feed=ready();frame=trade();frame['data'].update(change)
    feed.accept(frame,feed.segment)
    assert not feed.latest and not feed.points['SPY']
    assert feed.counts['invalid']==1


def test_preopen_postclose_and_previous_session_are_not_drawn_as_today():
    for now in (NOW.replace(hour=13),NOW.replace(hour=22)):
        feed=ready(now=now);feed.accept(trade(at=now),feed.segment)
        assert not feed.latest and not feed.snapshot()['points']['SPY']
    feed=ready();feed.accept(trade(),feed.segment)
    feed.clock=lambda:NOW+timedelta(days=1)
    assert feed.snapshot()['points']['SPY']==[]


def test_restart_retains_history_without_freshness_and_rejects_old_generation():
    feed=ready();feed.accept(trade(),feed.segment)
    snapshot=feed.snapshot()
    restored=BenchmarkTradeFeed(None,restored=snapshot,clock=lambda:NOW)
    assert restored.snapshot()['points']==snapshot['points'] and not restored.latest
    assert card(restored)['price']=='$99.00' and not card(restored)['price_fresh']
    restored.acknowledged=True
    restored.accept(trade(),restored.segment)  # Replay cannot create a new session point.
    assert not restored.latest
    restored.clock=lambda:NOW+timedelta(seconds=1)
    restored.accept(trade(at=NOW+timedelta(seconds=1)),feed.segment)
    assert not restored.latest
    restored.accept(trade(at=NOW+timedelta(seconds=1)),restored.segment)
    assert len(restored.points['SPY'])==2 and restored.latest
    snapshot['updated_at']=(NOW+timedelta(seconds=1)).isoformat()
    assert not BenchmarkTradeFeed(None,restored=snapshot,clock=lambda:NOW).points['SPY']


def test_snapshot_is_bounded_and_overwrites_one_control_without_trade_or_tick_journals(tmp_path):
    feed=ready()
    for second in range(3700):
        at=NOW+timedelta(seconds=second);feed.clock=lambda:at
        for symbol in SYMBOLS:feed.accept(trade(symbol,at=at),feed.segment)
    snapshot=feed.snapshot()
    assert all(len(rows)==MAX_POINTS for rows in snapshot['points'].values())
    assert len(json.dumps(snapshot).encode())<500_000
    with closing(Repository(tmp_path/'bounded.db',backend='sqlite')) as repo:
        repo.set_control(VIEW,snapshot);repo.set_control(VIEW,snapshot)
        assert repo.db.execute('SELECT COUNT(*) FROM controls WHERE name=?',(VIEW,)).fetchone()[0]==1
        for kind in ('orders','executions','quote_snapshots','market_bars','research_events'):
            assert repo.all(kind)==[]


def test_chart_joins_trade_samples_across_minutes_but_not_reconnects_or_long_gaps():
    feed=ready()
    for seconds,price in ((-3,'99'),(-2,'101'),(-1,'102'),(0,'101')):
        feed.accept(trade(at=NOW+timedelta(seconds=seconds),price=price),feed.segment)
    html=benchmark_cards_html([card(feed)])
    assert html.count('<polyline')==2 and '<circle' not in html
    assert '빈칸' not in html and '#60a5fa' in html and '#f87171' in html
    feed.invalidate();feed.segment+=1;feed.acknowledged=True;feed.state='CONNECTED'
    feed.clock=lambda:NOW+timedelta(seconds=1)
    feed.accept(trade(at=NOW+timedelta(seconds=1),price='102'),feed.segment)
    html=benchmark_cards_html([card(feed,NOW+timedelta(seconds=1))])
    assert '<circle' in html and '빈칸' in html
    feed.clock=lambda:NOW+timedelta(seconds=20)
    feed.accept(trade(at=NOW+timedelta(seconds=20),price='101'),feed.segment)
    assert benchmark_cards_html([card(feed,NOW+timedelta(seconds=20))]).count('<circle')==2


def test_price_and_publication_staleness_are_independent_and_no_return_without_close():
    feed=ready();feed.accept(trade(),feed.segment)
    assert card(feed)['price_fresh'] and card(feed)['change']=='-1.00%'
    snapshot=feed.snapshot()
    # Server no longer publishing: cached data is never advertised as live.
    later=NOW+timedelta(seconds=11)
    assert not benchmark_cards({},later,symbols=['SPY'],stream=snapshot)[0]['price_fresh']
    feed.clock=lambda:NOW+timedelta(seconds=31)
    stale=card(feed,NOW+timedelta(seconds=31))
    assert stale['price']=='$99.00' and stale['price_status']=='새 체결 대기'
    unknown=benchmark_cards({},NOW,symbols=['SPY'],stream=snapshot)[0]
    assert unknown['change']=='전일 대비 확인 중' and '<svg' not in benchmark_cards_html([unknown])


async def test_real_adapter_reconnects_only_trade_channels_and_old_replay_is_not_fresh(monkeypatch):
    from websockets.asyncio.server import serve
    clients=[];active=[];maximum=[];disconnect=asyncio.Event();new_trade=asyncio.Event()
    now=[NOW]
    class Auth:
        async def access_token(self):return 'fixture'
    async def server(ws):
        active.append(ws);maximum.append(len(active));clients.append(ws)
        try:
            assert json.loads(await ws.recv())==[{'type':'trade:us','codes':list(SYMBOLS)}]
            assert ws.request.headers['Authorization']=='Bearer fixture'
            await ws.send(json.dumps(ack()))
            await ws.send(json.dumps(trade()))
            if len(clients)==1:
                await disconnect.wait();await ws.close()
            else:
                await new_trade.wait();await ws.send(json.dumps(trade(at=now[0],price='101')))
                await ws.wait_closed()
        finally:active.remove(ws)
    async with serve(server,'127.0.0.1',0) as listener:
        port=listener.sockets[0].getsockname()[1]
        monkeypatch.setattr('sessionquant.collectors.toss_websocket.WS_URL',f'ws://127.0.0.1:{port}')
        monkeypatch.setattr('sessionquant.collectors.toss_websocket.now_utc',lambda:now[0])
        feed=BenchmarkTradeFeed(Auth(),clock=lambda:now[0],retry_initial=.01,retry_max=.02)
        task=asyncio.create_task(feed.run())
        try:
            await until(lambda:bool(feed.latest))
            disconnect.set()
            await until(lambda:len(clients)==2 and feed.counts['ignored']>0)
            assert not feed.latest and len(feed.points['SPY'])==1
            now[0]+=timedelta(seconds=1);new_trade.set()
            await until(lambda:bool(feed.latest))
            assert len(feed.points['SPY'])==2 and max(maximum)==1
            assert feed.points['SPY'][0][2]!=feed.points['SPY'][1][2]
        finally:
            disconnect.set();new_trade.set();feed.stop();await asyncio.wait_for(task,2)
        assert feed.state=='STOPPED' and not feed.latest and not active


async def test_partial_ack_backoff_stop_and_missing_ack_never_enable_display():
    class Stream:
        def __init__(self):self.calls=0
        async def stream(self,*args,**kwargs):
            self.calls+=1
            yield ack(['SPY']) if self.calls==1 else trade()
    stream=Stream();feed=BenchmarkTradeFeed(None,stream=stream,clock=lambda:NOW,retry_initial=.1,retry_max=.2)
    task=asyncio.create_task(feed.run())
    await until(lambda:feed.state=='BACKOFF')
    assert feed.retry_seconds==.1 and not feed.latest and not feed.acknowledged
    await until(lambda:stream.calls==2 and feed.state=='BACKOFF')
    assert feed.retry_seconds==.2 and not feed.latest
    feed.stop();await asyncio.wait_for(task,1)
    assert stream.calls==2 and feed.state=='STOPPED'


async def test_stream_profile_rest_budget_is_only_for_previous_closes(tmp_path):
    with closing(Repository(tmp_path/'refs.db',backend='sqlite')) as repo:
        monitor=MarketReferenceMonitor(repo,clock=lambda:NOW,symbols=list(SYMBOLS),display_only=True,
                                       stream_prices=True,calls_per_cycle=2)
        calls=[]
        async def read(stream,symbol):
            calls.append((stream,symbol));return {'data':None,'error':'fixture_missing'}
        for _ in range(2):
            before=len(calls);await monitor.poll(read);assert len(calls)-before<=2
        assert set(calls)=={('daily',symbol) for symbol in SYMBOLS}


def test_display_stream_requires_toss_websocket_and_cannot_replace_risk_reference_inputs():
    from dataclasses import replace
    from sessionquant.session_config import load_session_config
    config=load_session_config('config/sessionquant.strategy-lab-websocket.example.yaml')
    assert config.market_reference['websocket_chart']
    for changes in ({'quote_transport':'rest'},
                    {'market_reference':{**config.market_reference,'display_only':False}},
                    {'market_reference':{**config.market_reference,'symbols':['IEF']}}):
        with pytest.raises(ValueError):replace(config,**changes)
