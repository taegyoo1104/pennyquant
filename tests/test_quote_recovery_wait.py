from dataclasses import replace
from datetime import timedelta

import pytest

from sessionquant.models import jsonable
from sessionquant.portfolio.broker import PaperBroker
from sessionquant.session_engine import SessionEngine
from test_live_quotes import offer, prepare, receiver
from test_realtime_paper import observed, NOW  # noqa: F401


def pending(value):
    engine = value.engine
    engine.quote_recovery_wait = True
    quote = prepare(value)
    now = NOW + timedelta(seconds=2)
    feed, clock, delivery = receiver(now)
    engine.quote_entry_guard = lambda s, t: delivery.entry_allowed(s, t, known_quotes=engine.market.quotes)
    offer(feed, replace(quote, timestamp=now, received_at=now))
    engine.apply_batch(delivery.events(['AAPL'], now), now)
    return engine, quote, feed, clock, delivery, engine.broker.get_open_orders()[0]


@pytest.mark.parametrize('fault', ['disconnect', 'stale', 'conflict', 'engine_ahead'])
def test_transient_quote_failure_keeps_order_but_never_fills_until_valid_stream_book(observed, fault):
    engine, quote, feed, clock, delivery, order = pending(observed)
    clock.now += timedelta(seconds=11)
    if fault == 'disconnect':
        feed._invalidate('DISCONNECTED')
    elif fault == 'conflict':
        old = feed._quotes['AAPL']
        offer(feed, replace(old, bid=old.bid+1, ask=old.ask+1, received_at=clock.now))
    elif fault == 'engine_ahead':
        offer(feed, replace(quote, timestamp=clock.now-timedelta(seconds=1), received_at=clock.now))
    # A fresh REST risk snapshot alone must not unlock a websocket buy.
    engine.apply_batch([{'kind': 'quote', 'data': jsonable(replace(quote, timestamp=clock.now,
                                                              received_at=clock.now))}], clock.now)
    assert engine.broker.get_open_orders() == [order] and not engine.broker.fills
    assert order.unfilled_reason == 'websocket_quote_unavailable'
    assert order.cancel_requested_at is None
    reserved = engine.broker.reserved_cash
    interval = dict(order.quote_wait)
    engine.tick(clock.now + timedelta(milliseconds=500))
    assert order.quote_wait == interval and engine.broker.reserved_cash == reserved
    clock.now += timedelta(seconds=2)
    feed._acked = True
    newer = replace(quote, timestamp=clock.now, received_at=clock.now, bid=quote.bid+1, ask=quote.ask+1)
    offer(feed, newer)
    engine.apply_batch(delivery.events(['AAPL'], clock.now, known_quotes=engine.market.quotes), clock.now)
    assert [f.side for f in engine.broker.fills] == ['buy']
    assert engine.broker.fills[0].price >= newer.ask
    assert order.quote_wait['recovered_at'] == clock.now.isoformat()
    assert len(observed.repo.all('order_intents')) == 1
    assert order.cancel_reason == ''


@pytest.mark.parametrize('cancel_delay', [0, 5])
def test_unrecovered_order_expires_on_original_ttl_and_preserves_wait_reason(observed, cancel_delay):
    observed.engine.broker.settings = replace(observed.engine.broker.settings,
                                             paper_cancel_latency_seconds=str(cancel_delay))
    engine, quote, feed, clock, delivery, order = pending(observed)
    submitted = order.submitted_at
    feed._invalidate('DISCONNECTED')
    engine.tick(submitted + timedelta(seconds=10))
    engine.tick(submitted + timedelta(seconds=59))
    assert engine.broker.get_open_orders() == [order]
    engine.tick(submitted + timedelta(seconds=60))
    assert order.cancel_requested_at == submitted + timedelta(seconds=60)
    assert order.cancel_reason == 'assumed_order_timeout'
    assert order.cancel_unfilled_reason == 'websocket_quote_unavailable'
    engine.tick(submitted + timedelta(seconds=60+cancel_delay))
    assert order.status == 'cancelled' and not engine.broker.fills
    assert not engine.broker.get_open_orders() and engine.broker.reserved_cash == 0
    restored = PaperBroker.from_state(engine.broker.to_state())
    assert restored.orders[order.client_id].cancel_reason == 'assumed_order_timeout'
    assert restored.orders[order.client_id].quote_wait == order.quote_wait


