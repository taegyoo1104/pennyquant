from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from sessionquant.database.repository import Repository
from sessionquant.market.bars import session_bounds
from sessionquant.market.history import HistoryStore, KIND, SOURCE, VIEW, history_plan, current_view
from sessionquant.market.history_commands import backfill
from sessionquant.market.quality import rvol_report
from sessionquant.models import Bar

NOW = datetime(2026,11,16,14,35,2,tzinfo=timezone.utc)


def minute_rows(symbol, opened, count=390, volume=100, known=NOW):
    return [Bar(symbol,opened+timedelta(minutes=i),known,100,101,99,100,volume,
                source=SOURCE,price_adjustment='raw') for i in range(1,count+1)]


def seeded(repo, symbol='AAPL'):
    store = HistoryStore(repo)
    for day,opened,_ in history_plan(NOW):
        store.save(symbol,day,minute_rows(symbol,opened,count=5))
    store.ingest(minute_rows(symbol,session_bounds(NOW)[0],count=5,volume=200))
    return store


def test_db_restart_same_elapsed_rvol_is_calculated_but_not_execution_verified(tmp_path):
    path = tmp_path/'history.db'
    repo = Repository(path,backend='sqlite')
    seeded(repo)
    repo.close()
    repo = Repository(path,backend='sqlite')
    try:
        store = HistoryStore(repo)
        report = store.report('AAPL',NOW)
        assert report['calculated_value'] == 2 and report['complete_sessions'] == 20
        assert report['value'] is None and report['status'] == 'UNKNOWN'
        assert report['reasons'] == ['split_volume_comparability_unknown']
        assert len(store.load('AAPL',NOW)) == 100  # current session is explicit, not a hidden engine input
        assert store.report('AAPL',NOW-timedelta(seconds=3))['calculated_value'] is None
        assert not repo.all('orders') and repo.load_state() is None
    finally:
        repo.close()


def test_first_receipt_is_preserved_and_corrections_block_without_overwrite(tmp_path):
    repo = Repository(tmp_path/'history.db',backend='sqlite')
    try:
        store = seeded(repo)
        day,opened,_ = history_plan(NOW)[0]
        first = store.get('AAPL',day)[1][0]
        store.save('AAPL',day,[replace(first,received_at=NOW+timedelta(seconds=10))])
        assert len(store.get('AAPL',day)[1]) == 5
        assert store.get('AAPL',day)[1][0].received_at == NOW
        store.save('AAPL',day,[replace(first,volume=999,received_at=NOW+timedelta(seconds=1))])
        assert store.report('AAPL',NOW)['calculated_value'] == 2
        assert store.report('AAPL',NOW+timedelta(seconds=1))['calculated_value'] is None
        assert len(store.get('AAPL',day)[1]) == 6
    finally:
        repo.close()


def test_early_close_is_kept_and_twenty_full_sessions_are_also_prepared():
    plan = history_plan(datetime(2026,11,30,14,35,tzinfo=timezone.utc))
    assert len(plan) == 21
    assert any(day=='2026-11-27' and (closed-opened).total_seconds()==210*60 for day,opened,closed in plan)
    assert sum((closed-opened).total_seconds()==390*60 for _,opened,closed in plan)==20


class Pages:
    def __init__(self, rows):
        self.rows,self.calls = rows,[]

    async def candles(self,symbol,*,count,before):
        self.calls.append((symbol,before))
        rows = sorted([b for b in self.rows if b.symbol==symbol and b.timestamp<=before],key=lambda b:b.timestamp,reverse=True)[:count]
        return rows, (rows[-1].timestamp-timedelta(minutes=1)).isoformat() if len(rows)==count else None


