"""Operator stop may resume only after liquidation; ledger and risk limits survive."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from streamlit.testing.v1 import AppTest

from sessionquant.database.repository import Repository
from sessionquant.trading_controls import KEY, begin_session, enqueue, request_command
from test_trading_controls import fixture_engine, quotes


def test_stop_then_confirmed_resume_preserves_ledger_signals_and_risk_halt(tmp_path):
    repo,engine,state,entry,events=fixture_engine(tmp_path)
    try:
        enqueue(repo,'start',run_id=state['run_id'],now=entry)
        engine.apply_batch(events,entry)
        enqueue(repo,'stop_all',run_id=state['run_id'],now=entry+timedelta(seconds=1))
        engine.apply_batch(quotes(events,entry+timedelta(seconds=1)),entry+timedelta(seconds=1))
        assert state['status']=='STOPPED' and state['draining']
        assert not engine.broker.positions and not engine.broker.get_open_orders()
        before=deepcopy(engine.broker.to_state());signals=set(engine.consumed_signals)
        engine.broker.halted=True
        command=request_command(repo,'start',run_id=state['run_id'],environment='PAPER_LOCAL',now=entry+timedelta(seconds=2))
        assert not state['running']  # Clicking only queues; engine owns the transition.
        engine.apply_batch([],entry+timedelta(seconds=2))
        after=engine.broker.to_state()
        assert state['running'] and not state['draining'] and state['status']=='RUNNING'
        assert engine.broker.halted and engine.consumed_signals==signals
        for key in ('cash','realized_pnl','positions','orders','fills'):
            assert after[key]==before[key]
        assert repo.get_record('order_intents',command['id'])['status']=='accepted'
    finally:repo.close()


@pytest.mark.parametrize('change',[
    {'status':'LIQUIDATING'}, {'data_ready':False}, {'completed':True},
    {'mode':'live'}, {'draining':False}, {'running':True},
])
def test_resume_request_rejects_unready_terminal_or_live_state(tmp_path,change):
    repo=Repository(tmp_path/'control.db',backend='sqlite')
    try:
        now=datetime.now(timezone.utc);state=begin_session(repo,mode='paper',now=now)
        state.update(status='STOPPED',draining=True,**{k:v for k,v in change.items() if k not in ('status','draining')})
        state.update(change);repo.set_control(KEY,state)
        environment='LIVE_LOCAL' if state['mode']=='live' else 'PAPER_LOCAL'
        with pytest.raises(ValueError):
            request_command(repo,'start',run_id=state['run_id'],environment=environment,now=now)
        assert not repo.all('order_intents')
    finally:repo.close()


def test_engine_rechecks_liquidation_even_if_stale_stopped_screen_queues_resume(tmp_path):
    repo,engine,state,entry,events=fixture_engine(tmp_path)
    try:
        enqueue(repo,'start',run_id=state['run_id'],now=entry)
        engine.apply_batch(events,entry)
        assert engine.broker.get_open_orders()
        # A stale UI projection must never override the engine's current open orders.
        state.update(status='STOPPED',running=False,draining=True);repo.set_control(KEY,state)
        command=request_command(repo,'start',run_id=state['run_id'],environment='PAPER_LOCAL',now=entry+timedelta(seconds=1))
        engine.apply_batch([],entry+timedelta(seconds=1))
        assert not state['running']
        assert repo.get_record('order_intents',command['id'])['status']=='rejected'
        assert not engine.broker.fills
    finally:repo.close()


def test_completed_engine_rejects_even_direct_queued_start(tmp_path):
    repo,engine,state,entry,events=fixture_engine(tmp_path)
    try:
        state.update(completed=True)
        command=enqueue(repo,'start',run_id=state['run_id'],now=entry)
        engine.apply_batch(events,entry)
        assert not state['running'] and not engine.broker.orders
        assert repo.get_record('order_intents',command['id'])['status']=='rejected'
    finally:repo.close()


def test_resume_waits_for_actual_position_liquidation(tmp_path):
    repo,engine,state,entry,events=fixture_engine(tmp_path)
    try:
        enqueue(repo,'start',run_id=state['run_id'],now=entry)
        engine.apply_batch(events,entry)
        engine.apply_batch(quotes(events,entry+timedelta(seconds=1)),entry+timedelta(seconds=1))
        assert engine.broker.positions
        enqueue(repo,'stop_all',run_id=state['run_id'],now=entry+timedelta(seconds=2))
        engine.apply_batch([],entry+timedelta(seconds=2))
        assert state['status']=='LIQUIDATING' and engine.broker.positions
        with pytest.raises(ValueError):
            request_command(repo,'start',run_id=state['run_id'],environment='PAPER_LOCAL',now=entry+timedelta(seconds=2))
        engine.apply_batch(quotes(events,entry+timedelta(seconds=3)),entry+timedelta(seconds=3))
        assert state['status']=='STOPPED' and not engine.broker.positions and not engine.broker.get_open_orders()
        before=deepcopy(engine.broker.to_state())
        request_command(repo,'start',run_id=state['run_id'],environment='PAPER_LOCAL',now=entry+timedelta(seconds=4))
        engine.apply_batch([],entry+timedelta(seconds=4))
        assert state['running']
        after=engine.broker.to_state()
        for key in ('cash','realized_pnl','positions','orders','fills'):
            assert after[key]==before[key]
    finally:repo.close()


@pytest.mark.parametrize('compact',[False,True])
def test_dashboard_requires_explicit_resume_confirmation_and_never_writes_an_order(tmp_path,compact):
    now=datetime.now(timezone.utc);path=tmp_path/'ui.db'
    repo=Repository(path,backend='sqlite');state=begin_session(repo,mode='paper',now=now)
    state.update(status='STOPPED',draining=True);repo.set_control(KEY,state);repo.close()
    source=f'''
from sessionquant.database.repository import Repository
from sessionquant.trading_controls import render_controls
repo=Repository({str(path)!r},backend='sqlite')
render_controls(repo,positions=[],compact={compact!r})
repo.close()
'''
    app=AppTest.from_string(source).run(timeout=10)
    assert not app.exception and app.button(key='trading-start').label=='매매 재개'
    assert app.button(key='trading-start').disabled
    app.checkbox(key='confirm-resume-'+state['run_id']).check().run()
    assert not app.button(key='trading-start').disabled
    app.button(key='trading-start').click().run()
    assert app.button(key='trading-start').disabled
    repo=Repository(path,backend='sqlite')
    try:
        commands=repo.all('order_intents')
        assert len(commands)==1 and commands[0]['action']=='start' and commands[0]['status']=='pending'
        assert repo.get_control(KEY)['status']=='STOPPED'
        assert not repo.all('orders') and not repo.all('executions')
    finally:repo.close()
