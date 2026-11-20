"""운영형 홈이 과거 기록을 현재 정상으로 오인하지 않고 기존 제어를 지키는지 검사한다."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from sessionquant.dashboard.operations_overview import operation_cards, home_focus
from sessionquant.dashboard.session_app import DashboardData, build_view
from sessionquant.database.repository import Repository
from sessionquant.trading_controls import begin_session

NOW=datetime(2026,11,16,16,tzinfo=timezone.utc)
APP=str(Path(__file__).parents[1]/'sessionquant/dashboard/session_app.py')


def ready_data():
    data=DashboardData(engine_state={'mode':'paper','armed':True,'heartbeat':NOW.isoformat()},
        warmup={'status':'PARTIAL','history_ready_count':26,'history_excluded_count':24},
        watchlist_state={'status':'VALID','updated_at':NOW.isoformat()})
    data.tables['quote_snapshots']=[{'symbol':'AAPL','timestamp':NOW.isoformat(),'received_at':NOW.isoformat()}]
    return data


def test_fresh_receipts_and_ready_selection_have_distinct_scopes():
    data=ready_data();cards=operation_cards(data,build_view(data,NOW),NOW)
    assert cards[0]['value']=='최근 수신' and cards[0]['detail']=='0초 전 · 1종목'
    assert cards[1]['value']=='26개 준비' and cards[1]['detail']=='집중 —개 · 분봉 부족 24개 제외'
    assert cards[2]['value']=='기록 없음' and cards[3]['value']=='기록 없음'


def test_home_focus_count_excludes_expired_saved_focus():
    data=ready_data()
    data.watchlist_state.update(focus_count=3,items=[
        {'focused':True,'focus_valid_until':(NOW+timedelta(seconds=30)).isoformat()},
        {'focused':True,'focus_valid_until':(NOW-timedelta(seconds=1)).isoformat()},
        {'focused':True,'focus_valid_until':'invalid'}])
    assert operation_cards(data,build_view(data,NOW),NOW)[1]['detail'].startswith('집중 1개')


def test_home_does_not_claim_monitoring_when_engine_heartbeat_expires_first():
    data = ready_data()
    data.watchlist_state['items'] = [{'ticker': 'AAPL', 'focused': True,
        'focus_monitor_until': (NOW+timedelta(minutes=20)).isoformat()}]
    assert home_focus(data, build_view(data, NOW), NOW)[0]['monitored']
    data.engine_state['heartbeat'] = (NOW-timedelta(seconds=61)).isoformat()
    cards = home_focus(data, build_view(data, NOW), NOW)
    assert not cards[0]['monitored'] and cards[0]['title'] == '목록 갱신 대기'
    assert data.watchlist_state['status'] == 'VALID'  # Presentation never rewrites saved facts.


@pytest.mark.parametrize('field',['heartbeat','timestamp','received_at'])
@pytest.mark.parametrize('offset',[-61,1])
def test_stale_or_future_timestamps_do_not_claim_current_data(field,offset):
    data=ready_data();stamp=(NOW+timedelta(seconds=offset)).isoformat()
    if field=='heartbeat':data.engine_state[field]=stamp
    else:data.tables['quote_snapshots'][0][field]=stamp
    cards=operation_cards(data,build_view(data,NOW),NOW)
    assert cards[0]['value']!='최근 수신'
    if field=='heartbeat':assert cards[1]['value']=='저장 기록'


def test_waiting_watchlist_does_not_display_zero_of_zero_or_completed_inspection():
    data=ready_data();data.warmup={'status':'WAITING_WATCHLIST','required_tasks':0,'completed_tasks':0}
    card=operation_cards(data,build_view(data,NOW),NOW)[1]
    assert card['value']=='목록 대기' and '0' not in card['detail']
    data.warmup.update(status='COLLECTING',required_tasks=88,completed_tasks=40)
    card=operation_cards(data,build_view(data,NOW),NOW)[1]
    assert card['value']=='분봉 확인' and card['detail']=='40 / 88구간 확인'


def test_expired_watchlist_and_zero_ready_symbols_are_not_normal():
    data=ready_data();data.watchlist_state['status']='EXPIRED'
    card=operation_cards(data,build_view(data,NOW),NOW)[1]
    assert card['value']=='목록 확인 필요' and card['warning']
    data.watchlist_state['status']='VALID';data.warmup['history_ready_count']=0
    assert operation_cards(data,build_view(data,NOW),NOW)[1]['warning']


def test_successful_ai_responses_do_not_hide_failures_or_truncation():
    data=ready_data();data.news_flow={'counts':{'unique_articles':184,'llm_request_attempts':8,
        'validated_responses':2,'failed_or_rejected':6,'pending_or_unconfirmed':0},
        'scope':{'may_be_truncated':True},'llm_health':{}}
    cards=operation_cards(data,build_view(data,NOW),NOW)
    assert cards[2]['value']=='184건 수집' and '조회 범위' in cards[2]['detail']
    assert cards[3]['value']=='2건 완료' and cards[3]['warning'] and '6건' in cards[3]['detail']
    data.news_flow['llm_health']={'code':'NO_CREDIT'}
    assert '잔액' in operation_cards(data,build_view(data,NOW),NOW)[3]['detail']


def test_compact_confirmation_still_queues_only_explicit_start_and_stop(tmp_path,monkeypatch):
    path=tmp_path/'home.db';repo=Repository(path,backend='sqlite')
    state=begin_session(repo,mode='paper')
    repo.save_state({'broker':{'cash':'80','reserved_cash':'0','equity':'100',
        'positions':{'AAPL':{'qty':'0.1','avg_price':'200'}}}})
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite');monkeypatch.setenv('SQ_ENVIRONMENT','PAPER_LOCAL')
    monkeypatch.setattr('sys.argv',['session_app.py','--db',str(path)])
    app=AppTest.from_file(APP,default_timeout=30).run()
    assert not app.exception and app.button(key='trading-start').disabled
    assert not repo.all('order_intents')
    app.checkbox(key='confirm-start-'+state['run_id']).check().run()
    assert not repo.all('order_intents')
    app.button(key='trading-start').click().run()
    assert app.button(key='trading-start').disabled
    app.checkbox(key='confirm-stop-'+state['run_id']).check().run()
    app.button(key='trading-stop').click().run()
    assert [r['action'] for r in repo.all('order_intents')]==['start','stop_all']
    assert not repo.all('orders') and not repo.all('executions')
    app.radio(key='dashboard_page').set_value('매매 내역').run()
    assert not app.exception
    assert app.checkbox(key='confirm-sell-AAPL-'+state['run_id']) is not None
    repo.close()
