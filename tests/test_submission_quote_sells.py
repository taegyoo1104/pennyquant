from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json

import pytest

from sessionquant.portfolio import PaperBroker, OrderIntent, serialize
from sessionquant.portfolio.execution_guard import SellExecutionGuard
from sessionquant.session_config import SessionConfig, load_session_config
from sessionquant.dashboard.trade_reasons import fill_timing_label, execution_reasons
from sessionquant.dashboard.trade_cards import trade_cards_html
from test_paper_market_fills import NOW, bought, quote
from test_runtime_execution_guard import runtime, apply, book as runtime_book  # noqa: F401


def sell(broker, *, ident='sell', at=None, qty=None):
    return OrderIntent(ident,'AAPL','sell',at or NOW+timedelta(seconds=2),D('99.8'),
        qty=qty or broker.positions['AAPL'].qty,expected_price=D('99.9'),reason='stop_loss')


def test_sell_uses_current_bid_and_costs_immediately_without_double_execution():
    broker=bought(); now=NOW+timedelta(seconds=2)
    book=quote(2,'99','99.1')
    broker.process_quote(book,now)
    order=broker.submit_sell_with_quote(sell(broker),book,now)
    fill=broker.fills[-1]
    assert order.status=='filled' and order.accepted_at==order.submitted_at==fill.timestamp==now
    assert fill.price==D('98.901') and fill.side=='sell' and fill.reason=='stop_loss'
    assert fill.observed_latency==0 and fill.assumed_latency==0
    assert not broker.positions and broker.realized_pnl==broker.cash-100 and broker.reconcile()['ok']
    broker.submit_sell_with_quote(order.intent,book,now)
    assert len(broker.fills)==2


@pytest.mark.parametrize('fault',['stale','future','halted','conflict'])
def test_unusable_sell_book_keeps_position_and_order(fault):
    broker=bought(); now=NOW+timedelta(seconds=20)
    book=quote(0 if fault=='stale' else 21 if fault=='future' else 20,halted=fault=='halted')
    if fault=='conflict':
        broker.process_quote(book,now)
        book=replace(book,bid=90,ask=91)
    old_cash=broker.cash
    order=broker.submit_sell_with_quote(sell(broker,at=now),book,now)
    assert order.status=='accepted' and broker.positions and broker.cash==old_cash
    assert len(broker.fills)==1


def test_partial_sell_restart_cancel_and_replacement_cannot_renew_same_bid_depth():
    broker=bought(); now=NOW+timedelta(seconds=2);book=quote(2,size=1)
    original=broker.positions['AAPL'].qty
    order=broker.submit_sell_with_quote(sell(broker),book,now)
    assert order.status=='partial' and order.filled_qty==D('.1')
    broker=PaperBroker.from_state(json.loads(json.dumps(broker.to_state())))
    broker.cancel_order(order.client_id,now+timedelta(seconds=1))
    replacement=broker.submit_sell_with_quote(sell(broker,ident='rest',at=now+timedelta(seconds=1)),book,
                                              now+timedelta(seconds=1))
    assert replacement.filled_qty==0 and broker.positions['AAPL'].qty==original-D('.1')
    broker.process_quote(quote(4))
    assert not broker.positions and replacement.status=='filled'
    assert sum(f.qty for f in broker.fills if f.side=='sell')==original and broker.reconcile()['ok']


def test_pending_sell_reservation_prevents_overselling():
    broker=bought();now=NOW+timedelta(seconds=2)
    first=broker.submit_order(sell(broker))
    second=broker.submit_sell_with_quote(sell(broker,ident='again'),quote(2),now)
    assert second.status=='rejected' and second.rejection_reason=='insufficient_sellable_quantity'
    broker.submit_sell_with_quote(first.intent,quote(2),now)
    assert len(broker.fills)==1 and first.assumed_latency==1


