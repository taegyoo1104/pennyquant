"""Late startup repairs history before selection; exits and audit clocks remain independent."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from sessionquant.database.repository import Repository
from sessionquant.market.bars import session_bounds
from sessionquant.market.history import HistoryStore, history_plan, record_id
from sessionquant.market.warmup import StartupWarmup, CONTROL
from sessionquant.watchlist.history_selection import DailyHistorySelection, CONTROL as SELECTION
from sessionquant.trading_controls import begin_session, heartbeat, request_command, enqueue
from test_history_selection import prepare
from test_watchlist_history import minute_rows
from test_realtime_paper import observed, enter, NOW as SIGNAL_NOW  # noqa: F401
from sessionquant.models import jsonable

NOW = datetime(2026,11,16,18,30,2,tzinfo=timezone.utc)


def setup(tmp_path, symbols=('AAPL',), now=NOW):
    repo=Repository(tmp_path/'warmup.db',backend='sqlite')
    store=HistoryStore(repo)
    selection=DailyHistorySelection(repo,startup_pending=True)
    control=begin_session(repo,mode='paper',now=now)
    clock=SimpleNamespace(now=now)
    worker=StartupWarmup(repo,store,selection,control,clock=lambda:clock.now)
    manager=SimpleNamespace(allow_synthetic=False,base_symbols=lambda _:list(symbols),
        store=SimpleNamespace(status=lambda *args,**kwargs:{'status':'VALID'}))
    return repo,store,selection,control,clock,worker,manager


class Provider:
    def __init__(self, clock):
        self.clock=clock;self.calls=[];self.omit={};self.known={}
    async def fetch(self,symbol,cursor):
        self.calls.append((symbol,cursor))
        start,end=session_bounds(cursor)
        count=int((min(cursor,end)-start).total_seconds()/60)
        rows=minute_rows(symbol,start,count=count,known=self.clock.now)
        rows=[b for b in rows if b.timestamp not in self.omit.get(symbol,set())]
        page=rows[-200:]
        following=page[0].timestamp-timedelta(minutes=1) if len(rows)>200 else None
        return page,following
    def accept(self,symbol,rows):
        self.known.setdefault(symbol,set()).update(b.timestamp for b in rows)
    def seen(self,symbol):return self.known.get(symbol,set())


async def finish(worker,manager,provider):
    for _ in range(100):
        await worker.advance(manager,provider.fetch,provider.accept,provider.seen)
        if worker.control.get('data_ready'):return
    pytest.fail('warm-up did not finish')


async def test_late_start_repairs_yesterday_and_today_before_freezing(tmp_path):
    repo,store,selection,control,clock,worker,manager=setup(tmp_path)
    try:
        prepare(store,'AAPL',NOW,omit_last=True,known=NOW-timedelta(minutes=1))
        day,_,_=history_plan(NOW)[-1]
        original=store.get('AAPL',day)[1][0]
        assert selection.allowed(['AAPL'],NOW)==[]
        assert repo.get_control(SELECTION) is None
        provider=Provider(clock)
        await worker.advance(manager,provider.fetch,provider.accept,provider.seen,max_pages=1)
        assert not control['data_ready'] and selection.startup_pending
        await finish(worker,manager,provider)
        assert control['data_ready'] and not control['running']
        assert worker.report['status']=='READY' and selection.allowed(['AAPL'],NOW)==['AAPL']
        assert repo.get_control(SELECTION)['frozen']
        assert store.get('AAPL',day)[1][0].received_at==original.received_at
        # Two current-day pages for each of AAPL/SPY/QQQ and one missing historical day.
        assert len(provider.calls)==7
        assert len(provider.seen('AAPL'))==240
        assert repo.all('orders')==[]
    finally:repo.close()


async def test_unavailable_minutes_remain_excluded_without_fabrication(tmp_path):
    repo,store,selection,_,clock,worker,manager=setup(tmp_path,('AAPL','MSFT'))
    try:
        prepare(store,'AAPL',NOW)
        prepare(store,'MSFT',NOW,omit_last=True)
        day,_,end=history_plan(NOW)[-1]
        provider=Provider(clock);provider.omit['MSFT']={end}
        await finish(worker,manager,provider)
        assert worker.report['status']=='PARTIAL'
        assert selection.allowed(['AAPL','MSFT'],NOW)==['AAPL']
        assert any(r['symbol']=='MSFT' and r['missing_minutes']==1 for r in worker.report['issues'])
        assert not store.get('MSFT',day)[0]['quality']['complete']
        assert not any(symbol=='MSFT' and cursor.date()==NOW.date() for symbol,cursor in provider.calls)
    finally:repo.close()


async def test_restart_reuses_completed_history_but_hydrates_current_engine(tmp_path):
    repo,store,_,_,clock,worker,manager=setup(tmp_path)
    try:
        prepare(store,'AAPL',NOW)
        provider=Provider(clock)
        await finish(worker,manager,provider)
        # Simulate a process lost after its DB writes: engine memory is empty.
        other=StartupWarmup(repo,HistoryStore(repo),DailyHistorySelection(repo,startup_pending=True),
                            begin_session(repo,mode='paper',now=NOW),clock=lambda:clock.now)
        again=Provider(clock)
        await finish(other,manager,again)
        assert not again.calls
        assert len(again.seen('AAPL'))==240
        assert repo.get_control(CONTROL)['history_ready_count']==1
    finally:repo.close()


async def test_retry_keeps_gate_closed_and_progress_is_bounded(tmp_path):
    repo,_,selection,control,clock,worker,manager=setup(tmp_path)
    try:
        async def unavailable(*_):return None
        await worker.advance(manager,unavailable,lambda *_:None,lambda _:set())
        assert not control['data_ready'] and worker.report['status']=='RETRY_WAIT'
        assert worker.report['completed_tasks']==0 and selection.startup_pending
        heartbeat(repo,control,now=NOW)
        assert control['status']=='WARMING'
        with pytest.raises(ValueError):
            request_command(repo,'start',run_id=control['run_id'],environment='PAPER_LOCAL',now=NOW)
    finally:repo.close()


@pytest.mark.parametrize('offsets',[(11,),(11,300)])
async def test_sparse_holes_query_from_missing_minutes_not_session_close(tmp_path,offsets):
    sunday=datetime(2026,11,15,12,tzinfo=timezone.utc)
    repo,store,selection,_,clock,worker,manager=setup(tmp_path,now=sunday)
    try:
        prepare(store,'AAPL',sunday)
        day,opened,_=history_plan(sunday)[-1]
        row,bars=store.get('AAPL',day)
        missing={opened+timedelta(minutes=i) for i in offsets}
        repo.record('market_history_sessions',{**row,'bars':jsonable([b for b in bars if b.timestamp not in missing])})
        store.cache.pop(record_id('AAPL',day))
        provider=Provider(clock)
        await finish(worker,manager,provider)
        assert provider.calls==[('AAPL',stamp) for stamp in sorted(missing,reverse=True)]
        assert selection.allowed(['AAPL'],sunday)==['AAPL']
        assert worker.report['status']=='READY'
    finally:repo.close()


async def test_current_gap_uses_cache_and_one_missing_tail_request(tmp_path):
    repo,store,_,_,clock,worker,manager=setup(tmp_path)
    try:
        prepare(store,'AAPL',NOW)
        opened,_=session_bounds(NOW)
        old=NOW-timedelta(minutes=120)
        store.ingest(minute_rows('AAPL',opened,count=120,known=old))
        for symbol in ('SPY','QQQ'):
            store.ingest(minute_rows(symbol,opened,count=240,known=NOW))
        provider=Provider(clock)
        await finish(worker,manager,provider)
        assert provider.calls==[('AAPL',NOW.replace(second=0))]
        assert len(provider.seen('AAPL'))==240
        assert store.get('AAPL','2026-11-16')[1][0].received_at==old
    finally:repo.close()


async def test_cached_conflicts_and_future_receipts_cannot_become_ready(tmp_path):
    sunday=datetime(2026,11,15,12,tzinfo=timezone.utc)
    repo,store,selection,_,clock,worker,manager=setup(tmp_path,now=sunday)
    try:
        prepare(store,'AAPL',sunday)
        day,_,_=history_plan(sunday)[-1]
        first=store.get('AAPL',day)[1][0]
        store.save('AAPL',day,[replace(first,volume=first.volume+1)])
        provider=Provider(clock)
        await finish(worker,manager,provider)
        assert not provider.calls and not selection.allowed(['AAPL'],sunday)
        assert worker.report['status']=='PARTIAL'
        # A future receipt must still be fetched; stored quality flags are not proof.
        day,_,_=history_plan(sunday)[-2]
        row,bars=store.get('AAPL',day)
        repo.record('market_history_sessions',{**row,'bars':jsonable([
            replace(b,received_at=sunday+timedelta(hours=1)) if i==10 else b for i,b in enumerate(bars)])})
        other=StartupWarmup(repo,HistoryStore(repo),DailyHistorySelection(repo,startup_pending=True),
                            begin_session(repo,mode='paper',now=sunday),clock=lambda:sunday)
        again=Provider(clock)
        await finish(other,manager,again)
        assert again.calls==[('AAPL',bars[10].timestamp)]
        assert other.report['status']=='PARTIAL'
    finally:repo.close()


def test_cached_bar_availability_does_not_rewrite_receipt_or_accept_future(observed):
    from sessionquant.session_engine import event_time
    bar=next(row for row in observed.rows if row['kind']=='bar')
    restored={**bar,'restored_at':SIGNAL_NOW.isoformat()}
    assert event_time(restored)==SIGNAL_NOW
    assert restored['data']['received_at']==bar['data']['received_at']
    for bad in ({**restored,'restored_at':'2000-01-01T00:00:00+00:00'},
                {**restored,'kind':'quote'}):
        with pytest.raises(ValueError):event_time(bad)
    with pytest.raises(ValueError):
        observed.engine.apply_batch([{**restored,'restored_at':(SIGNAL_NOW+timedelta(seconds=1)).isoformat()}],SIGNAL_NOW)


async def test_weekend_and_next_session_use_completed_exchange_days(tmp_path):
    sunday=datetime(2026,11,15,12,tzinfo=timezone.utc)
    repo,store,_,control,clock,worker,manager=setup(tmp_path,now=sunday)
    try:
        prepare(store,'AAPL',sunday)
        provider=Provider(clock)
        await finish(worker,manager,provider)
        assert control['data_ready'] and not provider.calls
        assert worker.report['session']=='2026-11-16'
        clock.now=datetime(2026,11,17,14,tzinfo=timezone.utc)
        await worker.advance(manager,provider.fetch,provider.accept,provider.seen,max_pages=1)
        assert not control['data_ready'] and worker.report['session']=='2026-11-17'
        await finish(worker,manager,provider)
        assert all(cursor.date().isoformat()=='2026-11-16' for _,cursor in provider.calls)
    finally:repo.close()


def test_engine_rejects_queued_start_and_buy_while_warming(observed):
    from sessionquant.research import journal
    journal.start(observed.repo,observed.engine,source='fixture')
    control=begin_session(observed.repo,mode='paper',now=SIGNAL_NOW)
    control.update(running=True,data_ready=False)
    observed.engine.control_state=control
    request=enqueue(observed.repo,'start',run_id=control['run_id'],now=SIGNAL_NOW)
    decision=enter(observed)
    assert decision.signal is not None
    assert observed.repo.get_record('order_intents',request['id'])['status']=='rejected'
    assert observed.repo.all('orders')==[] and control['status']=='WARMING'
    batches=[r for r in observed.repo.all('research_events') if r['kind']=='engine_batch']
    assert batches and all(r['data']['manual_halt'] for r in batches)


def test_recording_replay_preserves_warmup_gate(observed,tmp_path,monkeypatch):
    from sessionquant.research import journal
    from sessionquant.research.bundle import export_recording
    from sessionquant.research.replay import replay_recording
    from sessionquant.session_engine import event_time
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite');monkeypatch.setenv('SQ_ENVIRONMENT','PAPER_LOCAL')
    clock=SimpleNamespace(now=SIGNAL_NOW)
    monkeypatch.setattr(journal,'clock',lambda:clock.now)
    journal.start(observed.repo,observed.engine,source='fixture')
    for snapshot in observed.universe.snapshots:
        observed.repo.record('universe_history',snapshot)
    control=begin_session(observed.repo,mode='paper',now=clock.now)
    control.update(running=True,data_ready=False)
    observed.engine.control_state=control
    clock.now=SIGNAL_NOW+timedelta(seconds=1)
    enter(observed)
    assert observed.repo.all('orders')==[]
    cached=next(row for row in observed.rows if row['kind']=='bar')
    observed.engine.apply_batch([{**cached,'restored_at':clock.now.isoformat()}],clock.now)
    frames=[row for row in observed.repo.all('research_events') if row['kind']=='engine_batch']
    restored=next(raw for frame in frames for raw in frame['data']['events'] if 'restored_at' in raw)
    assert restored['data']['received_at']==cached['data']['received_at']
    assert restored['timing']['received_at']==cached['data']['received_at']
    control['data_ready']=True
    clock.now=SIGNAL_NOW+timedelta(seconds=2)
    observed.engine.apply_batch([r for r in observed.rows if event_time(r)==clock.now],clock.now)
    assert observed.repo.all('orders')
    journal.stop(observed.repo)
    export_recording(observed.repo,tmp_path/'recording')
    replay=replay_recording(tmp_path/'recording',tmp_path/'replay.db')
    assert replay['state']==observed.engine.broker.to_state()
    assert replay['live_order_writes']==replay['llm_api_calls']==0


def test_warming_never_blocks_existing_position_stop_loss(observed):
    from sessionquant.session_engine import event_time
    enter(observed)
    stamp=SIGNAL_NOW+timedelta(seconds=2)
    observed.engine.apply_batch([r for r in observed.rows if event_time(r)==stamp],stamp)
    assert [f.side for f in observed.engine.broker.fills]==['buy']
    control=begin_session(observed.repo,mode='paper',now=stamp)
    control['data_ready']=False
    observed.engine.control_state=control
    quote=observed.engine.market.quotes['AAPL']
    for seconds in (3,4):
        now=SIGNAL_NOW+timedelta(seconds=seconds)
        current=replace(quote,timestamp=now,received_at=now,bid=100,ask=100.02)
        observed.engine.apply_batch([{'kind':'quote','data':jsonable(current)}],now)
    assert [f.side for f in observed.engine.broker.fills]==['buy','sell']
    assert not observed.engine.broker.positions and control['data_ready'] is False


async def test_source_prioritizes_risk_and_respects_budget_during_warmup(tmp_path):
    from watchlist_fixtures import context, NOW as stamp
    from test_watchlist_source import Market
    from sessionquant.market.source import TossSessionSource
    repo,_,_,_,manager=context(tmp_path,size=2)
    try:
        await manager.refresh(stamp)
        manager.history_selection=DailyHistorySelection(repo,startup_pending=True)
        control=begin_session(repo,mode='paper',now=stamp)
        worker=StartupWarmup(repo,HistoryStore(repo),manager.history_selection,control,clock=lambda:stamp)
        class Client(Market):
            async def candles(self,symbol,**kwargs):
                self.calls.append(('backfill' if kwargs.get('before') else 'candles',symbol))
                return [],None
        client=Client(lambda:stamp)
        seen=[]
        source=TossSessionSource(client,watchlist=manager,clock=lambda:stamp,max_api_calls=8,
            history_store=worker.store,warmup=worker,on_priority_batch=lambda b:seen.extend(q.symbol for q in b.quotes))
        await source.poll([],priority_symbols=['T001'])
        assert seen[:3]==['T001','SPY','QQQ']
        assert manager.state['last_cycle_calls']<=8
        assert not control['data_ready'] and repo.all('orders')==[]
    finally:repo.close()


@pytest.mark.parametrize('cached',[False,True])
async def test_source_delivers_caught_up_minutes_to_actual_engine(tmp_path,cached):
    from watchlist_fixtures import context, NOW as stamp
    from test_watchlist_source import Market
    from sessionquant.market.source import TossSessionSource
    from sessionquant.market.bars import indicators
    from sessionquant.session_engine import SessionEngine
    from sessionquant.session_runtime import batch_events
    stamp += timedelta(seconds=2)
    repo,universe,_,_,manager=context(tmp_path,size=1)
    try:
        await manager.refresh(stamp)
        store=HistoryStore(repo)
        prepare(store,'T499',stamp)
        if cached:
            store.ingest(minute_rows('T499',session_bounds(stamp)[0],count=90,known=stamp-timedelta(seconds=1)))
        manager.history_selection=DailyHistorySelection(repo,startup_pending=True)
        control=begin_session(repo,mode='paper',now=stamp)
        worker=StartupWarmup(repo,store,manager.history_selection,control,clock=lambda:stamp)
        engine=SessionEngine(repo,universe,synthetic=True,control_state=control,watchlist_policy=manager,
            strict_market_data=True,paper_data_policy='observed_paper_v1')
        engine.market.history_provider=lambda symbol,now:store.load(symbol,now)
        provider=Provider(SimpleNamespace(now=stamp))
        class Client(Market):
            async def candles(self,symbol,**kwargs):
                if cached and symbol=='T499' and kwargs.get('before'):
                    pytest.fail('complete cached current session must not be downloaded again')
                return await provider.fetch(symbol,kwargs.get('before') or stamp.replace(second=0))
        def receive(batch):
            from sessionquant.session_engine import event_time
            # Match the real runtime: old receipts alone would be silently dropped.
            events=[row for row in batch_events(batch)
                    if engine.last_time is None or event_time(row)>=engine.last_time]
            engine.apply_batch(events,stamp)
        source=TossSessionSource(Client(lambda:stamp),watchlist=manager,clock=lambda:stamp,
            history_store=store,warmup=worker,
            on_priority_batch=receive)
        for _ in range(20):
            batch=await source.poll([])
            engine.apply_batch(batch_events(batch),stamp)
            if control['data_ready']:break
        assert control['data_ready'] and control['status']=='WAITING'
        assert manager.light_symbols(stamp)==['T499']
        data=indicators(engine.market.bars['T499'],stamp)
        assert len(data.five)==18 and len(data.fifteen)==6
        assert data.vwap is not None and data.atr14 is not None and data.ema9 is not None
        if cached:
            assert min(bar.received_at for bar in engine.market.bars['T499'])==stamp-timedelta(seconds=1)
        assert repo.all('orders')==[]
    finally:repo.close()


def test_dashboard_shows_preparation_and_disables_start(tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    now=datetime.now(timezone.utc)
    repo,_,_,control,_,worker,_=setup(tmp_path,now=now)
    heartbeat(repo,control,now=now)
    worker.report.update(status='COLLECTING',required_tasks=50,completed_tasks=12)
    worker.publish();repo.close()
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite');monkeypatch.setenv('SQ_ENVIRONMENT','PAPER_LOCAL')
    monkeypatch.setattr('sys.argv',['session_app.py','--db',str(tmp_path/'warmup.db')])
    app=AppTest.from_file(str(Path('sessionquant/dashboard/session_app.py').resolve()),default_timeout=30).run()
    assert not app.exception
    assert app.button(key='trading-start').disabled
    assert any('12/50' in item.value for item in app.info)