def test_operator_stop_still_cancels_immediately_during_quote_wait(observed):
    engine, quote, feed, clock, delivery, order = pending(observed)
    feed._invalidate('DISCONNECTED')
    engine.tick(clock.now + timedelta(seconds=2))
    observed.repo.set_control('manual_halt', True)
    engine.tick(clock.now + timedelta(seconds=3))
    assert order.status == 'cancelled' and not engine.broker.fills
    assert order.cancel_reason == 'operator_stop_or_data_not_ready'
    assert order.cancel_unfilled_reason == 'websocket_quote_unavailable'


def test_execution_clock_still_blocks_a_buy_past_original_deadline(observed):
    engine, quote, feed, clock, delivery, order = pending(observed)
    clock.now = order.submitted_at + timedelta(seconds=59)
    engine.execution_guard.clock = lambda: clock.now + timedelta(seconds=2)
    offer(feed, replace(quote, timestamp=clock.now, received_at=clock.now))
    engine.apply_batch(delivery.events(['AAPL'], clock.now), clock.now)
    assert not engine.broker.fills
    assert order.unfilled_reason == 'runtime_order_no_longer_active'


@pytest.mark.parametrize('restriction', ['news', 'reference', 'daily_loss'])
def test_risk_restrictions_cancel_waiting_buys_without_waiting_for_ttl(observed, restriction):
    from types import SimpleNamespace
    engine, quote, feed, clock, delivery, order = pending(observed)
    feed._invalidate('DISCONNECTED')
    engine.tick(clock.now + timedelta(seconds=2))
    expected = {'news':'news_risk_restriction', 'reference':'macro_reference_risk_restriction',
                'daily_loss':'daily_loss_limit'}[restriction]
    if restriction == 'news':
        engine.risk_context = lambda now, symbol: {'block_entries': True}
    elif restriction == 'reference':
        engine.macro_reference_risk = SimpleNamespace(evaluate=lambda now: {'block_entries': True})
    else:
        engine.broker.halted = True
    engine.tick(clock.now + timedelta(seconds=3))
    assert order.status == 'cancelled' and not engine.broker.fills
    assert order.cancel_reason == expected


def test_partial_fill_wait_preserves_remaining_order_and_cash_reservation(observed):
    from decimal import Decimal
    engine, quote, feed, clock, delivery, order = pending(observed)
    engine.broker.settings = replace(engine.broker.settings, paper_depth_participation=Decimal('.001'))
    clock.now += timedelta(seconds=2)
    offer(feed, replace(quote, timestamp=clock.now, received_at=clock.now, ask_size=1))
    engine.apply_batch(delivery.events(['AAPL'], clock.now), clock.now)
    assert order.status == 'partial' and len(engine.broker.fills) == 1
    reserved = engine.broker.reserved_cash
    feed._invalidate('DISCONNECTED')
    clock.now += timedelta(seconds=2)
    engine.tick(clock.now)
    assert order.status == 'partial' and engine.broker.reserved_cash == reserved
    feed._acked = True
    clock.now += timedelta(seconds=2)
    offer(feed, replace(quote, timestamp=clock.now, received_at=clock.now))
    engine.apply_batch(delivery.events(['AAPL'], clock.now), clock.now)
    assert len(engine.broker.fills) == 2 and order.status == 'filled'
    assert len(observed.repo.all('order_intents')) == 1


