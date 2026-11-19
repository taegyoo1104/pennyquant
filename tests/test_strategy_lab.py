from dataclasses import replace
from datetime import timedelta
import json

import pytest

from sessionquant.market.bars import indicators
from sessionquant.market.simple_pullback import inspect_pattern
from sessionquant.market.strategies import technical_signal
from sessionquant.models import Bar
from sessionquant.session_demo import synthetic_tape
from sessionquant.events import parse_event
from sessionquant.session_engine import event_time


def data():
    rows, opened=synthetic_tape()
    bars=[parse_event(r).data for r in rows if r['kind']=='bar' and r['data']['symbol']=='AAPL']
    now=opened+timedelta(minutes=75,seconds=1)
    return indicators(bars,now),now


def test_simple_pattern_and_optional_volume():
    original,now=data()
    changed=replace(original,volume_ratio=None)
    pattern=inspect_pattern(changed)
    assert pattern['status']=='SIGNAL' and pattern['correction_bars']==2
    signal=technical_signal('AAPL','pullback_simple',changed,'BULLISH',now)
    assert signal and signal.allow_add is False and signal.setup_id
    assert technical_signal('AAPL','pullback',changed,'BULLISH',now) is None


@pytest.mark.parametrize('length', [1,2,3,4,5])
def test_one_to_four_declining_bars_are_allowed_but_not_five(length):
    original,now=data()
    five=list(original.five)
    anchor=len(five)-2-length
    for i in range(anchor+1,len(five)-1):
        close=102-(i-anchor)*.1
        five[i]=replace(five[i],open=close+.01,close=close,high=close+.04,low=close-.04)
    five[anchor]=replace(five[anchor],open=102,close=102,high=102.04,low=101.96)
    five[-1]=replace(five[-1],open=102,close=102.1,high=102.15,low=101.95)
    modified=replace(original,five=tuple(five),atr14=1)
    p=inspect_pattern(modified)
    assert p['correction_bars']==length
    assert p['checks'][1]['passed']==(length<=4)


def test_simple_rejects_stale_bars_and_missing_trend():
    original,now=data()
    assert technical_signal('AAPL','pullback_simple',original,'BULLISH',now+timedelta(minutes=6)) is None
    flat=replace(original,fifteen=(original.fifteen[-2],replace(original.fifteen[-1],close=original.fifteen[-2].close)))
    assert not inspect_pattern(flat)['checks'][0]['passed']
    assert technical_signal('AAPL','pullback_simple',flat,'BULLISH',now) is None


def test_lab_profile_is_separate_paper_configuration():
    from sessionquant.session_config import load_session_config
    from pathlib import Path
    c=load_session_config(Path('config/sessionquant.strategy-lab.example.yaml'))
    assert c.strategy=='pullback_simple' and not c.market_filter_enabled
    assert c.environment=='PAPER_LOCAL' and c.actual_account_monitor_enabled is False
    assert c.db_path.name=='sessionquant-strategy-lab-v1.db'
    assert (c.watchlist_size,c.focus_size,c.focus_selection)==(50,10,'turnover')
    assert c.strategy_explanation['enabled'] is True


async def test_turnover_focus_uses_fifty_candidates_without_spy_or_history(tmp_path):
    from watchlist_fixtures import context,NOW
    from sessionquant.watchlist.selection import WatchlistManager,VIEW
    from sessionquant.dashboard.focus_summary import focus_rows
    repo,universe,config,provider,_=context(tmp_path,count=60,size=50)
    config=replace(config,strategy='pullback_simple',focus_size=10,focus_selection='turnover')
    manager=WatchlistManager(repo,universe,config,provider=provider,clock=lambda:NOW,allow_synthetic=True)
    try:
        await manager.refresh(NOW)
        symbols=manager.light_symbols(NOW)
        assert len(symbols)==50
        manager.observe_prices([{'symbol':s,'lastPrice':'101','timestamp':NOW.isoformat(),'currency':'USD'} for s in symbols],NOW)
        manager.observe_rankings({'rankedAt':NOW.isoformat(),'rankings':[{'symbol':s,'currency':'USD',
            'tradingAmount':str((1000-int(s[1:]))*1000000),'tradingVolume':'100000'} for s in symbols]},NOW)
        assert not manager.state['prices'].get('SPY')
        focus=manager.focus(NOW)
        assert focus==sorted(symbols)[:10]
        manager.publish(NOW)
        state=repo.get_control(VIEW)
        assert state['operating_count']==50 and state['focus_count']==10
        display=focus_rows(state,NOW)
        assert len(display)==10 and all(r['최근 관측가(USD)']!='—' for r in display)
        assert all(r['상태']=='확인' for r in display)
        assert all('현재 집중 여부 미확인' in r['상태'] for r in focus_rows(state,NOW+timedelta(minutes=3)))
    finally:repo.close()


