"""소수점 체결의 정확성, 위험 한도와 세션 청산의 회귀 검증."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from sessionquant.models import Quote
from sessionquant.portfolio import (PaperBroker, PortfolioManager, PortfolioSettings, OrderIntent,
                                    SessionExitManager, serialize)

D = Decimal
NOW = datetime(2026, 11, 13, 16, 0, tzinfo=timezone.utc)


def quote(now=NOW, bid='99.9', ask='100', symbol='AAPL', **kw):
    return Quote(symbol, now, now, float(bid), float(ask), 100, 100, **kw)


def buy(broker, *, symbol='AAPL', amount='20', stop='98', now=NOW, price='101', is_add=False, ident='buy'):
    intent = OrderIntent(ident, symbol, 'buy', now, D(price), amount=D(amount), stop_price=D(stop), is_add=is_add)
    return broker.submit_order(intent)


def filled(broker=None):
    broker = broker or PaperBroker()
    buy(broker)
    broker.process_quote(quote(NOW + timedelta(seconds=1)))
    return broker


def plan(broker, **kw):
    args = dict(market_state='BULLISH', tradable=True, fractional_supported=True, trend_confirmed=True)
    args.update(kw)
    return PortfolioManager(broker).plan_entry('AAPL', D('100'), D('98'), NOW, **args)


def test_allocation_uses_equity_fraction_and_decimal_cash():
    b = PaperBroker()
    decision = plan(b)
    assert decision.approved and decision.intent.amount == D('20')
    b.submit_order(decision.intent)
    assert b.reserved_cash == D('20.02') and b.get_available_cash() == D('79.98')
    fill = b.process_quote(quote(NOW + timedelta(seconds=1)))[0]
    assert isinstance(fill.qty, Decimal) and fill.qty == D('0.199800')
    assert b.cash == D('80.000020000') - D('0.02')
    assert b.positions['AAPL'].qty == fill.qty
    assert b.reconcile()['ok']


def test_fees_applied_to_cumulative_order_not_each_partial_fill():
    b = PaperBroker()
    buy(b)
    first = b.process_quote(quote(NOW + timedelta(seconds=1)), max_fill_qty=D('0.05'))
    assert first[0].fee == 0 and b.get_order('buy').status == 'partial'
    b.process_quote(quote(NOW + timedelta(seconds=2)), max_fill_qty=D('0.05'))
    b.process_quote(quote(NOW + timedelta(seconds=3)))
    assert b.get_order('buy').fee == D('.02')
    assert sum(f.fee for f in b.fills) == b.get_order('buy').fee
    assert b.reserved_cash == 0


def test_duplicate_quote_and_restart_do_not_reuse_liquidity():
    b = PaperBroker()
    buy(b)
    q = quote(NOW + timedelta(seconds=1))
    assert len(b.process_quote(q, max_fill_qty=D('.05'))) == 1
    assert b.process_quote(q, max_fill_qty=D('.05')) == []
    b = PaperBroker.from_state(json.loads(json.dumps(b.to_state())))
    assert b.process_quote(q) == []
    assert len(b.process_quote(quote(NOW + timedelta(seconds=2)))) == 1
    assert len(b.fills) == 2


def test_quote_before_order_or_stale_future_halt_never_fills():
    for q in (quote(NOW - timedelta(seconds=1)), quote(NOW + timedelta(seconds=2)),
              quote(NOW, halted=True), quote(NOW, tradable=False)):
        b = PaperBroker()
        buy(b)
        assert b.process_quote(q, NOW) == []
    b = PaperBroker(); buy(b)
    assert b.process_quote(quote(), NOW + timedelta(seconds=11)) == []


def test_idempotency_rejects_same_id_with_changed_intent():
    b = PaperBroker()
    order = buy(b)
    assert buy(b) is order
    with pytest.raises(ValueError):
        buy(b, amount='10')
    assert len(b.orders) == 1


def test_restore_requires_identical_risk_configuration():
    b = filled()
    state = json.loads(json.dumps(b.to_state()))
    recovered = PaperBroker.from_state(state, b.settings)
    assert recovered.to_state() == state
    with pytest.raises(ValueError):
        PaperBroker.from_state(state, replace(b.settings, daily_loss_limit=D('.03')))


@pytest.mark.parametrize('settings', [dict(initial_cash='NaN'), dict(risk_per_symbol='Infinity'),
                                       dict(max_positions=6), dict(allow_adds='yes'), dict(max_adds=2)])
def test_invalid_financial_settings_fail_closed(settings):
    with pytest.raises(ValueError):
        PortfolioSettings(**settings)


def test_missing_eligibility_and_market_unknown_block_entries():
    b = PaperBroker()
    assert not plan(b, fractional_supported=False).approved
    assert not plan(b, market_state='UNKNOWN').approved
    assert not plan(b, market_state='BEARISH').approved
    assert not plan(b, market_state='NEUTRAL').approved
    assert not plan(b, major_bad_news=True).approved
    assert not plan(b, trend_confirmed=False).approved


def test_risk_budget_caps_wide_stop_trade_including_costs():
    b = PaperBroker()
    result = PortfolioManager(b).plan_entry('AAPL', D('100'), D('90'), NOW,
        market_state='BULLISH', tradable=True, fractional_supported=True, trend_confirmed=True)
    assert result.approved and result.intent.amount < D('7.5')
    amount = result.intent.amount
    qty = amount / result.intent.limit_price
    proceeds = qty * D('90') * (1-b.settings.slippage_rate)
    loss = amount+b.settings.fees('buy', amount)-proceeds+b.settings.fees('sell', proceeds)
    assert loss <= D('.75')


def test_single_add_requires_support_rebound_and_does_not_lower_stop():
    b = filled()
    m = PortfolioManager(b)
    common = dict(market_state='BULLISH', tradable=True, fractional_supported=True, trend_confirmed=True, add=True)
    assert not m.plan_entry('AAPL', '101', '97', NOW+timedelta(seconds=2), **common).approved
    d = m.plan_entry('AAPL', '101', '97', NOW+timedelta(seconds=2), support_confirmed=True, rebound_confirmed=True, **common)
    assert d.approved and d.intent.stop_price == D('98')
    b.submit_order(d.intent)
    b.process_quote(quote(NOW+timedelta(seconds=3), '100.9','101'))
    assert b.positions['AAPL'].adds == 1 and b.positions['AAPL'].stop_price == D('98')
    assert not m.plan_entry('AAPL', '101', '98', NOW+timedelta(seconds=4), support_confirmed=True, rebound_confirmed=True, **common).approved


def test_daily_loss_halts_latches_until_next_session_and_cancels_buys():
    b = filled()
    buy(b, symbol='MSFT', ident='msft')
    b.process_quote(quote(NOW+timedelta(seconds=2), '85','85.1'))
    assert b.halted and b.get_order('msft').status == 'cancelled'
    assert any(a['code']=='daily_loss_limit' for a in b.alerts)
    b.process_quote(quote(NOW+timedelta(seconds=3), '100','100.1'))
    assert b.halted
    b = PaperBroker.from_state(b.to_state())
    assert b.halted
    b.roll_session(NOW+timedelta(days=3))
    assert not b.halted


def test_early_profit_exit_is_latched_and_retries_after_price_drop():
    b = filled()
    m = PortfolioManager(b)
    q = quote(NOW+timedelta(seconds=2), '103','103.1')
    exits = m.check_exits(q,q.received_at,bearish_signal=True)
    assert exits and exits[0].intent.reason == 'profit_protection'
    assert b.positions['AAPL'].qty > 0
    later = NOW+timedelta(seconds=70)
    q2=quote(later,'97','97.1')
    restored=PaperBroker.from_state(b.to_state())
    result=PortfolioManager(restored).check_exits(q2,later)
    assert result and result[0].intent.reason=='profit_protection'
    assert restored.get_order(exits[0].client_id).status=='cancelled'
    restored.process_quote(q2,later)
    assert restored.positions  # 시장가도 제출 때 관측한 동일 호가로 체결하지 않는다.
    restored.process_quote(quote(later+timedelta(seconds=1),'97','97.1'))
    assert not restored.positions


def test_partial_sell_reserves_quantity_and_net_pnl_includes_both_fees():
    b=filled(); p=b.positions['AAPL']; qty=p.qty
    o=b.submit_order(OrderIntent('sell','AAPL','sell',NOW+timedelta(seconds=2),D('102'),qty=qty))
    duplicate=b.submit_order(OrderIntent('sell2','AAPL','sell',NOW+timedelta(seconds=2),D('102'),qty=qty))
    assert duplicate.status=='rejected'
    b.process_quote(quote(NOW+timedelta(seconds=3),'103','103.1'),max_fill_qty=D('.1'))
    assert o.status=='partial' and b.positions['AAPL'].qty==qty-D('.1')
    b.process_quote(quote(NOW+timedelta(seconds=4),'103','103.1'))
    assert o.status=='filled' and not b.positions
    assert b.realized_pnl==b.cash-D('100')


def test_session_deadlines_holidays_dst_and_early_close():
    e=SessionExitManager(PaperBroker())
    s=e.schedule(NOW)
    assert s.entry_start.hour==15 and s.entry_start.minute==45
    assert s.exit_start.hour==19 and s.exit_start.minute==40
    assert e.permits_entry(NOW)
    assert not e.permits_entry(s.entry_end)
    early=e.schedule(datetime(2026,11,27,16,tzinfo=timezone.utc))
    assert early.closes_at.hour==18 and early.fractional_deadline.hour==17
    assert early.exit_start.hour==16 and early.exit_start.minute==40
    assert not e.permits_entry(datetime(2026,11,27,16,tzinfo=timezone.utc))
    assert e.schedule(datetime(2026,7,3,15,tzinfo=timezone.utc)) is None
    summer=e.schedule(datetime(2026,10,30,16,tzinfo=timezone.utc))
    assert summer.entry_start.hour==14 and summer.exit_start.hour==18


def test_session_exit_submission_is_not_completion_and_residual_warns():
    b=filled(); e=SessionExitManager(b)
    now=datetime(2026,11,13,19,40,tzinfo=timezone.utc)
    result=e.tick(now,{'AAPL':quote(now)})
    assert result['orders'] and not result['liquidation_complete']
    assert b.positions
    assert e.tick(now,{'AAPL':quote(now)})['orders']==[]
    now+=timedelta(minutes=10)
    result=e.tick(now,{})
    assert any(a['code']=='session_exit_incomplete' for a in result['alerts'])
    assert not result['liquidation_complete']
    q=quote(now)
    e.tick(now,{'AAPL':q})
    b.process_quote(q,now)
    assert not e.tick(now,{'AAPL':q})['liquidation_complete']
    now+=timedelta(seconds=1);q=quote(now)
    b.process_quote(q,now)
    assert e.tick(now,{'AAPL':q})['liquidation_complete']


def test_cutoff_prevents_pending_buy_fill_and_overnight_alert_survives_restart():
    b=filled()
    now=datetime(2026,11,13,18,29,59,tzinfo=timezone.utc)
    buy(b,symbol='MSFT',ident='late',now=now)
    e=SessionExitManager(b); e.tick(now+timedelta(seconds=1),{})
    assert b.get_order('late').status=='cancelled'
    restored=PaperBroker.from_state(b.to_state())
    next_day=datetime(2026,11,16,15,0,tzinfo=timezone.utc)
    result=SessionExitManager(restored).tick(next_day,{'AAPL':quote(next_day)})
    assert result['orders'] and any(a['code']=='overnight_position' for a in result['alerts'])

@pytest.mark.parametrize('hour,minute,buffer',[(18,30,30),(17,40,120)])
def test_buy_pending_across_entry_cutoff_is_cancelled_instead_of_filled(hour,minute,buffer):
    broker=PaperBroker(PortfolioSettings(min_entry_to_exit_minutes=buffer))
    cutoff=NOW.replace(hour=hour,minute=minute)
    order=buy(broker,now=cutoff-timedelta(seconds=1))
    assert broker.process_quote(quote(cutoff))==[]
    assert order.status=='cancelled' and not broker.positions
    assert broker.cash==D('100') and broker.reserved_cash==0