def test_exit_guard_allows_halted_buys_after_entry_window_but_checks_real_exit_time():
    broker=bought();broker.halted=True
    now=NOW.replace(hour=19,minute=41)
    book=quote(0,at=now,tradable=False)
    guard=SellExecutionGuard(broker,lambda:now)
    order=broker.submit_sell_with_quote(sell(broker,at=now),book,now,
        sell_fill_guard=lambda order,book:guard.fill_blocker(order,book,now))
    assert order.status=='filled' and guard.checks[-1]['allowed']
    broker=bought();now=NOW.replace(hour=19,minute=59,second=59);book=quote(0,at=now)
    guard=SellExecutionGuard(broker,lambda:now+timedelta(seconds=2))
    order=broker.submit_sell_with_quote(sell(broker,at=now),book,now,
        sell_fill_guard=lambda order,book:guard.fill_blocker(order,book,now))
    assert len(broker.fills)==1 and order.unfilled_reason=='runtime_exit_window_closed'


@pytest.mark.parametrize('check',['stale','rollback','missing_record'])
def test_sell_runtime_guard_never_assumes_permission(check):
    broker=bought();now=NOW+timedelta(seconds=2);book=quote(2)
    guard=SellExecutionGuard(broker,lambda:now+timedelta(seconds=11 if check=='stale' else -1))
    if check=='missing_record':guard.recorded=[]
    broker.submit_sell_with_quote(sell(broker),book,now,
        sell_fill_guard=lambda order,book:guard.fill_blocker(order,book,now))
    assert len(broker.fills)==1 and not guard.checks[-1]['allowed']


@pytest.mark.parametrize('reason',['stop_loss','net_profit_target','profit_protection','holding_time_exit',
    'daily_loss_limit','confirmed_adverse_news','manual_market_sell','operator_stop_all','session_exit'])
def test_engine_all_exit_routes_fill_in_the_signal_batch_even_when_websocket_buy_guard_is_false(runtime,reason):
    v=runtime;engine=v.engine
    engine.submission_quote_sells=True
    engine.apply_batch(v.initial,v.entry);apply(v,1)
    position=engine.broker.positions['AAPL']
    bid=float(position.avg_price)
    offset=2
    if reason=='stop_loss':bid=float(position.stop_price)-.01
    elif reason=='net_profit_target':bid*=1.2
    elif reason=='profit_protection':
        bid*=1.04;engine._bearish=lambda symbol,now:True
    elif reason=='holding_time_exit':
        engine.manager.exit_policy=replace(engine.manager.exit_policy,max_holding_minutes=1);offset=62
    elif reason=='daily_loss_limit':engine.broker.halted=True
    elif reason=='confirmed_adverse_news':engine.risk_context=lambda now,symbol:{'major_adverse':True,'block_entries':True}
    elif reason=='session_exit':offset=(engine.exit_manager.schedule(v.entry).exit_start-v.entry).total_seconds()
    else:
        engine.broker.exit_intents['AAPL']={'reason':reason,'opened_at':position.opened_at.isoformat(),
                                          'requested_at':v.entry.isoformat(),'attempt':0}
        if reason=='operator_stop_all':v.repo.set_control('manual_halt',True)
    engine.quote_entry_guard=lambda symbol,now:False
    apply(v,offset,bid=bid)
    fills=[f for f in engine.broker.fills if f.side=='sell']
    assert len(fills)==1 and fills[0].reason==reason and fills[0].timestamp==v.clock.now
    assert fills[0].observed_latency==0 and not engine.broker.positions
    row=next(row for row in v.repo.all('executions') if row['side']=='sell')
    assert row['sell_fill_timing']=='submission_quote'
    assert fill_timing_label(row)=='주문 시점 호가 기준 모의 체결'
    html=trade_cards_html(execution_reasons([row]))
    assert '주문 시점 호가 기준 모의 체결' in html and '매도 이유' in html