def test_cancel_reason_display_does_not_guess_old_causes_or_expose_raw_codes():
    from sessionquant.dashboard.trade_reasons import order_cancellation_details
    assert order_cancellation_details({'status':'cancelled', 'unfilled_reason':'cancelled'}) == {
        '취소 사유':'기록 없음', '취소 직전 대기 사유':'기록 없음'}
    assert order_cancellation_details({'status':'cancelled', 'lifecycle':[{'order_audit':{
        'cancel_reason':'assumed_order_timeout', 'cancel_unfilled_reason':'websocket_quote_unavailable'}}]}) == {
        '취소 사유':'주문 대기 시간 초과', '취소 직전 대기 사유':'실시간 호가 확인 대기'}
    assert order_cancellation_details({'status':'cancelled', 'lifecycle':[{'order_audit':{
        'cancel_reason':'unknown_new_code'}}]})['취소 사유'] == '사유 확인 필요'


def test_pending_order_restores_and_uses_original_deadline_without_duplicate_submission(observed):
    engine, quote, feed, clock, delivery, order = pending(observed)
    feed._invalidate('DISCONNECTED')
    clock.now += timedelta(seconds=10)
    engine.tick(clock.now)
    restored = SessionEngine(observed.repo, observed.universe, synthetic=True, strategy='pullback_simple',
                             strict_market_data=True, paper_data_policy='observed_paper_v1', use_market_filter=False,
                             candidate_settings={'enabled': True}, restore=True, quote_recovery_wait=True)
    restored.quote_entry_guard = lambda s, t: delivery.entry_allowed(s, t, known_quotes=restored.market.quotes)
    same = restored.broker.get_open_orders()[0]
    assert same.client_id == order.client_id and same.submitted_at == order.submitted_at
    assert same.quote_wait == order.quote_wait
    clock.now += timedelta(seconds=2)
    feed._acked = True
    offer(feed, replace(quote, timestamp=clock.now, received_at=clock.now))
    restored.apply_batch(delivery.events(['AAPL'], clock.now, known_quotes=restored.market.quotes), clock.now)
    assert len(restored.broker.fills) == 1 and len(observed.repo.all('order_intents')) == 1


def test_recovery_policy_is_recorded_and_replayed_without_duplicating_order(observed, tmp_path, monkeypatch):
    from sessionquant.research import journal
    from sessionquant.research.bundle import export_recording
    from sessionquant.research.replay import replay_recording
    engine = observed.engine
    engine.strategy = engine.market.strategy = 'pullback_simple'
    engine.use_market_filter = engine.market.use_market_filter = False
    engine.quote_recovery_wait = True
    feed, clock, delivery = receiver(NOW)
    monkeypatch.setattr(journal, 'clock', lambda: clock.now)
    engine.quote_entry_guard = lambda s, t: delivery.entry_allowed(s, t, known_quotes=engine.market.quotes)
    journal.start(observed.repo, engine, source='SYNTHETIC_WEBSOCKET')
    observed.repo.record('universe_history', observed.universe.snapshots[0])
    clock.now += timedelta(seconds=1)
    quote = prepare(observed)
    offer(feed, replace(quote, timestamp=clock.now, received_at=clock.now))
    clock.now += timedelta(seconds=1)
    engine.apply_batch(delivery.events(['AAPL'], clock.now), clock.now)
    feed._invalidate('DISCONNECTED')
    clock.now += timedelta(seconds=10)
    engine.tick(clock.now)
    assert engine.broker.get_open_orders()
    clock.now += timedelta(seconds=2)
    feed._acked = True
    offer(feed, replace(quote, timestamp=clock.now, received_at=clock.now))
    engine.apply_batch(delivery.events(['AAPL'], clock.now), clock.now)
    journal.stop(observed.repo)
    export_recording(observed.repo, tmp_path/'recording')
    replay = replay_recording(tmp_path/'recording', tmp_path/'replay.db', use_market_filter=False)
    assert replay['state'] == engine.broker.to_state()