async def test_interrupted_backfill_resumes_cursor_then_skips_complete_days(tmp_path,monkeypatch):
    import sessionquant.market.history_commands as commands
    day,opened,closed = history_plan(NOW)[-1]
    monkeypatch.setattr(commands,'history_plan',lambda *a,**kw:[(day,opened,closed)])
    path = tmp_path/'history.db'
    repo = Repository(path,backend='sqlite')
    client = Pages(minute_rows('AAPL',opened))
    first = await backfill(repo,['AAPL'],client,now=NOW,max_requests=1)
    assert first['status']=='INTERRUPTED' and first['read_requests']==1
    assert HistoryStore(repo).get('AAPL',day)[0]['quality']['received_minutes']==200
    repo.close()
    repo = Repository(path,backend='sqlite')
    try:
        second = await backfill(repo,['AAPL'],client,now=NOW,max_requests=5)
        assert second['status']=='COMPLETE' and second['read_requests']==1
        assert client.calls[1][1] < client.calls[0][1]
        assert HistoryStore(repo).get('AAPL',day)[0]['quality']['received_minutes']==390
        third = await backfill(repo,['AAPL'],client,now=NOW,max_requests=5)
        assert third['status']=='COMPLETE' and third['read_requests']==0
        assert len(repo.all(KIND))==1 and repo.load_state() is None and not repo.all('orders')
    finally:
        repo.close()


async def test_backfill_rate_limit_stops_and_persists_safe_retry_time(tmp_path,monkeypatch):
    import sessionquant.market.history_commands as commands
    monkeypatch.setattr(commands,'history_plan',lambda *a,**kw:history_plan(NOW)[-1:])

    class Limited:
        async def candles(self,*a,**kw):
            response=httpx.Response(429,headers={'Retry-After':'120'},request=httpx.Request('GET','https://example.invalid?token=PRIVATE'))
            response.raise_for_status()

    repo = Repository(tmp_path/'history.db',backend='sqlite')
    try:
        report=await backfill(repo,['AAPL'],Limited(),now=NOW)
        assert report['error']=={'type':'HTTPStatusError','http_status':429}
        assert report['retry_after'] and 'PRIVATE' not in str(report)
    finally:
        repo.close()


def test_engine_reads_db_history_without_promoting_unknown_eligibility(tmp_path):
    from sessionquant.market.engine import MarketEngine
    from sessionquant.market.universe import UniverseStore
    repo=Repository(tmp_path/'history.db',backend='sqlite')
    try:
        store=seeded(repo)
        universe=UniverseStore()
        universe.refresh(['AAPL'],source='FIXTURE',observed_at=NOW-timedelta(days=1))
        engine=MarketEngine(universe,strict_data=True)
        engine.history_provider=store.load
        for bar in minute_rows('AAPL',session_bounds(NOW)[0],count=5,volume=200):engine.on_bar(bar)
        decision=engine.evaluate('AAPL',NOW)
        assert decision.indicators['data_quality']['rvol']['calculated_value']==2
        assert decision.indicators['relative_volume'] is None and not decision.eligible
        assert len(engine.bars['AAPL'])==5
    finally:
        repo.close()


def test_per_symbol_expiry_and_new_minute_do_not_inherit_heartbeat(tmp_path):
    repo=Repository(tmp_path/'history.db',backend='sqlite')
    try:
        store=seeded(repo)
        store.publish(['AAPL','MSFT'],NOW,updated=['AAPL'])
        view=repo.get_control(VIEW)
        assert current_view(view,NOW)['items'][0]['calculated_value']==2
        assert current_view(view,NOW+timedelta(seconds=31))['items'][0]['calculated_value'] is None
        assert current_view(view,NOW+timedelta(seconds=58))['items'][0]['calculated_value'] is None
        assert current_view(view,NOW)['items'][1]['calculated_value'] is None
    finally:
        repo.close()


def test_history_seed_records_once_per_run_and_replays_without_lookahead(tmp_path):
    from sessionquant.research.journal import CONTROL
    repo=Repository(tmp_path/'history.db',backend='sqlite')
    other=Repository(tmp_path/'replay.db',backend='sqlite')
    try:
        store=seeded(repo)
        repo.set_control(CONTROL,{'active':True,'run_id':'fixture','source':'toss','synthetic':True})
        store.load('AAPL',NOW,record=True)
        store.load('AAPL',NOW,record=True)
        records=repo.all('research_events')
        assert len(records)==20 and all(r['kind']==KIND for r in records)
        for event in records:other.record(KIND,event['data'])
        restored=HistoryStore(other)
        assert restored.load('AAPL',NOW)==store.load('AAPL',NOW)
        assert not restored.load('AAPL',NOW-timedelta(seconds=1))
    finally:
        repo.close();other.close()


