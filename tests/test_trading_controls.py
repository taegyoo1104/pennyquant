"""시작 대기·수동 청산·종료 우선순위와 청산 직전 진입 차단을 검증한다."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import copy
import pytest

from sessionquant.database.repository import Repository
from sessionquant.models import utc
from sessionquant.session_demo import synthetic_tape
from sessionquant.session_engine import SessionEngine,event_time
from sessionquant.market.universe import UniverseStore
from sessionquant.trading_controls import begin_session,enqueue,heartbeat,pending,KEY
from sessionquant.session_window import schedule_times,permits_new_entry


def fixture_engine(tmp_path):
    rows,opened=synthetic_tape()
    universe=UniverseStore(); universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=opened-timedelta(days=1))
    repo=Repository(tmp_path/'controlled.db',backend='sqlite')
    state=begin_session(repo,mode='paper',now=opened)
    engine=SessionEngine(repo,universe,synthetic=True,control_state=state)
    for t in sorted({event_time(r) for r in rows if r['kind']=='bar'}):
        engine.apply_batch([r for r in rows if r['kind']=='bar' and event_time(r)==t],t)
    entry=opened+timedelta(minutes=75,seconds=1)
    events=[r for r in rows if event_time(r)==entry]
    return repo,engine,state,entry,events


def quotes(events,now):
    rows=copy.deepcopy([r for r in events if r['kind']=='quote'])
    for row in rows: row['data'].update(timestamp=now.isoformat(),received_at=now.isoformat())
    return rows


def test_startup_waits_for_button_and_risk_exits_work_while_stopped(tmp_path):
    repo,engine,state,entry,events=fixture_engine(tmp_path)
    try:
        engine.apply_batch(events,entry)
        assert not engine.broker.orders
        enqueue(repo,'start',run_id=state['run_id'],now=entry+timedelta(seconds=1))
        engine.apply_batch(quotes(events,entry+timedelta(seconds=1)),entry+timedelta(seconds=1))
        assert len(engine.broker.orders)==1
        engine.apply_batch(quotes(events,entry+timedelta(seconds=2)),entry+timedelta(seconds=2))
        assert engine.broker.positions
        for cid in ('manual-1','manual-2'):
            enqueue(repo,'sell_symbol',run_id=state['run_id'],symbol='AAPL',now=entry+timedelta(seconds=3),command_id=cid)
        engine.apply_batch(quotes(events,entry+timedelta(seconds=3)),entry+timedelta(seconds=3))
        assert sum(o.intent.side=='sell' for o in engine.broker.orders.values())==1
        assert engine.broker.positions
        engine.apply_batch(quotes(events,entry+timedelta(seconds=4)),entry+timedelta(seconds=4))
        assert not engine.broker.positions
    finally: repo.close()


def test_stop_cancels_pending_buy_before_fill_and_beats_start(tmp_path):
    repo,engine,state,entry,events=fixture_engine(tmp_path)
    try:
        enqueue(repo,'start',run_id=state['run_id'],now=entry)
        engine.apply_batch(events,entry)
        assert engine.broker.get_open_orders()
        enqueue(repo,'start',run_id=state['run_id'],now=entry+timedelta(seconds=1))
        enqueue(repo,'stop_all',run_id=state['run_id'],now=entry+timedelta(seconds=1))
        engine.apply_batch(quotes(events,entry+timedelta(seconds=1)),entry+timedelta(seconds=1))
        assert not engine.broker.fills and not engine.broker.get_open_orders()
        assert not state['running'] and state['status']=='STOPPED'
        assert any(r.get('status')=='rejected' for r in repo.all('order_intents') if r.get('action')=='start')
    finally: repo.close()


def test_previous_process_and_stale_commands_cannot_start_new_engine(tmp_path):
    repo=Repository(tmp_path/'commands.db',backend='sqlite')
    try:
        now=datetime.now(timezone.utc)
        old=begin_session(repo,mode='live',account_seq=7,now=now)
        enqueue(repo,'start',run_id=old['run_id'],account_seq=7,now=now)
        new=begin_session(repo,mode='live',account_seq=7,now=now)
        assert not new['running'] and pending(repo,new,now)==[]
        assert repo.all('order_intents')[0]['status']=='expired'
        with pytest.raises(ValueError): enqueue(repo,'start',run_id=new['run_id'],account_seq=8,now=now)
        with pytest.raises(ValueError): enqueue(repo,'start',run_id=new['run_id'],account_seq=7,now=now+timedelta(seconds=61))
    finally: repo.close()


@pytest.mark.parametrize('minute,allowed',[(13*60+29,True),(13*60+30,False),(14*60+39,False),(14*60+40,False)])
def test_regular_day_cannot_buy_just_before_session_exit(minute,allowed):
    now=datetime(2026,11,13,minute//60+5,minute%60,tzinfo=timezone.utc)
    assert permits_new_entry(now) is allowed
    schedule=schedule_times(now)
    assert schedule['exit_start'].hour==19 and schedule['exit_start'].minute==40
    assert schedule['entry_end']<=schedule['exit_start']-timedelta(minutes=30)


def test_early_close_and_configurable_buffer_cannot_shorten_safety_margin():
    now=datetime(2026,11,27,16,tzinfo=timezone.utc)
    times=schedule_times(now)
    assert times['closes_at'].hour==18 and times['exit_start'].hour==16
    assert not permits_new_entry(now)
    with pytest.raises(ValueError): schedule_times(now,min_entry_to_exit_minutes=1)
    regular=datetime(2026,11,13,17,tzinfo=timezone.utc)
    assert schedule_times(regular,min_entry_to_exit_minutes=120)['entry_end']<schedule_times(regular)['entry_end']


def test_dashboard_queues_only_on_click_and_stop_remains_available(tmp_path):
    from streamlit.testing.v1 import AppTest
    now=datetime.now(timezone.utc); path=tmp_path/'buttons.db'
    repo=Repository(path,backend='sqlite'); state=begin_session(repo,mode='paper',now=now); repo.close()
    # UI의 DB 쓰기는 요청 원장에 한정되며 주문·체결은 엔진 없이 생성되지 않는다.
    source=f"""
from sessionquant.database.repository import Repository
from sessionquant.trading_controls import render_controls
repo=Repository({str(path)!r},backend='sqlite')
render_controls(repo,positions=[{{'symbol':'AAPL','qty':'0.1'}}])
repo.close()
"""
    app=AppTest.from_string(source).run(timeout=10)
    assert not app.exception
    repo=Repository(path,backend='sqlite'); assert repo.all('order_intents')==[]; repo.close()
    app.checkbox(key='confirm-start-'+state['run_id']).check().run()
    app.button(key='trading-start').click().run()
    assert app.button(key='trading-start').disabled
    app.checkbox(key='confirm-stop-'+state['run_id']).check().run()
    assert not app.button(key='trading-stop').disabled
    app.button(key='trading-stop').click().run()
    repo=Repository(path,backend='sqlite')
    assert [x['action'] for x in repo.all('order_intents')]==['start','stop_all']
    assert not repo.all('orders') and not repo.all('executions')
    repo.close()