def test_engine_stall_preserves_exit_then_next_valid_book_fills(runtime,monkeypatch):
    v=runtime;engine=v.engine;engine.submission_quote_sells=True
    engine.apply_batch(v.initial,v.entry);apply(v,1)
    position=engine.broker.positions['AAPL']
    engine.broker.exit_intents['AAPL']={'reason':'manual_market_sell','opened_at':position.opened_at.isoformat(),
                                      'requested_at':v.entry.isoformat(),'attempt':0}
    original=engine.broker.submit_sell_with_quote
    def delayed(*args,**kwargs):
        v.clock.now+=timedelta(seconds=11)
        return original(*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(engine.broker,'submit_sell_with_quote',delayed)
        apply(v,2)
    assert len(engine.broker.fills)==1 and engine.broker.positions
    assert engine.broker.get_open_orders()[0].unfilled_reason=='runtime_quote_stale_or_invalid'
    assert v.repo.load_state()['last_sell_execution_check_at']==v.clock.now.isoformat()
    from sessionquant.session_engine import SessionEngine
    v.engine=engine=SessionEngine(v.repo,v.universe,restore=True,submission_quote_sells=True,**v.options)
    assert engine.sell_execution_guard.last_checked_at==v.clock.now
    apply(v,14)
    assert len(engine.broker.fills)==2 and not engine.broker.positions
    assert next(row for row in v.repo.all('executions') if row['side']=='sell')['sell_fill_timing']=='next_quote'


def test_conflicting_input_batch_cannot_instantly_clear_a_position(runtime):
    v=runtime;engine=v.engine;engine.submission_quote_sells=True
    engine.apply_batch(v.initial,v.entry);apply(v,1)
    position=engine.broker.positions['AAPL']
    engine.broker.exit_intents['AAPL']={'reason':'manual_market_sell','opened_at':position.opened_at.isoformat(),
                                      'requested_at':v.entry.isoformat(),'attempt':0}
    v.clock.now=v.entry+timedelta(seconds=2)
    engine.apply_batch([runtime_book(v,2,bid=100),runtime_book(v,2,bid=99)],v.clock.now)
    assert len(engine.broker.fills)==1 and engine.broker.positions
    apply(v,3)
    assert len(engine.broker.fills)==2 and not engine.broker.positions


def test_new_sell_policy_records_and_replays_execution_checks(runtime,tmp_path,monkeypatch):
    from sessionquant.research import journal
    from sessionquant.research.bundle import export_recording
    from sessionquant.research.replay import replay_recording
    v=runtime;engine=v.engine;engine.submission_quote_buys=True;engine.submission_quote_sells=True
    monkeypatch.setattr(journal,'clock',lambda:v.clock.now)
    journal.start(v.repo,engine,source='SYNTHETIC_FIXTURE')
    v.repo.record('universe_history',v.universe.snapshots[0])
    engine.apply_batch(v.initial,v.entry)
    assert engine.broker.positions
    bid=float(engine.broker.positions['AAPL'].stop_price)-.01
    apply(v,2,bid=bid)
    assert [f.side for f in engine.broker.fills]==['buy','sell']
    assert engine.sell_execution_guard.checks[-1]['allowed']
    journal.stop(v.repo)
    export_recording(v.repo,tmp_path/'recording')
    result=replay_recording(tmp_path/'recording',tmp_path/'replay.db',use_market_filter=False)
    assert result['state']==engine.broker.to_state()


def test_sell_policy_is_independent_and_explicit_for_websocket_only():
    cfg=load_session_config('config/sessionquant.strategy-lab-websocket.example.yaml')
    assert cfg.submission_quote_sells and replace(cfg,paper_sell_fill_timing=None).submission_quote_sells
    assert not replace(cfg,paper_sell_fill_timing='next_quote').submission_quote_sells
    assert replace(cfg,paper_buy_fill_timing='next_quote').submission_quote_sells
    assert not SessionConfig().submission_quote_sells
    with pytest.raises(ValueError):replace(cfg,paper_sell_fill_timing='unknown')
