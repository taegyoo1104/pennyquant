"""Stream delivery through the real Paper engine, including failure paths."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path

import pytest

from sessionquant.collectors.toss_quote_feed import TossQuoteFeed
from sessionquant.database.repository import Repository
from sessionquant.events import parse_event
from sessionquant.market.live_quotes import LiveQuoteDelivery
from sessionquant.market.source import MarketBatch, TossSessionSource
from sessionquant.models import jsonable
from sessionquant.portfolio.selection import VIEW
from sessionquant.session_config import SessionConfig, load_session_config
from sessionquant.session_engine import SessionEngine, event_time
from sessionquant.session_runtime import batch_events
from test_realtime_paper import observed, NOW  # noqa: F401


def receiver(now):
    clock=SimpleNamespace(now=now)
    feed=TossQuoteFeed(object(),clock=lambda:clock.now,max_age_seconds=10)
    feed.set_symbols(['AAPL']);feed._acked=True;feed.state='SUBSCRIBED'
    return feed,clock,LiveQuoteDelivery(feed)


def offer(feed,quote):
    feed._accept(quote,'websocket',feed._generation)


def prepare(value):
    # Reuse real raw minute bars and listing evidence, but the simple strategy
    # deliberately has no RVOL or reference-market prerequisite.
    value.engine.strategy=value.engine.market.strategy='pullback_simple'
    value.engine.use_market_filter=value.engine.market.use_market_filter=False
    rows=[r for r in value.rows if r['kind']!='quote' and event_time(r)<=NOW+timedelta(seconds=1)]
    value.engine.apply_batch(sorted(rows,key=event_time),NOW+timedelta(seconds=1))
    return next(parse_event(r).data for r in value.rows if r['kind']=='quote' and r['data']['symbol']=='AAPL')


def test_stream_batch_passes_heartbeat_watermark_without_forging_receipt(observed):
    quote=prepare(observed)
    now=NOW+timedelta(seconds=3)
    feed,clock,delivery=receiver(now)
    observed.engine.quote_entry_guard=lambda s,t:delivery.entry_allowed(s,t,known_quotes=observed.engine.market.quotes)
    quote=replace(quote,timestamp=NOW+timedelta(seconds=1),received_at=NOW+timedelta(seconds=1),exchange='UNKNOWN')
    offer(feed,quote)
    observed.engine.tick(now)
    events=delivery.events(['AAPL'],now)
    assert event_time(events[0])==now and events[0]['data']['received_at']==quote.received_at.isoformat()
    observed.engine.apply_batch(events,now)
    assert len(observed.repo.all('order_intents'))==1 and not observed.engine.broker.fills
    assert delivery.events(['AAPL'],now)==[]
    clock.now=now+timedelta(seconds=2)
    offer(feed,replace(quote,timestamp=clock.now,received_at=clock.now))
    observed.engine.apply_batch(delivery.events(['AAPL'],clock.now),clock.now)
    assert [fill.side for fill in observed.engine.broker.fills]==['buy']
    # A new connection or newly received duplicate is not new liquidity.
    feed._invalidate('DISCONNECTED');feed._acked=True
    offer(feed,replace(quote,timestamp=clock.now,received_at=clock.now))
    assert delivery.events(['AAPL'],clock.now)==[]
    assert len(observed.repo.all('order_intents'))==1


@pytest.mark.parametrize('cancel_delay',[0,5])
def test_disconnect_cancels_pending_buy_and_rest_cannot_reenter(observed,cancel_delay):
    observed.engine.broker.settings=replace(observed.engine.broker.settings,paper_cancel_latency_seconds=str(cancel_delay))
    quote=prepare(observed);now=NOW+timedelta(seconds=2)
    feed,clock,delivery=receiver(now)
    observed.engine.quote_entry_guard=lambda s,t:delivery.entry_allowed(s,t,known_quotes=observed.engine.market.quotes)
    offer(feed,replace(quote,timestamp=now,received_at=now))
    observed.engine.apply_batch(delivery.events(['AAPL'],now),now)
    assert observed.engine.broker.get_open_orders()
    feed._invalidate('DISCONNECTED');now+=timedelta(seconds=2)
    rest=replace(quote,timestamp=now,received_at=now)
    observed.engine.apply_batch([{'kind':'quote','data':jsonable(rest)}],now)
    assert not observed.engine.broker.fills
    if cancel_delay:
        assert observed.engine.broker.get_open_orders()[0].status=='cancel_pending'
    observed.engine.tick(now+timedelta(seconds=cancel_delay))
    assert not observed.engine.broker.get_open_orders()
    row=observed.repo.get_control(VIEW)['rows'][0]
    assert 'websocket_quote_unavailable' in row['blockers']
    assert len(observed.repo.all('order_intents'))==1


def test_risk_rest_quote_exits_held_symbol_when_feed_disconnected(observed):
    quote=prepare(observed);now=NOW+timedelta(seconds=2)
    feed,clock,delivery=receiver(now)
    observed.engine.quote_entry_guard=lambda s,t:delivery.entry_allowed(s,t,known_quotes=observed.engine.market.quotes)
    for offset in (0,2):
        clock.now=now+timedelta(seconds=offset)
        offer(feed,replace(quote,timestamp=clock.now,received_at=clock.now))
        observed.engine.apply_batch(delivery.events(['AAPL'],clock.now),clock.now)
    assert 'AAPL' in observed.engine.broker.positions
    feed._invalidate('DISCONNECTED')
    # No focus membership. The held position remains in risk subscriptions.
    watchlist=SimpleNamespace(light_symbols=lambda t:['MSFT'],focus=lambda t:['MSFT'])
    assert delivery.synchronize(watchlist,observed.engine.broker,clock.now)==['AAPL','MSFT']
    assert set(feed._desired)=={'AAPL','MSFT'}
    pos=observed.engine.broker.positions['AAPL']
    observed.engine.broker.exit_intents['AAPL']={'reason':'manual_market_sell','opened_at':pos.opened_at.isoformat(),
        'requested_at':clock.now.isoformat(),'attempt':0}
    for offset in (4,6):
        stamp=now+timedelta(seconds=offset)
        q=replace(quote,timestamp=stamp,received_at=stamp)
        observed.engine.tick(stamp)
        observed.engine.apply_batch(batch_events(MarketBatch(quotes=[q]),delivered_at=stamp,
            last_time=observed.engine.last_time,delayed_quotes=True),stamp)
    assert [fill.side for fill in observed.engine.broker.fills]==['buy','sell']
    assert not observed.engine.broker.positions


def test_profile_relaxes_only_cross_symbol_alignment(tmp_path):
    from test_stage08_selection import session, batch_for
    c=load_session_config(Path('config/sessionquant.strategy-lab-websocket.example.yaml'))
    assert c.quote_transport=='websocket' and c.db_path.name=='sessionquant-strategy-lab-ws-v1.db'
    assert SessionConfig().quote_transport=='rest'
    with pytest.raises(ValueError):replace(c,watchlist_size=51)
    with pytest.raises(ValueError):replace(c,paper_data_policy='strict')
    repo,engine,market=session(tmp_path,options={'quote_alignment_seconds':None},symbols=('AAPL','MSFT'))
    try:
        market.quotes['AAPL']=replace(market.quotes['AAPL'],timestamp=NOW-timedelta(seconds=3),received_at=NOW-timedelta(seconds=3))
        engine.apply_batch(batch_for(market),NOW)
        assert repo.get_control(VIEW)['approved_new_count']==2
        later=NOW+timedelta(seconds=12)
        engine.apply_batch([{'kind':'quote','data':jsonable(replace(market.quotes['AAPL'],received_at=later))}],later)
        row=next(r for r in repo.get_control(VIEW)['rows'] if r['symbol']=='AAPL')
        assert 'candidate_quote_stale_or_unknown' in row['blockers']
    finally:repo.close()


async def test_source_skips_candidate_rest_books_but_rotates_candles_and_falls_back_for_risk(tmp_path):
    from watchlist_fixtures import context, observe, NOW as stamp
    from test_watchlist_source import Market
    repo,universe,config,provider,manager=context(tmp_path,size=50)
    from sessionquant.watchlist.selection import WatchlistManager
    config=replace(config,strategy='pullback_simple',focus_selection='turnover')
    manager=WatchlistManager(repo,universe,config,provider=provider,clock=lambda:stamp,allow_synthetic=True)
    try:
        await manager.refresh(stamp);observe(manager)
        client=Market(lambda:stamp+timedelta(seconds=60))
        feed=SimpleNamespace(latest=lambda s,t:None)
        source=TossSessionSource(client,clock=client.clock,watchlist=manager,include_benchmarks=False,
            quote_feed=feed,watchlist_candles_per_cycle=0,history_pages_per_cycle=0)
        await source.poll([],priority_symbols=['T001'])
        first={s for method,s in client.calls if method=='candles'}
        assert [s for method,s in client.calls if method=='quote']==['T001']
        client.calls.clear()
        feed.latest=lambda s,t:object() if s=='T001' else None
        await source.poll([],priority_symbols=['T001'])
        assert not [s for method,s in client.calls if method=='quote']
        second={s for method,s in client.calls if method=='candles'}
        assert first-second and second-first
        assert manager.state['candidate_quote_reserved_seconds']==0
    finally:repo.close()


@pytest.mark.parametrize('chart_enabled',[False,True])
async def test_runtime_delivers_while_rest_poll_is_blocked_and_cleans_shared_client(tmp_path,monkeypatch,chart_enabled):
    from sessionquant.market.universe import UniverseStore
    from sessionquant.session_runtime import run_session
    from sessionquant.watchlist.selection import WatchlistManager
    now=datetime.now(timezone.utc)
    universe=tmp_path/'universe.json'
    UniverseStore(universe).refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=now)
    delivered=asyncio.Event();closed=[];feeds=[];frames=[];display_feeds=[]
    class Market:
        def __init__(self):self.client=self
        async def poll(self,*a,**k):
            await asyncio.wait_for(delivered.wait(),8)
            return MarketBatch()
        async def quote(self,symbol):await asyncio.Event().wait()
        async def aclose(self):closed.append(True)
    class Stream:
        async def stream(self,symbols,**kwargs):
            yield {'type':'subscriptions','subscribed':['orderbook:us:AAPL'],'rejected':[]}
            stamp=datetime.now(timezone.utc).isoformat()
            yield {'type':'message','topic':'orderbook:us:AAPL','received_at':stamp,
                'data':{'timestamp':stamp,'currency':'USD','bids':[{'price':'100','volume':'1000'}],
                        'asks':[{'price':'100.01','volume':'1000'}]}}
            await asyncio.Event().wait()
    market=Market()
    from sessionquant.market_reference.trade_feed import BenchmarkTradeFeed
    class DisplayStream:
        async def stream(self,symbols,**kwargs):
            assert kwargs['trades'] is True and kwargs['orderbook'] is False
            yield {'type':'subscriptions','subscribed':[f'trade:us:{s}' for s in symbols],'rejected':[]}
            stamp=datetime.now(timezone.utc).isoformat()
            yield {'type':'message','topic':'trade:us:SPY','received_at':stamp,
                   'data':{'timestamp':stamp,'currency':'USD','price':'100','volume':'1'}}
            await asyncio.Event().wait()
    def make_display(client,**kwargs):
        assert client is market.client
        feed=BenchmarkTradeFeed(client,stream=DisplayStream(),**kwargs)
        display_feeds.append(feed);return feed
    def make_feed(client,**kwargs):
        assert client is market.client
        feed=TossQuoteFeed(client,stream=Stream(),**kwargs);feeds.append(feed);return feed
    apply=SessionEngine.apply_batch
    def capture(self,events,stamp,**kwargs):
        result=apply(self,events,stamp,**kwargs)
        if any(r['kind']=='quote' for r in events):frames.extend(events);delivered.set()
        return result
    monkeypatch.setattr('sessionquant.session_runtime.TossSessionSource',lambda **k:market)
    monkeypatch.setattr('sessionquant.collectors.toss_quote_feed.TossQuoteFeed',make_feed)
    monkeypatch.setattr('sessionquant.market_reference.trade_feed.BenchmarkTradeFeed',make_display)
    monkeypatch.setattr(WatchlistManager,'light_symbols',lambda self,t:['AAPL'])
    monkeypatch.setattr(WatchlistManager,'focus',lambda self,t:['AAPL'])
    monkeypatch.setattr(SessionEngine,'apply_batch',capture)
    c=SessionConfig(database_backend='sqlite',db_path=tmp_path/'runtime.db',universe_path=universe,
        quote_transport='websocket',strategy='pullback_simple',paper_data_policy='observed_paper_v1',
        news_enabled=False,actual_account_monitor_enabled=False,market_filter_enabled=False,focus_selection='turnover',
        market_reference={'websocket_chart':chart_enabled,'display_only':True,'symbols':['SPY','QQQ','SOXX']})
    await asyncio.wait_for(run_session(c,no_dashboard=True,max_cycles=1,desktop_notifications=False),12)
    assert frames and frames[0]['delivered_at']>frames[0]['data']['received_at']
    assert feeds[0].state=='STOPPED' and closed==[True]
    assert len(display_feeds)==int(chart_enabled)
    assert all(feed.state=='STOPPED' for feed in display_feeds)
    assert all(row['data'].get('symbol')=='AAPL' for row in frames if row['kind']=='quote')
    repo=Repository(c.db_path,backend='sqlite')
    try:
        assert repo.get_control('sq_runtime')['identity']=='toss-readonly-websocket-v1'
        status=repo.get_control('websocket_quotes_v1')
        assert status['state']=='STOPPED' and status['delivery_batches']==1
        assert status['selected_fresh_count']==0
        display=repo.get_control('benchmark_trade_view_v1')
        assert display['enabled']==chart_enabled
        if chart_enabled:assert display['state']=='STOPPED' and not display['latest']
    finally:repo.close()


def test_disconnect_recording_replays_pending_buy_cancellation(observed,tmp_path,monkeypatch):
    from sessionquant.research import journal
    from sessionquant.research.bundle import export_recording
    from sessionquant.research.replay import replay_recording
    observed.engine.strategy=observed.engine.market.strategy='pullback_simple'
    observed.engine.use_market_filter=observed.engine.market.use_market_filter=False
    feed,clock,delivery=receiver(NOW)
    monkeypatch.setattr(journal,'clock',lambda:clock.now)
    observed.engine.quote_entry_guard=lambda s,t:delivery.entry_allowed(s,t,known_quotes=observed.engine.market.quotes)
    journal.start(observed.repo,observed.engine,source='SYNTHETIC_WEBSOCKET')
    observed.repo.record('universe_history',observed.universe.snapshots[0])
    clock.now+=timedelta(seconds=1)
    quote=prepare(observed)
    # Receipt precedes delivery, and recording must preserve both clocks.
    offer(feed,replace(quote,timestamp=clock.now,received_at=clock.now))
    clock.now+=timedelta(seconds=1)
    observed.engine.apply_batch(delivery.events(['AAPL'],clock.now),clock.now)
    assert observed.engine.broker.get_open_orders()
    feed._invalidate('DISCONNECTED');clock.now+=timedelta(seconds=1)
    observed.engine.tick(clock.now)
    assert not observed.engine.broker.get_open_orders() and not observed.engine.broker.fills
    journal.stop(observed.repo)
    export_recording(observed.repo,tmp_path/'ws-recording')
    replay=replay_recording(tmp_path/'ws-recording',tmp_path/'ws-replay.db',use_market_filter=False)
    assert replay['state']==observed.engine.broker.to_state()
    # A real process restart loses feed memory, but the emitted/cancelled setup
    # remains consumed even after a valid new connection restores entry access.
    feed,clock,delivery=receiver(clock.now+timedelta(seconds=1))
    restored=SessionEngine(observed.repo,observed.universe,synthetic=True,strategy='pullback_simple',
        strict_market_data=True,paper_data_policy='observed_paper_v1',use_market_filter=False,
        candidate_settings={'enabled':True},restore=True)
    restored.quote_entry_guard=lambda s,t:delivery.entry_allowed(s,t,known_quotes=restored.market.quotes)
    for _ in range(2):
        offer(feed,replace(quote,timestamp=clock.now,received_at=clock.now))
        restored.apply_batch(delivery.events(['AAPL'],clock.now,known_quotes=restored.market.quotes),clock.now)
        assert delivery.entry_allowed('AAPL',clock.now,known_quotes=restored.market.quotes)
        clock.now+=timedelta(seconds=1)
    assert not restored.broker.get_open_orders() and not restored.broker.fills
    assert len(observed.repo.all('order_intents'))==1


def test_fifty_cached_books_deliver_only_focus_and_focus_change_uses_latest():
    from sessionquant.models import Quote
    pool=[f'T{i:02}' for i in range(50)]
    now=NOW+timedelta(seconds=2)
    feed,clock,delivery=receiver(now)
    watchlist=SimpleNamespace(light_symbols=lambda t:pool,focus=lambda t:pool[:10])
    broker=SimpleNamespace(positions={},exit_intents={},get_open_orders=lambda:[])
    selected=delivery.synchronize(watchlist,broker,now)
    generation=feed._generation;feed._acked=True
    for i in range(100):
        stamp=NOW+timedelta(milliseconds=i)
        for symbol in pool:
            offer(feed,Quote(symbol,stamp,stamp,100,100.01,1000,1000))
    events=delivery.events(selected,now)
    assert len(events)==10 and len(feed._quotes)==50
    assert all(r['data']['timestamp']==stamp.isoformat() for r in events)
    assert delivery.events(selected,now)==[]
    watchlist.focus=lambda t:pool[10:20]
    selected=delivery.synchronize(watchlist,broker,now)
    assert feed._generation==generation
    assert {r['data']['symbol'] for r in delivery.events(selected,now)}==set(pool[10:20])


@pytest.mark.parametrize('fault',['older','conflicting'])
@pytest.mark.parametrize('restart',[False,True])
def test_reconnect_bad_book_cannot_unlock_cached_engine_quote_for_delayed_bar(observed,fault,restart):
    engine=observed.engine
    engine.strategy=engine.market.strategy='pullback_simple'
    engine.use_market_filter=engine.market.use_market_filter=False
    now=NOW+timedelta(seconds=1)
    feed,clock,delivery=receiver(now)
    engine.quote_entry_guard=lambda s,t:delivery.entry_allowed(s,t,known_quotes=engine.market.quotes)
    quote=next(parse_event(r).data for r in observed.rows if r['kind']=='quote' and r['data']['symbol']=='AAPL')
    quote=replace(quote,timestamp=now,received_at=now)
    offer(feed,quote)
    tail=next(r for r in observed.rows if r['kind']=='bar' and r['data']['symbol']=='AAPL'
        and r['data']['timestamp']==NOW.isoformat())
    initial=[r for r in observed.rows if r['kind']!='quote' and event_time(r)<=now and r!=tail]
    engine.apply_batch(sorted(initial+delivery.events(['AAPL'],now),key=event_time),now)
    assert not observed.repo.all('order_intents')
    now+=timedelta(seconds=1)
    if restart:
        # Feed ordering memory disappears on process restart; engine state does not.
        feed,clock,delivery=receiver(now)
        engine=SessionEngine(observed.repo,observed.universe,synthetic=True,strategy='pullback_simple',
            strict_market_data=True,paper_data_policy='observed_paper_v1',use_market_filter=False,
            candidate_settings={'enabled':True},restore=True)
        engine.quote_entry_guard=lambda s,t:delivery.entry_allowed(s,t,known_quotes=engine.market.quotes)
    else:
        clock.now=now;feed._invalidate('DISCONNECTED');feed._generation+=1;feed._acked=True
    bad=(replace(quote,timestamp=quote.timestamp-timedelta(seconds=1),received_at=now) if fault=='older'
        else replace(quote,bid=quote.bid+5,ask=quote.ask+5,received_at=now))
    offer(feed,bad)
    assert not delivery.entry_allowed('AAPL',now,known_quotes=engine.market.quotes)
    assert delivery.events(['AAPL'],now,known_quotes=engine.market.quotes)==[]
    delayed=MarketBatch(bars=[parse_event(tail).data])
    engine.apply_batch(batch_events(delayed,delivered_at=now,last_time=engine.last_time),now)
    assert not observed.repo.all('order_intents')
    assert 'websocket_quote_unavailable' in observed.repo.get_control(VIEW)['rows'][0]['blockers']
    # A genuinely newer valid book reopens eligibility; same setup still submits once.
    clock.now=now+timedelta(seconds=1)
    offer(feed,replace(quote,timestamp=clock.now,received_at=clock.now))
    assert not delivery.entry_allowed('AAPL',clock.now,known_quotes=engine.market.quotes)
    engine.apply_batch(delivery.events(['AAPL'],clock.now,known_quotes=engine.market.quotes),clock.now)
    assert delivery.entry_allowed('AAPL',clock.now,known_quotes=engine.market.quotes)
    assert len(observed.repo.all('order_intents'))==1
    assert delivery.events(['AAPL'],clock.now,known_quotes=engine.market.quotes)==[]


@pytest.mark.parametrize('fault',['reconnect','conflict_then_newer'])
def test_newer_book_must_reach_engine_after_disconnect_or_conflict_before_delayed_bar_can_enter(observed,fault):
    engine=observed.engine
    engine.strategy=engine.market.strategy='pullback_simple'
    engine.use_market_filter=engine.market.use_market_filter=False
    now=NOW+timedelta(seconds=1)
    feed,clock,delivery=receiver(now)
    engine.quote_entry_guard=lambda s,t:delivery.entry_allowed(s,t,known_quotes=engine.market.quotes)
    quote=next(parse_event(r).data for r in observed.rows if r['kind']=='quote' and r['data']['symbol']=='AAPL')
    quote=replace(quote,timestamp=now,received_at=now)
    offer(feed,quote)
    tail=next(r for r in observed.rows if r['kind']=='bar' and r['data']['symbol']=='AAPL'
        and r['data']['timestamp']==NOW.isoformat())
    initial=[r for r in observed.rows if r['kind']!='quote' and event_time(r)<=now and r!=tail]
    engine.apply_batch(sorted(initial+delivery.events(['AAPL'],now),key=event_time),now)
    assert delivery.entry_allowed('AAPL',now,known_quotes=engine.market.quotes)
    assert not observed.repo.all('order_intents')
    clock.now=now+timedelta(seconds=1)
    if fault=='reconnect':
        feed._invalidate('DISCONNECTED');feed._generation+=1;feed._acked=True
    else:
        # Both frames arrive before the next engine heartbeat/guard invocation.
        offer(feed,replace(quote,bid=quote.bid+5,ask=quote.ask+5,received_at=clock.now))
    offer(feed,replace(quote,timestamp=clock.now,received_at=clock.now))
    assert feed.latest('AAPL',clock.now) is not None
    assert not delivery.entry_allowed('AAPL',clock.now,known_quotes=engine.market.quotes)
    delayed=MarketBatch(bars=[parse_event(tail).data])
    engine.apply_batch(batch_events(delayed,delivered_at=clock.now,last_time=engine.last_time),clock.now)
    assert not observed.repo.all('order_intents')
    engine.apply_batch(delivery.events(['AAPL'],clock.now,known_quotes=engine.market.quotes),clock.now)
    assert len(observed.repo.all('order_intents'))==1
    clock.now+=timedelta(seconds=1)
    offer(feed,replace(quote,timestamp=clock.now,received_at=clock.now))
    # Once this connection has supplied the engine, normal batching doesn't
    # cancel its pending order merely because the next stream update is newer.
    assert delivery.entry_allowed('AAPL',clock.now,known_quotes=engine.market.quotes)
    engine.apply_batch(delivery.events(['AAPL'],clock.now,known_quotes=engine.market.quotes),clock.now)
    assert len(observed.repo.all('order_intents'))==1
    assert [fill.side for fill in engine.broker.fills]==['buy']
    watchlist=SimpleNamespace(light_symbols=lambda t:['MSFT'],focus=lambda t:['MSFT'])
    broker=SimpleNamespace(positions={},exit_intents={},get_open_orders=lambda:[])
    delivery.synchronize(watchlist,broker,clock.now)
    assert not delivery._entry_generations
