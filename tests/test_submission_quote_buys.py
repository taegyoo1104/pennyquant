from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json
from pathlib import Path

import pytest

from sessionquant.portfolio import PaperBroker, PortfolioSettings, OrderIntent, serialize
from sessionquant.session_config import SessionConfig, load_session_config
from sessionquant.dashboard.trade_reasons import fill_timing_label
from sessionquant.dashboard.trade_cards import trade_cards_html
from test_paper_market_fills import NOW, intent, quote
from test_live_quotes import prepare, receiver, offer
from test_realtime_paper import observed, NOW as ENGINE_NOW  # noqa: F401


def test_submission_fills_on_current_ask_with_costs_without_waiting_for_new_quote():
    broker=PaperBroker()
    book=quote(-1,ask='105')
    # The engine has already used this fresh book for valuation/strategy.
    broker.process_quote(book,NOW)
    order=broker.submit_buy_with_quote(intent(),book,NOW)
    fill=broker.fills[0]
    assert order.status=='filled' and order.accepted_at==order.submitted_at==NOW
    assert fill.timestamp==NOW and fill.quote_timestamp==book.timestamp and fill.observed_latency==0
    assert fill.assumed_latency==0 and fill.price==D('105.105') and fill.fee==D('.02')
    assert broker.cash==D('100')-fill.qty*fill.price-fill.fee and broker.reconcile()['ok']
    assert order._order_audit()['buy_fill_timing']=='submission_quote'
    assert broker.submit_buy_with_quote(intent(),book,NOW) is order
    assert len(broker.fills)==1


@pytest.mark.parametrize('invalid', ['stale','future','halted','untradable','guard'])
def test_invalid_submission_book_does_not_create_a_fill(invalid):
    broker=PaperBroker()
    book=quote(-11 if invalid=='stale' else 1 if invalid=='future' else 0,
               halted=invalid=='halted',tradable=invalid!='untradable')
    order=broker.submit_buy_with_quote(intent(),book,NOW,
        buy_fill_guard=(lambda order,book:'runtime_quote_stale_or_invalid') if invalid=='guard' else None)
    assert not broker.fills and broker.cash==100 and order.status=='accepted'
    assert broker.reserved_cash==D('20.02')


def test_three_positions_and_pending_buys_still_reserve_all_slots():
    broker=PaperBroker(PortfolioSettings(max_positions=3))
    for symbol in ('AAPL','MSFT','NVDA'):
        order=broker.submit_buy_with_quote(intent(symbol,symbol=symbol),replace(quote(0),symbol=symbol),NOW)
        assert order.status=='filled'
    fourth=broker.submit_buy_with_quote(intent('AMD',symbol='AMD'),replace(quote(0),symbol='AMD'),NOW)
    assert fourth.status=='rejected' and fourth.rejection_reason=='position_limit' and len(broker.fills)==3
    broker=PaperBroker(PortfolioSettings(max_positions=3))
    for symbol in ('AAPL','MSFT','NVDA'):
        broker.submit_order(intent(symbol,symbol=symbol))
    fourth=broker.submit_buy_with_quote(intent('AMD',symbol='AMD'),replace(quote(0),symbol='AMD'),NOW)
    assert fourth.rejection_reason=='position_limit' and not broker.fills


def test_insufficient_cash_and_daily_halt_cannot_be_bypassed():
    broker=PaperBroker()
    order=broker.submit_buy_with_quote(intent(amount='100'),quote(0),NOW)
    assert order.rejection_reason=='insufficient_cash' and not broker.fills
    broker.halted=True
    order=broker.submit_buy_with_quote(intent('halted'),quote(0),NOW)
    assert order.rejection_reason=='daily_loss_limit' and not broker.fills


def test_partial_fill_does_not_reuse_depth_after_restart_and_keeps_original_deadline():
    broker=PaperBroker()
    book=quote(0,size=1)
    order=broker.submit_buy_with_quote(intent(),book,NOW)
    assert order.status=='partial' and order.filled_qty==D('.1')
    broker=PaperBroker.from_state(json.loads(json.dumps(broker.to_state())))
    broker.submit_buy_with_quote(intent(),book,NOW)
    assert not broker.process_quote(book,NOW+timedelta(seconds=1))
    assert len(broker.fills)==1
    assert broker.process_quote(quote(2,size=1))
    assert broker.orders['buy'].status=='filled' and broker.reconcile()['ok']
    # A later add cannot replenish the same book's already used depth.
    add=replace(intent('add',at=NOW+timedelta(seconds=2)),is_add=True)
    broker.submit_buy_with_quote(add,quote(2,size=1),NOW+timedelta(seconds=2))
    assert broker.orders['add'].filled_qty < D('.001')
    broker.expire_orders(NOW+timedelta(seconds=62))
    assert broker.orders['add'].status=='cancelled'


