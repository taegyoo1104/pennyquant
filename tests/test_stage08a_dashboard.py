"""08A 화면의 범위·최신성·제어 격리를 실제 저장소와 렌더러로 검사한다."""
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from streamlit.testing.v1 import AppTest

from sessionquant.database.repository import Repository
from sessionquant.dashboard.session_app import DashboardData,build_view
from sessionquant.dashboard.overview import PAGES,main_news,recent_trades,compact_cost
from sessionquant.dashboard.reference_panel import reference_rows
from sessionquant.models import NewsEvent,jsonable
from sessionquant.trading_controls import begin_session,request_command

NOW=datetime(2026,11,13,16,tzinfo=timezone.utc)
APP=str(Path(__file__).parents[1]/'sessionquant/dashboard/session_app.py')


def data_for(held=True,focused=True):
    return DashboardData(state={'broker':{'positions':{'AAPL':{'qty':'1','avg_price':'100'}} if held else {}}},
        watchlist_state={'status':'VALID','updated_at':NOW.isoformat(),'items':[
            {'ticker':'MSFT','focused':focused,'focus_valid_until':(NOW+timedelta(minutes=1)).isoformat()}]})


def article(identity,symbol,**changes):
    return jsonable(NewsEvent(identity,(symbol,),'Release',NOW-timedelta(minutes=1),NOW,
        'Issuer IR','https://issuer.test/'+identity,'Full verified release.',True,False))|changes


@pytest.mark.parametrize('held,focused,expected',[(True,True,{'rss:a','rss:m'}),(True,False,{'rss:a'}),(False,True,{'rss:m'}),(False,False,set())])
def test_main_news_uses_only_current_union_and_preserves_other_records(held,focused,expected):
    data=data_for(held,focused)
    data.actual_account={'items':[{'symbol':'AMZN','quantity':'1'}]}
    data.tables['news_events']=[article('rss:a','AAPL'),article('rss:m','MSFT'),article('rss:x','AMZN')]
    assert {r['id'] for r in main_news(data,build_view(data,NOW),NOW)}==expected
    assert len(data.tables['news_events'])==3


@pytest.mark.parametrize('changes',[{'published_at':None},{'published_at':(NOW-timedelta(hours=25)).isoformat()},
    {'published_at':(NOW+timedelta(seconds=1)).isoformat()},{'verified':False}])
def test_main_news_excludes_unverified_or_old_publication(changes):
    data=data_for();data.tables['news_events']=[article('rss:a','AAPL',**changes)]
    assert not main_news(data,build_view(data,NOW),NOW)


def test_stale_focus_does_not_revive_and_multi_analysis_deduplicates():
    data=data_for();data.watchlist_state['updated_at']=(NOW-timedelta(hours=1)).isoformat()
    event=article('rss:a','AAPL',symbols=['AAPL','MSFT','AMZN'])
    data.tables['news_analysis']=[{'event':event,'ticker':t,'source_kind':'rss','primary_verified':True} for t in ['AAPL','MSFT']]
    result=main_news(data,build_view(data,NOW),NOW)
    assert len(result)==1 and result[0]['labels']=='보유 AAPL'


def test_main_has_bounded_recent_trades_and_no_buy_realized_pnl():
    rows=[{'timestamp':(NOW-timedelta(seconds=i)).isoformat(),'symbol':'AAPL','side':'buy','realized_pnl':'999','fee':None} for i in range(8)]
    result=recent_trades(rows,NOW)
    assert len(result)==5 and all(r['매도 순실현손익']=='—' and r['수수료']=='—' for r in result)


def test_cost_card_uses_controls_only_and_distinguishes_limit_from_funding():
    class ControlsOnly:
        def get_control(self,key,default):
            return {'session_llm_budget_v1':{'2026-11':{'spent':'2','reservations':{'a':{'amount':'1'}}}},
                'session_llm_limits_v1':{'monthly_hard_budget_usd':'3'},
                'openai_funding_note':{'total_usd':'10'}}.get(key,default)
    assert compact_cost(ControlsOnly(),NOW)=={'month':Decimal('2'),'monthly_hard_budget_usd':Decimal('3'),
                                             'deposited':Decimal('10')}


@pytest.mark.parametrize('quality,latency',[('GOOD','DELAYED'),('UNKNOWN','UNKNOWN'),('GOOD','REALTIME')])
def test_reference_badge_never_upgrades_latency_and_stale_numbers_disappear(quality,latency):
    item={'quality_status':quality,'latency_class':latency,'current_price':'99','valid_until':(NOW+timedelta(seconds=1)).isoformat()}
    row=reference_rows({'items':{'IEF':item}},NOW)[1]
    assert row['지연 구분']==latency
    stale=reference_rows({'items':{'IEF':item}},NOW+timedelta(seconds=2))[1]
    assert stale['관측 가격']=='—'  # No source observation/timestamp to display.


@pytest.mark.parametrize('environment',['LIVE_LOCAL','DEMO','LIVE_READONLY'])
def test_wrong_environment_cannot_queue(tmp_path,environment):
    repo=Repository(tmp_path/'commands.db',backend='sqlite');state=begin_session(repo,mode='paper',now=NOW)
    with pytest.raises(ValueError):request_command(repo,'start',run_id=state['run_id'],environment=environment,now=NOW)
    assert not repo.all('order_intents');repo.close()


