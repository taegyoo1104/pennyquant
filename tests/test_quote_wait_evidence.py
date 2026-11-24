from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from sessionquant.models import Quote
from sessionquant.session_engine import SessionEngine
from sessionquant.dashboard.trade_reasons import order_quote_wait_details
from test_live_quotes import receiver, offer
from test_realtime_paper import NOW


@pytest.mark.parametrize('fault', ['feed_not_ready', 'quote_missing', 'quote_conflict', 'quote_time_invalid',
    'quote_stale', 'engine_quote_missing', 'stream_behind_engine', 'post_conflict_not_delivered',
    'generation_not_delivered', 'engine_quote_conflict'])
def test_guard_evidence_identifies_failed_branch_without_changing_book(fault):
    feed, clock, delivery = receiver(NOW)
    quote=Quote('AAPL', NOW, NOW, 100, 101, 10, 10)
    offer(feed, quote)
    known={'AAPL':quote}
    if fault=='feed_not_ready':feed._invalidate('DISCONNECTED')
    elif fault=='quote_missing':feed._quotes.clear()
    elif fault=='quote_conflict':offer(feed,replace(quote,bid=101,ask=102))
    elif fault=='quote_time_invalid':feed._quotes['AAPL']=replace(quote,received_at=NOW+timedelta(seconds=1))
    elif fault=='quote_stale':clock.now+=timedelta(seconds=11)
    elif fault=='engine_quote_missing':known={}
    elif fault=='stream_behind_engine':known['AAPL']=replace(quote,timestamp=NOW+timedelta(seconds=1))
    elif fault=='engine_quote_conflict':known['AAPL']=replace(quote,bid=101,ask=102)
    else:
        if fault=='post_conflict_not_delivered':offer(feed,replace(quote,bid=101,ask=102))
        clock.now+=timedelta(seconds=1)
        offer(feed,replace(quote,timestamp=clock.now,received_at=clock.now))
    cached=dict(feed._quotes)
    assert not delivery.entry_allowed('AAPL',clock.now,known_quotes=known)
    evidence=delivery.entry_evidence('AAPL')
    assert evidence['reason']==fault and evidence['checked_at']==clock.now.isoformat()
    assert evidence['engine_quote_at']==(known['AAPL'].timestamp.isoformat() if known else None)
    assert evidence['max_age_seconds']==10
    assert feed._quotes==cached
    # Caller mutations cannot change the cached guard evidence.
    evidence['reason']='changed'
    assert delivery.entry_evidence('AAPL')['reason']==fault


def test_evidence_clears_on_recovery_and_subscription_removal():
    feed, clock, delivery=receiver(NOW)
    quote=Quote('AAPL',NOW,NOW,100,101,10,10)
    assert not delivery.entry_allowed('AAPL',NOW)
    offer(feed,quote)
    assert delivery.entry_allowed('AAPL',NOW,known_quotes={'AAPL':quote})
    assert delivery.entry_evidence('AAPL')=={}
    clock.now+=timedelta(seconds=11)
    assert not delivery.entry_allowed('AAPL',clock.now,known_quotes={'AAPL':quote})
    delivery.synchronize(SimpleNamespace(light_symbols=lambda now:[],focus=lambda now:[]),
                         SimpleNamespace(positions={},exit_intents={},get_open_orders=lambda:[]),clock.now)
    assert delivery._entry_evidence=={}


def test_diagnostic_payload_is_bounded_and_failure_does_not_escape():
    engine=SessionEngine.__new__(SessionEngine)
    engine.quote_entry_evidence=lambda symbol:{'reason':'quote_stale','quote_at':'x'*97,
                                             'generation':{'nested':'value'},'extra':'x'*10000}
    assert engine._quote_wait_evidence('AAPL')=={'reason':'quote_stale'}
    def broken(symbol):raise RuntimeError('private provider message')
    engine.quote_entry_evidence=broken
    assert engine._quote_wait_evidence('AAPL')=={'reason':'evidence_unavailable'}


def test_wait_display_explains_clock_and_connection_without_guessing_old_reason():
    row={'lifecycle':[{'order_audit':{'quote_wait':{'started_at':NOW.isoformat(),'evidence':{
        'reason':'quote_stale','quote_at':NOW.isoformat(),'quote_age_seconds':11,'max_age_seconds':10}}}}]}
    result=order_quote_wait_details(row)
    assert result['호가 대기 원인']=='호가가 허용 시간보다 오래됨'
    assert result['수신 호가 발생 시각'].endswith('KST')
    assert result['호가 경과(초)']==11 and result['허용 경과(초)']==10
    assert order_quote_wait_details({})['호가 대기 원인']=='기록 없음'
    evidence=row['lifecycle'][0]['order_audit']['quote_wait']['evidence']
    evidence.update(reason='feed_not_ready',connection_state='BACKOFF')
    assert order_quote_wait_details(row)['호가 대기 원인']=='연결 재시도 대기'
    evidence['reason']='new_internal_reason'
    assert order_quote_wait_details(row)['호가 대기 원인']=='사유 확인 필요'