def test_conflicting_or_older_book_and_preexisting_order_cannot_get_instant_fill():
    broker=PaperBroker()
    broker.process_quote(quote(0),NOW)
    broker.submit_buy_with_quote(intent(),quote(0,ask='101'),NOW)
    assert not broker.fills
    broker.submit_buy_with_quote(intent(),quote(1),NOW+timedelta(seconds=1))
    assert not broker.fills  # Resubmission of the same ID is never another attempt.
    broker=PaperBroker()
    broker.submit_order(intent())
    broker.submit_buy_with_quote(intent(),quote(0),NOW)
    assert not broker.fills and broker.orders['buy'].assumed_latency==1


def test_sell_behavior_is_still_post_acceptance_quote():
    broker=PaperBroker()
    broker.submit_buy_with_quote(intent(),quote(0),NOW)
    sell=OrderIntent('sell','AAPL','sell',NOW,D('99.8'),qty=broker.positions['AAPL'].qty)
    broker.submit_order(sell)
    assert not broker.process_quote(quote(0))
    assert broker.process_quote(quote(1))[0].side=='sell'
    assert not broker.positions


def test_runtime_default_changes_only_websocket_paper_profile():
    cfg=load_session_config(Path('config/sessionquant.strategy-lab-websocket.example.yaml'))
    assert cfg.submission_quote_buys and replace(cfg,paper_buy_fill_timing=None).submission_quote_buys
    assert not replace(cfg,paper_buy_fill_timing='next_quote').submission_quote_buys
    assert not SessionConfig().submission_quote_buys
    with pytest.raises(ValueError):replace(cfg,paper_buy_fill_timing='unknown')
    with pytest.raises(ValueError):replace(cfg,portfolio={'paper_fill_model':'legacy_limit_v1'})


def test_engine_records_immediate_fill_and_replays_same_state(observed,tmp_path,monkeypatch):
    from sessionquant.research import journal
    from sessionquant.research.bundle import export_recording
    from sessionquant.research.replay import replay_recording
    engine=observed.engine
    engine.strategy=engine.market.strategy='pullback_simple'
    engine.use_market_filter=engine.market.use_market_filter=False
    engine.submission_quote_buys=True
    engine.quote_recovery_wait=True
    feed,clock,delivery=receiver(ENGINE_NOW)
    monkeypatch.setattr(journal,'clock',lambda:clock.now)
    engine.quote_entry_guard=lambda s,t:delivery.entry_allowed(s,t,known_quotes=engine.market.quotes)
    engine.execution_guard.clock=lambda:clock.now
    journal.start(observed.repo,engine,source='SYNTHETIC_WEBSOCKET')
    observed.repo.record('universe_history',observed.universe.snapshots[0])
    clock.now+=timedelta(seconds=1)
    book=prepare(observed)
    offer(feed,replace(book,timestamp=clock.now,received_at=clock.now))
    clock.now+=timedelta(seconds=1)
    engine.apply_batch(delivery.events(['AAPL'],clock.now),clock.now)
    assert len(engine.broker.fills)==1 and not engine.broker.get_open_orders()
    fill=engine.broker.fills[0]
    assert fill.timestamp==clock.now and fill.observed_latency==0
    assert [check['phase'] for check in engine.execution_guard.checks]==['submission','fill']
    row=observed.repo.all('executions')[0]
    assert row['buy_fill_timing']=='submission_quote'
    assert fill_timing_label(row)=='주문 시점 호가 기준 모의 체결'
    assert '주문 시점 호가 기준 모의 체결' in trade_cards_html([row])
    assert fill_timing_label(serialize(engine.broker.orders[fill.client_id]))=='주문 시점 호가 기준 모의 체결'
    journal.stop(observed.repo)
    export_recording(observed.repo,tmp_path/'recording')
    result=replay_recording(tmp_path/'recording',tmp_path/'replay.db',use_market_filter=False)
    assert result['state']==engine.broker.to_state()


def test_immediate_engine_still_checks_actual_clock_at_fill(observed):
    engine=observed.engine
    engine.submission_quote_buys=True
    book=prepare(observed)
    now=ENGINE_NOW+timedelta(seconds=2)
    feed,clock,delivery=receiver(now)
    engine.quote_entry_guard=lambda s,t:delivery.entry_allowed(s,t,known_quotes=engine.market.quotes)
    # Permission at submission does not authorize a fill after a processing stall.
    checks=iter([now,now+timedelta(seconds=11)])
    engine.execution_guard.clock=lambda:next(checks)
    offer(feed,replace(book,timestamp=now,received_at=now))
    engine.apply_batch(delivery.events(['AAPL'],now),now)
    assert not engine.broker.fills
    assert engine.broker.get_open_orders()[0].unfilled_reason=='runtime_quote_stale_or_invalid'
    assert [check['allowed'] for check in engine.execution_guard.checks]==[True,False]
