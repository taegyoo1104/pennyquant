"""Home cards are display-only and distinguish observations from entry permission."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from sessionquant.dashboard.home_cards import focus_cards, focus_cards_html

NOW=datetime(2026,11,19,15,46,tzinfo=timezone.utc)


def snapshots(stage='REBOUND'):
    state={'status':'VALID','updated_at':NOW.isoformat(),'data_max_age_seconds':120,
        'items':[{'ticker':'AAPL','focused':True,'last_price':'191.25',
                  'last_price_at':(NOW-timedelta(seconds=8)).isoformat(),
                  'focus_monitor_until':(NOW+timedelta(minutes=20)).isoformat()}]}
    assessment={'rows':[{'symbol':'AAPL','evaluated_at':NOW.isoformat(),'signal_present':True,
        'progress':{'stage':stage,'five_at':NOW.replace(minute=45).isoformat(),
                    'fifteen_at':NOW.replace(minute=45).isoformat(),'entry_history_ready':True}}]}
    return state,assessment


def test_current_focus_shows_observed_price_and_three_steps_without_buy_permission():
    state,assessment=snapshots();before=deepcopy((state,assessment))
    card=focus_cards(state,assessment,now=NOW)[0]
    assert card['price']=='$191.25' and card['price_kind']=='관측가'
    assert card['price_fresh'] and card['stage_fresh'] and card['monitored']
    assert card['title']=='반등 확인'
    assert [s['state'] for s in card['steps']]==['done']*3
    assert '주문 조건' in card['explanation']
    assert (state,assessment)==before


@pytest.mark.parametrize('fault',['expired_focus','expired_list','pending_focus','future_assessment','old_bar','naive_bar','missing_bar'])
def test_stale_or_invalid_context_never_shows_passed_strategy_steps(fault):
    state,assessment=snapshots();row=state['items'][0];saved=assessment['rows'][0]
    if fault=='expired_focus':row['focus_monitor_until']=NOW.isoformat()
    elif fault=='expired_list':state['updated_at']=(NOW-timedelta(minutes=4)).isoformat()
    elif fault=='pending_focus':row['focus_refresh_pending']=True
    elif fault=='future_assessment':saved['evaluated_at']=(NOW+timedelta(seconds=1)).isoformat()
    elif fault=='old_bar':saved['progress']['five_at']=NOW.replace(minute=40).isoformat()
    elif fault=='naive_bar':saved['progress']['five_at']='2026-11-19T15:45:00'
    elif fault=='missing_bar':saved['progress'].pop('fifteen_at')
    card=focus_cards(state,assessment,now=NOW)[0]
    assert not card['stage_fresh'] and card['stage'] is None
    assert all(s['state']=='pending' for s in card['steps'])
    assert card['title']!='반등 확인'


@pytest.mark.parametrize('price',[None,'NaN','Infinity','-1','0',True,'nonsense'])
def test_invalid_prices_are_unknown(price):
    state,assessment=snapshots();state['items'][0]['last_price']=price
    card=focus_cards(state,assessment,now=NOW)[0]
    assert card['price']=='—' and not card['price_fresh']


@pytest.mark.parametrize('stamp',[None,'invalid','2026-11-19T15:46:00','2026-11-19T15:46:01+00:00'])
def test_invalid_or_future_price_timestamps_do_not_show_current_price(stamp):
    state,assessment=snapshots();state['items'][0]['last_price_at']=stamp
    card=focus_cards(state,assessment,now=NOW)[0]
    assert card['price']=='—' and not card['price_fresh']


def test_expired_price_stays_visible_as_previous_not_current():
    state,assessment=snapshots()
    state['items'][0]['last_price_at']=(NOW-timedelta(minutes=5)).isoformat()
    card=focus_cards(state,assessment,now=NOW)[0]
    assert card['price']=='$191.25' and card['price_status']=='이전 가격'
    assert not card['price_fresh']


def test_newer_quote_is_explicitly_midpoint_not_last_trade():
    state,assessment=snapshots()
    quote={'symbol':'AAPL','bid':'192','ask':'192.04','timestamp':NOW.isoformat(),'received_at':NOW.isoformat()}
    card=focus_cards(state,assessment,quotes={'AAPL':quote},now=NOW)[0]
    assert card['price']=='$192.02' and card['price_kind']=='호가 중간값'
    quote['received_at']=(NOW+timedelta(seconds=1)).isoformat()
    assert focus_cards(state,assessment,quotes={'AAPL':quote},now=NOW)[0]['price']=='$191.25'
    quote['received_at']=NOW.isoformat();quote['symbol']='MSFT'
    assert focus_cards(state,assessment,quotes={'AAPL':quote},now=NOW)[0]['price']=='$191.25'


def test_stage_does_not_lock_in_and_order_approval_is_not_fill():
    state,assessment=snapshots()
    assessment['rows'][0]['order_status']='APPROVED'
    card=focus_cards(state,assessment,now=NOW)[0]
    assert '승인' in card['explanation'] and '별도로' in card['explanation']
    assessment['rows'][0]['progress']['stage']='TREND_MISSING'
    assessment['rows'][0]['order_status']='BLOCKED'
    card=focus_cards(state,assessment,now=NOW)[0]
    assert card['steps'][0]['state']=='wait' and card['steps'][1]['state']=='pending'
    assert '보류' in card['explanation']
    assessment['rows'][0]['progress']['entry_history_ready']=False
    assert '준비 중' in focus_cards(state,assessment,now=NOW)[0]['explanation']


def test_html_escapes_dynamic_values_and_has_only_allowlisted_classes():
    malicious='<img src=x onerror="alert(1)">'
    card={'symbol':malicious,'price':malicious,'price_kind':malicious,'price_status':malicious,
          'time_label':malicious,'title':malicious,'explanation':malicious,
          'steps':[{'label':malicious,'state':'done" onclick="alert(1)'}]}
    html=focus_cards_html([card])
    assert '<img' not in html and '&lt;img' in html
    assert '<li class="pending">' in html and 'onclick=' not in html


def test_cards_show_maximum_ten_unique_focused_symbols_and_empty_state():
    state,assessment=snapshots()
    template=state['items'][0]
    state['items']=[{**template,'ticker':f'T{i}'} for i in range(12)]
    state['items'].insert(1,state['items'][0])
    cards=focus_cards(state,assessment,now=NOW)
    assert len(cards)==len({c['symbol'] for c in cards})==10
    assert '집중 종목을 선정' in focus_cards_html([])
    assert focus_cards({},now=NOW)==[]


@pytest.mark.parametrize('missing',['evaluated_at','five_at','fifteen_at','progress'])
def test_explicit_null_stage_fields_render_waiting_without_exception(missing):
    state,assessment=snapshots();saved=assessment['rows'][0]
    if missing in {'progress','evaluated_at'}:saved[missing]=None
    else:saved['progress'][missing]=None
    card=focus_cards(state,assessment,now=NOW)[0]
    assert not card['stage_fresh'] and card['title']=='판단 대기'
    assert all(step['state']=='pending' for step in card['steps'])


def test_preopen_message_requires_confirmed_session_and_current_focus(monkeypatch):
    state,assessment=snapshots();assessment['rows'][0]['progress']=None
    monkeypatch.setattr('sessionquant.session_window.schedule_times',lambda now:{'opens_at':NOW+timedelta(hours=1)})
    card=focus_cards(state,assessment,now=NOW)[0]
    assert card['title']=='개장 대기'
    assert card['explanation']=='정규장 분봉이 쌓이면 전략을 확인해요.'
    assert all(step['state']=='pending' for step in card['steps'])
    monkeypatch.setattr('sessionquant.session_window.schedule_times',lambda now:None)
    assert focus_cards(state,assessment,now=NOW)[0]['title']=='판단 대기'
    state['updated_at']=(NOW-timedelta(hours=1)).isoformat()
    assert focus_cards(state,assessment,now=NOW)[0]['title']=='목록 갱신 대기'