async def test_simple_source_keeps_held_priority_and_stops_history_at_session_open(tmp_path):
    from types import SimpleNamespace
    from watchlist_fixtures import context,NOW
    from test_watchlist_source import Market
    from sessionquant.market.source import TossSessionSource
    from sessionquant.watchlist.selection import WatchlistManager
    from sessionquant.market.bars import session_bounds
    repo,universe,config,provider,_=context(tmp_path,size=50)
    config=replace(config,strategy='pullback_simple',focus_size=10,focus_selection='turnover')
    manager=WatchlistManager(repo,universe,config,provider=provider,clock=lambda:NOW,allow_synthetic=True)
    try:
        await manager.refresh(NOW)
        opened=session_bounds(NOW)[0]
        manager.state['history_progress']={'T001':{'started':True,'cursor':(opened-timedelta(minutes=1)).isoformat(),'done':False}}
        class Client(Market):
            async def candles(self,symbol,**kwargs):
                assert 'before' not in kwargs  # Must not page into earlier sessions.
                rows,_=await super().candles(symbol,**kwargs)
                old=replace(rows[0],timestamp=opened-timedelta(minutes=1))
                return [old,*rows],None
        client=Client(lambda:NOW)
        source=TossSessionSource(client,clock=lambda:NOW,watchlist=manager,
            include_benchmarks=False,current_session_only=True,watchlist_candles_per_cycle=0)
        batch=await source.poll([],priority_symbols=['T001'])
        assert batch.quotes[0].symbol=='T001'
        assert all(q.symbol not in {'SPY','QQQ'} for q in batch.quotes)
        assert all(b.timestamp>opened for b in batch.bars)
        assert manager.state['history_progress']['T001']['status']=='SESSION_WINDOW_REACHED'
        assert manager.state['last_cycle_calls']<=24
    finally:repo.close()


def test_scenarios_execute_real_paper_path_and_report_fixture_truthfully(tmp_path):
    from sessionquant.strategy_lab import demonstrate
    result=demonstrate(tmp_path/'lab')
    assert result['passed'] and result['llm_api_calls']==0 and result['llm_mode']=='FIXTURE'
    report=json.loads((tmp_path/'lab/acceptance.json').read_text())
    for row in report['scenarios']:
        assert row['passed']
        if row['case'] in {'entry_exit','restart','llm_unavailable','llm_failure'}:
            assert row['fills']==['buy','sell']
        else:assert not row['fills']
    successful=report['scenarios'][0]['decisions']
    assert any(d.get('news_score_signal',{}).get('analysis_ids') for d in successful)
    assert any(d.get('order_intent_id') for d in successful)
    unavailable=report['scenarios'][-1]['decisions']
    assert all(d['indicators']['score_components']['news']==0 for d in unavailable)
    assert '실제 LLM 호출 0회' in (tmp_path/'lab/report.html').read_text()
    with pytest.raises(ValueError):demonstrate(tmp_path/'lab')


def test_duplicate_setup_identity_survives_later_evaluation():
    original,now=data()
    one=technical_signal('AAPL','pullback_simple',original,'BULLISH',now)
    two=technical_signal('AAPL','pullback_simple',original,'BULLISH',now+timedelta(seconds=1))
    assert one.id==two.id


