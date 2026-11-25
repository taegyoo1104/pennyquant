"""Operational recording bounds must not change financial or recovery results."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from sessionquant.database.repository import Repository
from sessionquant.research.storage import BUDGET, StoragePolicy, archive_selection, due, store
from sessionquant.research import journal

NOW = datetime(2026,11,24,16,tzinfo=timezone.utc)
POLICY = StoragePolicy(mode='bounded_diagnostics')


@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path/'bounds.db',backend='sqlite')
    yield r
    r.close()


def test_sampling_quota_restart_and_new_york_day(tmp_path):
    path=tmp_path/'restart.db'
    r=Repository(path,backend='sqlite')
    policy=StoragePolicy(mode='bounded_diagnostics',daily_records=2)
    row=lambda i:{'id':str(i),'kind':'diagnostic','data':'x'*200}
    assert store(r,policy,'research_events',row(1),'frame',NOW)
    assert not store(r,policy,'research_events',row(2),'frame',NOW+timedelta(seconds=59))
    assert store(r,policy,'research_events',row(2),'frame',NOW+timedelta(seconds=60))
    r.close()
    r=Repository(path,backend='sqlite')
    try:
        assert not due(r,policy,'frame',NOW+timedelta(hours=7),urgent=True)
        assert not store(r,policy,'research_events',row(3),'frame',NOW+timedelta(hours=7),urgent=True)
        # UTC midnight still belongs to the previous New York day.
        assert not due(r,policy,'frame',datetime(2026,11,25,0,tzinfo=timezone.utc))
        assert store(r,policy,'research_events',row(3),'frame',datetime(2026,11,25,5,tzinfo=timezone.utc))
        assert r.get_control(BUDGET)['records']==1
        assert len(r.all('research_events'))==3
    finally:r.close()


def test_bytes_record_size_and_transaction_rollback(repo,monkeypatch):
    policy=StoragePolicy(mode='bounded_diagnostics',daily_bytes=1024,max_record_bytes=800)
    large={'id':'oversize','data':'x'*900}
    assert not store(repo,policy,'research_events',large,'frame',NOW)
    assert repo.get_control(BUDGET)['records']==0
    row={'id':'one','data':'x'*600}
    assert store(repo,policy,'research_events',row,'frame',NOW+timedelta(minutes=1))
    assert not store(repo,policy,'research_events',{**row,'id':'two'},'frame',NOW+timedelta(minutes=2))
    assert repo.get_control(BUDGET)['bytes']<=1024
    before=repo.get_control(BUDGET)
    original=repo.set_control
    def fail(name,value):
        if name==BUDGET:raise RuntimeError('disk failure')
        return original(name,value)
    monkeypatch.setattr(repo,'set_control',fail)
    with pytest.raises(RuntimeError):
        store(repo,policy,'research_events',{'id':'rolled-back'},'frame',NOW+timedelta(days=1))
    assert repo.get_record('research_events','rolled-back') is None
    assert repo.get_control(BUDGET)==before


@pytest.mark.parametrize('bad',[{},[],False,0,['bad'],{'version':1,'day':'not-date'},
    {'version':1,'day':'2026-11-25','bytes':1,'records':1,'last_capture':{}}])
def test_invalid_or_future_budget_stops_only_optional_copies(repo,bad):
    repo.set_control(BUDGET,bad)
    assert not due(repo,POLICY,'frame',NOW)
    assert not store(repo,POLICY,'research_events',{'id':'skip'},'frame',NOW)
    archive_selection(SimpleNamespace(repo=repo,auxiliary_storage=POLICY),
                      {'id':'order-evidence','approved_count':1,'rows':[]},NOW)
    assert repo.get_record('pipeline_status','order-evidence')


def test_quota_exhaustion_preserves_real_selection_order_links_and_restore(tmp_path):
    from test_stage08_selection import market_fixture,batch_for,NOW as selection_now
    from sessionquant.session_engine import SessionEngine
    from sessionquant.portfolio.selection import VIEW
    market=market_fixture()
    repo=Repository(tmp_path/'orders.db',backend='sqlite')
    try:
        policy=StoragePolicy(mode='bounded_diagnostics',daily_records=1)
        assert store(repo,policy,'research_events',{'id':'quota'},'frame',selection_now)
        engine=SessionEngine(repo,market.universe,synthetic=True,candidate_settings={},auxiliary_storage=asdict(policy))
        engine.apply_batch(batch_for(market),selection_now)
        frame=repo.get_control(VIEW)
        intents=repo.all('order_intents')
        assert len(intents)==2
        assert all(repo.get_record('pipeline_status',x['selection_id'])==frame for x in intents)
        restored=SessionEngine(repo,market.universe,synthetic=True,restore=True,candidate_settings={},auxiliary_storage=asdict(policy))
        assert restored.broker.to_state()==engine.broker.to_state()
        assert restored.broker.reconcile()['ok']
    finally:repo.close()


def test_primary_news_kept_without_auxiliary_duplicate(repo):
    repo.set_control(journal.CONTROL,{'active':True,'run_id':'r','auxiliary_storage':asdict(POLICY)})
    repo.record('news_events',{'id':'news','symbol':'AAPL','title':'Original article'})
    repo.set_control('market_reference_view_v1',{'items':{'SPY':{'price':'600'}}})
    assert repo.all('news_events')[0]['title']=='Original article'
    assert repo.get_control('market_reference_view_v1')['items']['SPY']['price']=='600'
    assert repo.all('research_events')==[]


def test_six_hundred_selection_ticks_sample_ten_and_keep_order_evidence(repo):
    engine=SimpleNamespace(repo=repo,auxiliary_storage=POLICY)
    full_bytes=0
    for second in range(600):
        frame={'id':str(second),'approved_count':0,'rows':[{'symbol':'AAPL','detail':'x'*4000}]}
        full_bytes+=len(json.dumps(frame).encode())
        archive_selection(engine,frame,NOW+timedelta(seconds=second))
    rows=repo.all('pipeline_status')
    assert len(rows)==10
    actual=sum(len(json.dumps(row).encode()) for row in rows)
    assert actual<full_bytes/50


def test_bounded_and_full_recording_have_identical_trades_and_bounded_replay_rejected(tmp_path,monkeypatch):
    from sessionquant.market.universe import UniverseStore
    from sessionquant.session_demo import synthetic_tape
    from sessionquant.session_engine import SessionEngine,event_time
    from sessionquant.research.bundle import export_recording
    from sessionquant.research.replay import replay_recording
    states=[]
    rows,opening=synthetic_tape()
    for mode in ('full','bounded_diagnostics'):
        r=Repository(tmp_path/(mode+'.db'),backend='sqlite')
        try:
            u=UniverseStore();u.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=opening-timedelta(days=1))
            engine=SessionEngine(r,u,synthetic=True,record_inputs=True,auxiliary_storage={'mode':mode})
            monkeypatch.setattr(journal,'clock',lambda:opening-timedelta(seconds=1))
            journal.start(r,engine,source='toss')
            for now in sorted({event_time(row) for row in rows}):
                monkeypatch.setattr(journal,'clock',lambda:now)
                engine.apply_batch([row for row in rows if event_time(row)==now],now)
            journal.stop(r)
            assert len(r.all('executions'))==2
            states.append(engine.broker.to_state())
            if mode=='bounded_diagnostics':
                export_recording(r,tmp_path/'bounded-bundle')
                with pytest.raises(ValueError,match='진단'):
                    replay_recording(tmp_path/'bounded-bundle',tmp_path/'replay.db')
                assert not (tmp_path/'replay.db').exists()
        finally:r.close()
    assert states[0]==states[1]
