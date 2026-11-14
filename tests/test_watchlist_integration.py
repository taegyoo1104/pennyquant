"""감시목록의 진입 차단이 기존 보유 청산·UI·설정 호환을 깨지 않는지 검증한다."""
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import json

from streamlit.testing.v1 import AppTest

from sessionquant.database.repository import Repository
from sessionquant.market.universe import UniverseStore
from sessionquant.session_demo import synthetic_tape
from sessionquant.session_engine import SessionEngine,event_time
from sessionquant.session_config import load_session_config
from sessionquant.dashboard.watchlist_panel import current_status
from watchlist_fixtures import NOW,context,observe


def process_tape(engine,rows,*,protect_after_fill=False):
    blocked=SimpleNamespace(entry_blockers=lambda *args:['watchlist_unknown'])
    index=0
    while index<len(rows):
        now=event_time(rows[index]);end=index+1
        while end<len(rows) and event_time(rows[end])==now:end+=1
        if protect_after_fill and engine.broker.positions:engine.watchlist_policy=blocked
        engine.apply_batch(rows[index:end],now,cursor=end);index=end


def test_unknown_watchlist_blocks_valid_technical_strategy_without_enabling_live_orders(tmp_path):
    rows,opened=synthetic_tape()
    universe=UniverseStore()
    universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=opened-timedelta(days=1))
    repo=Repository(tmp_path/'blocked.db',backend='sqlite')
    try:
        engine=SessionEngine(repo,universe,synthetic=True,
            watchlist_policy=SimpleNamespace(entry_blockers=lambda *args:['watchlist_unknown']))
        process_tape(engine,rows)
        assert engine.broker.fills==[] and engine.broker.positions=={}
        assert any('watchlist_unknown' in row['blockers'] for row in repo.all('candidates'))
        assert repo.get_control('sessionquant_context')['live_orders_enabled'] is False
    finally:repo.close()


def test_unknown_watchlist_does_not_stop_same_day_exit_of_existing_position(tmp_path):
    rows,opened=synthetic_tape();universe=UniverseStore()
    universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=opened-timedelta(days=1))
    repo=Repository(tmp_path/'exits.db',backend='sqlite')
    try:
        engine=SessionEngine(repo,universe,synthetic=True)
        process_tape(engine,rows,protect_after_fill=True)
        assert [r.side for r in engine.broker.fills]==['buy','sell']
        assert engine.broker.positions=={}
        assert any(o.intent.reason=='session_exit' for o in engine.broker.orders.values())
    finally:repo.close()


def test_old_config_remains_loadable_with_safe_new_defaults(tmp_path):
    path=tmp_path/'old.yaml'
    path.write_text('symbols_per_cycle: 2\ncompanies: {}\n',encoding='utf-8')
    config=load_session_config(path)
    assert config.watchlist_size==100 and config.focus_size==20 and config.watchlist_enabled
    assert config.symbols_per_cycle==2
    assert config.market_cap_input==(tmp_path/'../data/market-cap-input.json').resolve()
    assert path.read_text()=='symbols_per_cycle: 2\ncompanies: {}\n'


async def test_dashboard_marks_past_focus_as_history_and_labels_fixture(tmp_path,monkeypatch):
    repo,_,_,_,manager=context(tmp_path)
    try:
        await manager.refresh(NOW);now=observe(manager);manager.publish(now)
        state=repo.get_control('watchlist_dashboard_v1')
        assert current_status(state,now)==('VALID',True)
        assert current_status(state,now+timedelta(minutes=5))==('VALID',False)
        assert current_status(state,now+timedelta(days=8))[0]=='EXPIRED'
    finally:repo.close()
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite')
    monkeypatch.setattr('sys.argv',['session_app.py','--db',str(tmp_path/'watch.db')])
    app=AppTest.from_file(str(Path(__file__).parents[1]/'sessionquant/dashboard/session_app.py'),default_timeout=30).run()
    assert not app.exception
    assert any('합성 감시목록' in w.value for w in app.warning)
    assert any(m.label=='현재 집중 분석' and m.value=='0 / 20' for m in app.metric)
    assert any('과거 분석' in c.value for c in app.caption)


async def test_export_contains_only_collected_history_and_does_not_write_orders(tmp_path,monkeypatch):
    from sessionquant.watchlist.commands import command
    repo,_,config,_,manager=context(tmp_path)
    await manager.refresh(NOW);now=observe(manager);manager.publish(now);repo.close()
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite')
    output=tmp_path/'export.json'
    result=await command(config,'watchlist-export',output=output)
    assert result['snapshots']==1 and result['live_orders_enabled'] is False
    history=json.loads(output.read_text())
    assert history['snapshots'][0]['known_at']==NOW.isoformat()
    assert any(row['items'] for row in history['focus_history'])
    repo=Repository(config.db_path,backend='sqlite')
    assert repo.all('orders')==[]
    repo.close()


async def test_unknown_company_allowlist_keeps_macro_sources_running(tmp_path):
    from sessionquant.macro.service import NewsPipeline
    repo,_,_,_,manager=context(tmp_path)
    calls=[]
    class Company:
        events,status=[],[]
        async def refresh(self,symbols,now):
            calls.append(('company',symbols));return []
        async def aclose(self):pass
    class Source:
        status=[]
        def __init__(self,name):self.name=name
        async def fetch(self,**kwargs):
            calls.append((self.name,True));return []
        async def aclose(self):pass
    analyzer=SimpleNamespace(ledger=SimpleNamespace(snapshot=lambda now:{}))
    news=NewsPipeline(repo,analyzer=analyzer,company_service=Company(),clock=lambda:NOW,
        official_client=Source('official'),treasury_client=Source('treasury'),gdelt_client=Source('gdelt'),fred_client=Source('fred'))
    try:
        contract=manager.news_targets(NOW)
        assert contract['analysis_symbols']==[] and contract['company_allowlist']==[]
        await news.refresh(contract['analysis_symbols'],now=NOW)
        assert {name for name,_ in calls}=={'company','official','treasury','gdelt','fred'}
    finally:
        await news.aclose();repo.close()