@pytest.mark.parametrize('stale,missing',[(False,False),(True,False),(False,True)])
def test_observed_policy_uses_current_session_only_and_retains_data_guards(tmp_path, stale, missing):
    from sessionquant.database.repository import Repository
    from sessionquant.market.universe import UniverseStore
    from sessionquant.market.history import KIND, SOURCE
    from sessionquant.market.eligibility import toss_eligibility
    from sessionquant.models import jsonable
    from sessionquant.session_engine import SessionEngine
    tape,opened=synthetic_tape()
    now=opened+timedelta(minutes=75,seconds=1)
    universe=UniverseStore()
    universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=opened-timedelta(days=1))
    repo=Repository(tmp_path/'observed-simple.db',backend='sqlite')
    try:
        engine=SessionEngine(repo,universe,strategy='pullback_simple',synthetic=True,
            strict_market_data=True,paper_data_policy='observed_paper_v1',use_market_filter=False,
            candidate_settings={'enabled':True,'use_sector_score':False})
        rows=[]
        for r in tape:
            if r['kind']=='rvol' or event_time(r)>now:continue
            if r['kind']=='eligibility':
                r={'kind':'eligibility','data':jsonable(toss_eligibility('AAPL',
                    {'symbol':'AAPL','status':'ACTIVE','market':'NASDAQ','currency':'USD'},opened))}
            if r['kind']=='bar':
                r['data'].update(source=SOURCE,price_adjustment='raw')
                if missing and r['data']['symbol']=='AAPL' and event_time(r)==opened+timedelta(minutes=74):continue
            if stale and r['kind']=='quote':
                r['data']['timestamp']=(now-timedelta(minutes=2)).isoformat()
            rows.append(r)
        engine.apply_batch(sorted(rows,key=event_time),now)
        assert not repo.all(KIND)
        orders=repo.all('orders')
        if stale or missing:assert not orders
        else:
            assert len(orders)==1
            d=engine.market.evaluate('AAPL',now)
            assert d.eligible and d.signal and d.indicators['relative_volume'] is None
            assert d.indicators['rvol_required'] is False
    finally:repo.close()


async def test_real_news_parser_and_llm_transport_feed_simple_strategy(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from sessionquant.database.repository import Repository
    from sessionquant.macro.service import NewsPipeline
    from sessionquant.market.universe import UniverseStore
    from sessionquant.session_engine import SessionEngine
    from sessionquant.portfolio.selection import VIEW
    from test_realtime_paper import rss_service, NOW
    repo=Repository(tmp_path/'news-simple.db',backend='sqlite')
    analyzer,service,client,requests=await rss_service(repo,monkeypatch)
    pipeline=NewsPipeline(repo,analyzer=analyzer,company_service=service,clock=lambda:NOW)
    tape,opened=synthetic_tape()
    universe=UniverseStore();universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=opened-timedelta(days=1))
    try:
        await pipeline.refresh_company(['AAPL'],NOW)
        engine=SessionEngine(repo,universe,strategy='pullback_simple',synthetic=True,
            use_market_filter=False,risk_provider=pipeline,candidate_settings={'enabled':True,'use_sector_score':False})
        stamp=NOW+timedelta(seconds=1)
        engine.apply_batch([r for r in tape if r['kind']!='rvol' and event_time(r)<=stamp],stamp)
        selected=repo.get_control(VIEW)['rows'][0]
        assert selected['news_score_signal']['llm_call_ids']
        assert selected['indicators']['score_components']['news']>0
        assert selected['order_intent_id']
        assert len([r for r in requests if r.method=='POST'])==1
        assert repo.all('llm_calls') and repo.all('orders')
    finally:
        await pipeline.aclose();await client.aclose();repo.close()


def test_lab_dashboard_displays_intent_and_news_provenance(tmp_path, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    from sessionquant.strategy_lab import demonstrate
    demonstrate(tmp_path/'ui-lab')
    monkeypatch.setenv('SQ_DB',str(tmp_path/'ui-lab/entry_exit.db'))
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite')
    monkeypatch.setenv('PQ_DB_BACKEND','sqlite')
    app=AppTest.from_file(str(Path('sessionquant/dashboard/session_app.py').resolve()),default_timeout=30).run()
    assert not app.exception
    assert any('전략 실험' in element.value for element in app.info)
    assert any(e.label=='전략 → LLM → 주문 기록' for e in app.expander)


def test_quality_screen_does_not_show_optional_rvol_as_missing_requirement():
    from sessionquant.dashboard.market_quality_panel import coverage_display
    assert coverage_display({'items':[{'rvol_required':False}]},'strategy_rvol')=='참고 · 진입 필수 아님'
