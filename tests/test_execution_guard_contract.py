"""Runtime buy permissions are validated per order without changing sell behavior."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from sessionquant.models import Quote
from sessionquant.portfolio import OrderIntent, PaperBroker, PortfolioSettings
from sessionquant.portfolio.execution_guard import BuyExecutionGuard


NOW = datetime(2026, 11, 13, 16, tzinfo=timezone.utc)
D = Decimal


def quote(seconds=1):
    stamp = NOW+timedelta(seconds=seconds)
    return Quote('AAPL', stamp, stamp, 99.9, 100, 1000, 1000, True)


def pending(model='market_v2', ident='buy'):
    broker = PaperBroker(PortfolioSettings(paper_fill_model=model))
    order = broker.submit_order(OrderIntent(ident, 'AAPL', 'buy', NOW, D('100.1'),
                                           amount=D('20'), stop_price=D('98')))
    return broker, order


def permission(order, book, *, allowed=True, reason='', checked_at=None, phase='fill'):
    return {'phase':phase, 'order_id':order.client_id, 'symbol':book.symbol,
            'quote_at':book.timestamp.isoformat(), 'quote_received_at':book.received_at.isoformat(),
            'checked_at':(checked_at or book.received_at).isoformat(), 'allowed':allowed, 'reason':reason}


def fill(broker, guard, book):
    return broker.process_quote(book, book.received_at,
        buy_fill_guard=lambda order, value: guard.fill_blocker(order, value, book.received_at))


@pytest.mark.parametrize('model', ['market_v2', 'legacy_limit_v1'])
def test_recorded_denial_with_empty_reason_cannot_turn_into_fill(model):
    broker, order = pending(model)
    book = quote()
    guard = BuyExecutionGuard(broker)
    guard.recorded = [permission(order, book, allowed=False)]
    assert fill(broker, guard, book) == []
    assert not broker.positions and broker.cash == D('100')
    assert order.unfilled_reason == 'runtime_execution_check_invalid'
    assert guard.checks[0]['allowed'] is False


@pytest.mark.parametrize('allowed', [0, 1, 'true', 'false', None, [], {}])
def test_recorded_permission_requires_actual_boolean(allowed):
    broker, order = pending()
    book = quote()
    guard = BuyExecutionGuard(broker)
    guard.recorded = [permission(order, book, allowed=allowed)]
    assert guard.fill_blocker(order, book, book.received_at) == 'runtime_execution_check_invalid'
    assert guard.checks[0]['allowed'] is False


@pytest.mark.parametrize('reason', [None, 'unknown_reason', [], {}, 0, False])
def test_malformed_recorded_denial_reason_blocks_safely(reason):
    broker, order = pending()
    book = quote()
    guard = BuyExecutionGuard(broker)
    guard.recorded = [permission(order, book, allowed=False, reason=reason)]
    assert guard.fill_blocker(order, book, book.received_at) == 'runtime_execution_check_invalid'
    assert guard.checks[0]['allowed'] is False


@pytest.mark.parametrize('checked_at', [None, 'invalid-time', '2026-11-13T16:00:01', {}, [], 1, False])
def test_malformed_recorded_permission_time_blocks(checked_at):
    broker, order = pending()
    book = quote()
    guard = BuyExecutionGuard(broker)
    guard.recorded = [{**permission(order, book), 'checked_at':checked_at}]
    assert guard.fill_blocker(order, book, book.received_at) == 'runtime_execution_check_invalid'


@pytest.mark.parametrize('changed', [
    {'phase':'submit'}, {'order_id':'another-buy'}, {'symbol':'MSFT'},
    {'quote_at':NOW.isoformat()}, {'quote_received_at':NOW.isoformat()},
])
def test_permission_cannot_be_borrowed_from_other_order_or_quote(changed):
    broker, order = pending()
    book = quote()
    guard = BuyExecutionGuard(broker)
    guard.recorded = [{**permission(order, book), **changed}]
    assert fill(broker, guard, book) == []
    assert order.unfilled_reason == 'runtime_execution_check_mismatch'


def test_recorded_sequence_consumes_one_outcome_per_check_without_searching_ahead():
    broker, order = pending()
    book = quote()
    guard = BuyExecutionGuard(broker)
    guard.recorded = [{**permission(order, book), 'order_id':'another-buy'}, permission(order, book)]
    assert guard.fill_blocker(order, book, book.received_at) == 'runtime_execution_check_mismatch'
    assert guard.fill_blocker(order, book, book.received_at) is None
    assert guard.fill_blocker(order, book, book.received_at) == 'runtime_execution_check_missing'
    assert [row['allowed'] for row in guard.checks] == [False, True, False]


@pytest.mark.parametrize('recorded,reason', [([], 'runtime_execution_check_missing'),
    ({}, 'runtime_execution_check_invalid'), ([None], 'runtime_execution_check_missing'),
    (['bad-row'], 'runtime_execution_check_invalid')])
def test_missing_or_malformed_recorded_outcomes_never_fill(recorded, reason):
    broker, order = pending()
    book = quote()
    guard = BuyExecutionGuard(broker)
    guard.recorded = recorded
    assert fill(broker, guard, book) == []
    assert order.unfilled_reason == reason


def test_check_budget_is_bounded_and_resets_without_resetting_clock(monkeypatch):
    from sessionquant.portfolio import execution_guard
    monkeypatch.setattr(execution_guard, 'MAX_CHECKS', 2)
    broker, order = pending()
    book = quote()
    guard = BuyExecutionGuard(broker, lambda: book.received_at)
    assert guard.fill_blocker(order, book, book.received_at) is None
    assert guard.fill_blocker(order, book, book.received_at) is None
    for _ in range(3):
        assert guard.fill_blocker(order, book, book.received_at) == 'runtime_execution_check_limit'
    assert len(guard.checks) == 2 and guard.overflow_count == 3
    guard.begin_batch()
    assert guard.checks == [] and guard.overflow_count == 0
    assert guard.last_checked_at == book.received_at
    assert guard.fill_blocker(order, book, book.received_at) is None


def test_oversized_recording_is_not_partially_trusted(monkeypatch):
    from sessionquant.portfolio import execution_guard
    monkeypatch.setattr(execution_guard, 'MAX_CHECKS', 1)
    broker, order = pending()
    book = quote()
    guard = BuyExecutionGuard(broker)
    guard.recorded = [permission(order, book), permission(order, book)]
    assert fill(broker, guard, book) == []
    assert order.unfilled_reason == 'runtime_execution_check_invalid'


@pytest.mark.parametrize('elapsed,reason', [(-1, 'runtime_clock_rollback'),
    (11, 'runtime_quote_stale_or_invalid'), (3600, 'runtime_quote_stale_or_invalid')])
def test_runtime_clock_rollback_and_forward_jump_deny(elapsed, reason):
    broker, order = pending()
    book = quote()
    guard = BuyExecutionGuard(broker, lambda: book.received_at+timedelta(seconds=elapsed))
    assert fill(broker, guard, book) == []
    assert order.unfilled_reason == reason


def test_future_source_quote_is_invalid_at_execution_clock():
    broker, order = pending()
    guard = BuyExecutionGuard(broker, lambda: NOW)
    assert guard.fill_blocker(order, quote(), NOW) == 'runtime_quote_stale_or_invalid'


def test_clock_watermark_survives_batch_reset_and_restoration():
    broker, order = pending()
    book = quote()
    guard = BuyExecutionGuard(broker, lambda: NOW+timedelta(seconds=2))
    assert guard.fill_blocker(order, book, book.received_at) is None
    guard.begin_batch()
    guard.clock = lambda: NOW+timedelta(seconds=1)
    assert guard.fill_blocker(order, book, book.received_at) == 'runtime_clock_rollback'
    assert guard.last_checked_at == NOW+timedelta(seconds=2)
    restored = BuyExecutionGuard(broker, guard.clock, last_checked_at=guard.last_checked_at.isoformat())
    assert restored.fill_blocker(order, book, book.received_at) == 'runtime_clock_rollback'


@pytest.mark.parametrize('clock', [lambda: None, lambda: NOW.replace(tzinfo=None),
    lambda: (_ for _ in ()).throw(RuntimeError('clock unavailable'))])
def test_unavailable_runtime_clock_denies_without_raising(clock):
    broker, order = pending()
    book = quote()
    guard = BuyExecutionGuard(broker, clock)
    assert fill(broker, guard, book) == []
    assert order.unfilled_reason == 'runtime_clock_unavailable'
    assert guard.last_checked_at is None


def test_same_quote_checks_each_buy_order_at_its_own_execution_time():
    broker, first = pending()
    # Build a serialized execution-state edge case with two reservations. Public
    # submission normally admits one buy per symbol; each fill still needs a guard.
    second = deepcopy(first)
    second.intent = replace(first.intent, client_id='second-buy')
    broker.orders[second.client_id] = second
    book = quote()
    clock_values = iter([book.received_at, book.received_at+timedelta(seconds=11)])
    guard = BuyExecutionGuard(broker, lambda: next(clock_values))
    fills = fill(broker, guard, book)
    assert [item.client_id for item in fills] == [first.client_id]
    assert [row['order_id'] for row in guard.checks] == [first.client_id, second.client_id]
    assert [row['allowed'] for row in guard.checks] == [True, False]
    assert second.filled_qty == 0 and second.unfilled_reason == 'runtime_quote_stale_or_invalid'
    assert broker.reconcile()['ok']


@pytest.mark.parametrize('model', ['market_v2', 'legacy_limit_v1'])
def test_fresh_buy_is_checked_and_sell_does_not_call_buy_guard(model):
    broker, order = pending(model)
    book = quote()
    guard = BuyExecutionGuard(broker, lambda: book.received_at)
    assert [item.side for item in fill(broker, guard, book)] == ['buy']
    assert guard.checks[0]['order_id'] == order.client_id and guard.checks[0]['allowed']
    broker.submit_order(OrderIntent('sell', 'AAPL', 'sell', NOW+timedelta(seconds=2), D('99'),
                                   qty=broker.positions['AAPL'].qty))
    def forbidden(*args):
        pytest.fail('A sell must not invoke the runtime buy guard')
    fills = broker.process_quote(quote(3), NOW+timedelta(seconds=3), buy_fill_guard=forbidden)
    assert [item.side for item in fills] == ['sell'] and not broker.positions


def test_disabled_guard_preserves_legacy_recordings_without_checks():
    broker, order = pending()
    book = quote()
    guard = BuyExecutionGuard(broker)
    assert fill(broker, guard, book)
    assert guard.checks == [] and guard.last_checked_at is None