def test_two_browser_connections_queue_one_request_and_no_orders(tmp_path):
    path=tmp_path/'commands.db';repo=Repository(path,backend='sqlite');state=begin_session(repo,mode='paper',now=NOW)
    def submit(_):
        other=Repository(path,backend='sqlite')
        try:return request_command(other,'start',run_id=state['run_id'],environment='PAPER_LOCAL',now=NOW)['id']
        finally:other.close()
    with ThreadPoolExecutor(2) as pool:assert len(set(pool.map(submit,range(2))))==1
    assert len(repo.all('order_intents'))==1 and repo.all('orders')==[];repo.close()


def test_stale_heartbeat_and_unknown_managed_quantity_block_commands(tmp_path):
    repo=Repository(tmp_path/'commands.db',backend='sqlite');state=begin_session(repo,mode='paper',now=NOW)
    for action,at in [('start',NOW+timedelta(seconds=61)),('sell_symbol',NOW)]:
        with pytest.raises(ValueError):request_command(repo,action,run_id=state['run_id'],environment='PAPER_LOCAL',symbol='AAPL' if action=='sell_symbol' else None,now=at)
    assert not repo.all('order_intents');repo.close()


def test_all_six_pages_navigation_and_refresh_are_read_only(tmp_path,monkeypatch):
    path=tmp_path/'ui.db';repo=Repository(path,backend='sqlite');begin_session(repo,mode='paper')
    repo.save_state({'broker':{'cash':'100','reserved_cash':'0','equity':'100','positions':{}}});repo.close()
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite');monkeypatch.setattr('sys.argv',['session_app.py','--db',str(path)])
    app=AppTest.from_file(APP,default_timeout=30).run()
    assert [m.label for m in app.metric]==['시세','종목','모의 총자산','매수 가능 금액','일간 수익률','총 수익률','뉴스','AI 분석']
    assert app.sidebar.radio(key='dashboard_page').options==list(PAGES)
    assert app.button(key='trading-start').disabled
    app.checkbox[0].check().run()
    for page in PAGES:
        app.radio(key='dashboard_page').set_value(page).run()
        assert not app.exception
    app.radio(key='dashboard_page').set_value('대시보드').run()
    assert app.checkbox[0].value
    app.button(key='go-매매 전체 보기').click().run()
    assert app.radio(key='dashboard_page').value=='매매 내역'
    repo=Repository(path,backend='sqlite');assert not repo.all('order_intents') and not repo.all('orders');repo.close()


def test_market_fragment_refreshes_stored_values_and_toggle_disables_timer(tmp_path,monkeypatch):
    import streamlit as st
    path = tmp_path / 'refresh.db'
    writer = Repository(path, backend='sqlite')
    begin_session(writer, mode='paper')
    writer.set_control('watchlist_dashboard_v1', {'status': 'VALID', 'operating_count': 20,
                                               'updated_at': datetime.now(timezone.utc).isoformat()})
    monkeypatch.setenv('SQ_DB_BACKEND', 'sqlite')
    monkeypatch.setattr('sys.argv', ['session_app.py', '--db', str(path)])
    intervals = []
    original_fragment = st.fragment
    def fragment(*args, **kwargs):
        intervals.append(kwargs.get('run_every'))
        return original_fragment(*args, **kwargs)
    monkeypatch.setattr(st, 'fragment', fragment)
    try:
        app = AppTest.from_file(APP, default_timeout=30).run()
        assert not app.exception and intervals[-3:] == [5, 2, 60]
        app.radio(key='dashboard_page').set_value('시장·종목').run()
        assert not app.exception and intervals[-1] == 10
        app.radio(key='market_detail').set_value('감시·집중 종목').run()
        assert any(m.label == '유효 운영 종목' and m.value == '20 / 50' for m in app.metric)
        writer.set_control('watchlist_dashboard_v1', {'status': 'VALID', 'operating_count': 19,
                                                   'updated_at': datetime.now(timezone.utc).isoformat()})
        app.run()
        assert any(m.label == '유효 운영 종목' and m.value == '19 / 50' for m in app.metric)
        app.radio(key='dashboard_page').set_value('뉴스·LLM').run()
        assert intervals[-1] == 30
        app.toggle(key='dashboard_auto_refresh').set_value(False).run()
        assert not app.exception and intervals[-1] is None
        app.radio(key='dashboard_page').set_value('대시보드').run()
        assert not app.exception and intervals[-3:] == [None, None, None]
        assert not writer.all('orders') and not writer.all('order_intents')
    finally:
        writer.close()


def test_malformed_focus_expiry_never_crashes_or_labels_current_news():
    data=data_for();data.watchlist_state['items'][0]['focus_valid_until']='unknown'
    data.tables['news_events']=[article('rss:a','AAPL'),article('rss:m','MSFT')]
    rows=main_news(data,build_view(data,NOW),NOW)
    assert len(rows)==1 and rows[0]['labels']=='보유 AAPL'


def test_missing_average_purchase_price_stays_unknown_with_valid_quote():
    data=data_for();data.state['broker']['positions']['AAPL'].pop('avg_price')
    data.tables['quote_snapshots']=[{'symbol':'AAPL','bid':'100','timestamp':NOW.isoformat()}]
    row=build_view(data,NOW)['positions'][0]
    assert row['평균 매수가']=='—' and row['평가 손익']=='—' and row['평가액']=='$100.00'