async def test_background_rotation_covers_watchlist_and_keeps_risk_quotes_first(tmp_path):
    from types import SimpleNamespace
    from sessionquant.market.source import TossSessionSource
    from watchlist_fixtures import context, observe, NOW as base
    from test_watchlist_source import Market
    repo,_,_,_,manager=context(tmp_path,size=50)
    stamp=SimpleNamespace(now=base+timedelta(seconds=60))

    class Candles(Market):
        async def candles(self,symbol,**kwargs):
            self.calls.append(('candles',symbol))
            return minute_rows(symbol,session_bounds(stamp.now)[0],count=1,known=stamp.now),None

    try:
        await manager.refresh(base);observe(manager)
        client=Candles(lambda:stamp.now)
        source=TossSessionSource(client,watchlist=manager,clock=lambda:stamp.now,
                                 history_store=HistoryStore(repo),watchlist_candles_per_cycle=4)
        visited=set()
        for _ in range(15):
            client.calls.clear()
            await source.poll([],priority_symbols=['T001'])
            assert [s for kind,s in client.calls if kind=='quote'][:3]==['T001','SPY','QQQ']
            assert manager.state['last_cycle_calls'] <= 24
            visited.update(s for kind,s in client.calls if kind=='candles')
            stamp.now+=timedelta(seconds=60)
        assert set(manager.light_symbols(stamp.now)) <= visited
        assert len(repo.get_control(VIEW)['items'])==50
    finally:
        repo.close()


def test_default_fifty_retains_explicit_hundred_option():
    from sessionquant.session_config import SessionConfig
    assert SessionConfig(company_rss_catalog=False).watchlist_size==50
    assert SessionConfig(watchlist_size=100,company_rss_catalog=False).watchlist_size==100


async def test_reducing_existing_watchlist_refreshes_before_weekly_deadline(tmp_path):
    from sessionquant.watchlist.selection import WatchlistManager
    from watchlist_fixtures import context,NOW as stamp
    repo,universe,config,provider,manager=context(tmp_path)
    try:
        await manager.refresh(stamp)
        old=manager.store.at(stamp)
        reduced=WatchlistManager(repo,universe,replace(config,watchlist_size=50),provider=provider,allow_synthetic=True)
        reduced.publish(stamp)
        assert len(repo.get_control('watchlist_dashboard_v1')['items'])==50
        assert reduced.entry_blockers('T400',stamp)==['outside_market_cap_watchlist']
        await reduced.refresh(stamp+timedelta(seconds=1))
        current=reduced.store.at(stamp+timedelta(seconds=1))
        assert len(current['items'])==50 and current['id']!=old['id']
    finally:
        repo.close()


def test_record_export_and_replay_include_db_history_used_by_engine(tmp_path,monkeypatch):
    from sessionquant.market.universe import UniverseStore
    from sessionquant.models import Quote,jsonable
    from sessionquant.research import journal
    from sessionquant.research.bundle import export_recording
    from sessionquant.research.replay import replay_recording
    from sessionquant.session_engine import SessionEngine
    monkeypatch.setattr(journal,'clock',lambda:NOW)
    repo=Repository(tmp_path/'observed.db',backend='sqlite')
    try:
        store=seeded(repo)
        universe=UniverseStore()
        universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=NOW-timedelta(days=1))
        engine=SessionEngine(repo,universe,synthetic=True,strict_market_data=True,record_inputs=True)
        engine.market.history_provider=lambda symbol,stamp:store.load(symbol,stamp,record=True)
        journal.start(repo,engine,source='toss')
        repo.record('universe_history',universe.snapshots[0])
        rows=[{'kind':'bar','data':jsonable(b)} for b in minute_rows('AAPL',session_bounds(NOW)[0],count=5,volume=200)]
        rows.append({'kind':'quote','data':jsonable(Quote('AAPL',NOW,NOW,100,100.01,1000,1000))})
        engine.apply_batch(rows,NOW)
        original=engine._batch_decisions[0]['indicators']['data_quality']['rvol']
        assert original['calculated_value']==2
        journal.stop(repo)
        bundle=tmp_path/'bundle'
        export_recording(repo,bundle)
        replay_recording(bundle,tmp_path/'replay.db')
        replay=Repository(tmp_path/'replay.db',backend='sqlite')
        try:
            decision=replay.all('candidates')[-1]
            assert decision['indicators']['data_quality']['rvol']==original
            assert len(replay.all(KIND))==20 and replay.all('orders')==[]
        finally:
            replay.close()
    finally:
        repo.close()
