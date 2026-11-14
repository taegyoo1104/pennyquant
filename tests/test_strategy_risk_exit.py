"""위험·예약·취소 확인·청산 완료가 같은 규칙을 사용하는지 합성 자료로 검증한다."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json

import pytest

from sessionquant.database.repository import Repository
from sessionquant.market.universe import UniverseStore
from sessionquant.models import jsonable
from sessionquant.portfolio import PaperBroker,PortfolioManager,PortfolioSettings,OrderIntent,SessionExitManager
from sessionquant.portfolio.exit_policy import ExitPolicy
from sessionquant.session_engine import SessionEngine,event_time
from sessionquant.session_demo import synthetic_tape
from sessionquant.session_runtime import replay_session
from test_paper_market_fills import NOW,quote,intent,bought

D=Decimal


def plan(manager, symbol='AAPL', now=NOW, **changes):
    kwargs={'market_state':'BULLISH','tradable':True,'fractional_supported':True,'trend_confirmed':True,
            'bid':'99.9','strategy':'pullback'}
    kwargs.update(changes)
    ask=kwargs.pop('ask','100');stop=kwargs.pop('stop','98')
    return manager.plan_entry(symbol,ask,stop,now,**kwargs)


def test_add_plan_never_uses_price_decline_as_rebound_and_records_combined_costs():
    broker=bought();manager=PortfolioManager(broker)
    common={'add':True,'support_confirmed':True,'rebound_confirmed':True}
    assert plan(manager,now=NOW+timedelta(seconds=2),**common).reason=='add_would_average_down'
    decision=plan(manager,now=NOW+timedelta(seconds=2),ask='101',bid='100.9',stop='97',**common)
    assert decision.approved
    audit=decision.audit['projection']
    assert D(audit['old_qty'])==broker.positions['AAPL'].qty
    assert D(audit['combined_qty'])==D(audit['old_qty'])+D(audit['planned_qty'])
    assert D(audit['stop_loss_estimate'])<=D(audit['risk_budget'])
    assert decision.intent.stop_price==D('98') and D(audit['entry_fee'])>=0 and D(audit['exit_fee_estimate'])>0
    broker.submit_order(decision.intent)
    broker.process_quote(quote(3,'100.9','101'))
    assert broker.positions['AAPL'].adds==1 and broker.positions['AAPL'].avg_price>D('100.1')
    assert plan(manager,now=NOW+timedelta(seconds=4),ask='102',bid='101.9',**common).reason=='add_conditions_not_met'


@pytest.mark.parametrize('missing',['support_confirmed','rebound_confirmed','trend_confirmed'])
def test_add_requires_all_independent_confirmations(missing):
    broker=bought();manager=PortfolioManager(broker)
    kwargs={'add':True,'support_confirmed':True,'rebound_confirmed':True,'trend_confirmed':True}
    kwargs[missing]=False
    assert not plan(manager,now=NOW+timedelta(seconds=2),ask='101',bid='100.9',**kwargs).approved


def test_reserved_cash_fees_and_future_mark_loss_restrict_normal_multi_position_exposure():
    broker=PaperBroker();manager=PortfolioManager(broker)
    for index in range(4):
        decision=plan(manager,symbol=f'S{index}')
        assert decision.approved
        broker.submit_order(decision.intent)
    assert broker.reserved_cash<=100 and broker.reserved_cash>sum(o.intent.amount for o in broker.get_open_orders())
    assert not plan(manager,symbol='S4').approved
    for index in range(4):broker.process_quote(replace(quote(1),symbol=f'S{index}'))
    snapshot=manager.risk_snapshot(NOW+timedelta(seconds=1))
    assert snapshot['issues']==[] and D(snapshot['gross_value'])<=D(snapshot['equity'])*D('.8')
    assert all(D(v['loss_estimate'])<=D(v['risk_budget']) for v in snapshot['positions'].values())


def test_two_preferred_positions_are_not_a_requirement_and_five_is_a_hard_count_cap():
    broker=PaperBroker(PortfolioSettings(initial_weight=D('.1'),add_weight=D('.1')))
    manager=PortfolioManager(broker)
    assert broker.positions=={}
    for i in range(5):
        now=NOW+timedelta(seconds=2*i)
        decision=plan(manager,symbol=f'S{i}',now=now)
        assert decision.approved
        broker.submit_order(decision.intent)
        broker.process_quote(replace(quote(2*i+1),symbol=f'S{i}'))
        if i==1:assert len(broker.positions)==2
    assert len(broker.positions)==5
    assert plan(manager,symbol='S5',now=NOW+timedelta(seconds=12)).reason=='position_limit'
    assert manager.risk_snapshot(NOW+timedelta(seconds=12))['issues']==[]


def test_missing_old_pending_reference_bid_remains_unknown_instead_of_made_up():
    broker=PaperBroker();broker.submit_order(intent(symbol='MSFT'))
    assert plan(PortfolioManager(broker)).reason=='pending_mark_reference_unknown'


def test_add_market_gap_is_recorded_and_warned_instead_of_hiding_filled_quantity():
    broker=bought();manager=PortfolioManager(broker)
    decision=plan(manager,now=NOW+timedelta(seconds=2),ask='101',bid='100.9',add=True,support_confirmed=True,rebound_confirmed=True)
    assert decision.approved
    old_avg=broker.positions['AAPL'].avg_price
    broker.submit_order(decision.intent)
    assert broker.process_quote(quote(3,'98.9','99'))
    assert broker.positions['AAPL'].avg_price<old_avg and broker.positions['AAPL'].stop_price==98
    assert any(a['code']=='add_execution_below_average' for a in broker.alerts)


def test_gap_breach_is_an_alert_and_daily_two_percent_is_not_a_loss_guarantee():
    broker=bought();broker.process_quote(quote(2,'50','50.1'))
    assert broker.halted and broker.equity<98
    manager=PortfolioManager(broker);snapshot=manager.risk_snapshot(NOW+timedelta(seconds=2))
    assert D(snapshot['positions']['AAPL']['loss_estimate'])>D('2')
    assert any('symbol_risk_budget_exceeded' in issue for issue in snapshot['issues'])
    assert not plan(manager,symbol='MSFT',now=NOW+timedelta(seconds=2)).approved
    manager.check_exits(quote(2,'50','50.1'),NOW+timedelta(seconds=2))
    broker.process_quote(quote(3,'50','50.1'))
    assert not broker.positions and broker.realized_pnl < -2


def test_exit_waits_for_buy_cancellation_then_sells_requeried_partial_quantity_once():
    broker=bought(PortfolioSettings(paper_cancel_latency_seconds=D('2')))
    manager=PortfolioManager(broker)
    add=plan(manager,now=NOW+timedelta(seconds=2),ask='101',bid='100.9',add=True,support_confirmed=True,rebound_confirmed=True)
    broker.submit_order(add.intent)
    manager.check_exits(quote(3,'103','103.1'),NOW+timedelta(seconds=3),bearish_signal=True)
    assert broker.get_order(add.intent.client_id).status=='cancel_pending'
    assert not [o for o in broker.orders.values() if o.intent.side=='sell']
    broker.process_quote(quote(4,'101.9','102'),max_fill_qty=D('.01'))
    quantity=broker.positions['AAPL'].qty
    assert quantity>D('.199')
    manager.check_exits(quote(5,'103','103.1'),NOW+timedelta(seconds=5))
    sells=[o for o in broker.orders.values() if o.intent.side=='sell']
    assert len(sells)==1 and sells[0].intent.qty==quantity
    manager.check_exits(quote(5,'103','103.1'),NOW+timedelta(seconds=5))
    assert len([o for o in broker.orders.values() if o.intent.side=='sell'])==1
    broker.process_quote(quote(6,'103','103.1'))
    assert SessionExitManager(broker).snapshot(NOW+timedelta(seconds=6))['liquidation_complete']


def test_zero_positions_with_unknown_buy_is_not_reported_liquidation_complete():
    broker=PaperBroker();broker.submit_order(intent());broker.mark_unknown('buy',NOW)
    manager=SessionExitManager(broker)
    now=NOW.replace(hour=19,minute=50)
    status=manager.tick(now,{})
    assert status['positions_remaining']=={} and not status['liquidation_complete']
    assert status['unknown_orders']==['buy'] and status['pending_buy_cancellations']==['buy']
    assert any(a['code']=='entry_cancel_unconfirmed' for a in status['alerts'])


def test_time_exit_latches_without_quote_and_profit_protection_can_be_compared():
    broker=bought();manager=PortfolioManager(broker,ExitPolicy(max_holding_minutes=1))
    now=NOW+timedelta(seconds=61)
    assert manager.check_exits(None,now,symbol='AAPL')==[]
    assert broker.exit_intents['AAPL']['reason']=='holding_time_exit'
    assert manager.check_exits(quote(62),NOW+timedelta(seconds=62))
    broker.process_quote(quote(63))
    assert not broker.positions
    for enabled in [True,False]:
        sample=bought();m=PortfolioManager(sample,ExitPolicy(profit_protection=enabled))
        orders=m.check_exits(quote(2,'103','103.1'),NOW+timedelta(seconds=2),bearish_signal=True)
        assert bool(orders)==enabled
        assert sample.settings.target_return==D('.10')


def test_news_lookup_error_cannot_stop_price_stop_loss_or_session_cleanup(tmp_path):
    class Broken:
        def risk_context(self,*args,**kwargs):raise RuntimeError('PRIVATE_SECRET')
    rows,opened=synthetic_tape();universe=UniverseStore();universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=opened)
    repo=Repository(tmp_path/'news-fault.db',backend='sqlite')
    try:
        engine=SessionEngine(repo,universe,synthetic=True,risk_provider=Broken())
        for now in sorted({event_time(r) for r in rows if event_time(r)<opened+timedelta(hours=2)}):
            engine.apply_batch([r for r in rows if event_time(r)==now],now)
        assert engine.broker.positions and not engine.failed
        at=opened+timedelta(minutes=76)
        for i in [0,1]:
            q=replace(quote(0,'95','95.1',at=at),timestamp=at+timedelta(seconds=i),received_at=at+timedelta(seconds=i))
            engine.apply_batch([{'kind':'quote','data':jsonable(q)}],q.received_at)
        assert not engine.broker.positions and engine.repo.get_control('sessionquant_context')['session']['liquidation_complete']
        assert engine.repo.get_control('sessionquant_context')['news_status']=='DEGRADED'
        assert 'PRIVATE_SECRET' not in str(repo.all('system_logs'))
        assert any(o.intent.reason=='stop_loss' for o in engine.broker.orders.values())
    finally:repo.close()


def test_provider_fault_retains_confirmed_entry_restriction_but_never_invents_adverse_exit(tmp_path):
    class Provider:
        failing=False
        def risk_context(self,*args,**kwargs):
            if self.failing:raise ValueError('PRIVATE')
            return {'status':'AVAILABLE','block_entries':True,'blackout':False,'major_adverse':False}
    repo=Repository(tmp_path/'cache.db',backend='sqlite');provider=Provider()
    try:
        engine=SessionEngine(repo,UniverseStore(),risk_provider=provider)
        assert engine.risk_context(NOW,'AAPL')['block_entries']
        provider.failing=True
        risk=engine.risk_context(NOW+timedelta(seconds=1),'AAPL')
        assert risk['block_entries'] and risk['major_adverse'] is False
        assert not engine.risk_context(NOW-timedelta(seconds=1),'AAPL')['block_entries']
    finally:repo.close()


def test_same_closed_signal_cannot_create_second_order_after_restart_or_bar_receipt(tmp_path):
    rows,opened=synthetic_tape();universe=UniverseStore();universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=opened)
    repo=Repository(tmp_path/'dedup.db',backend='sqlite')
    try:
        engine=SessionEngine(repo,universe,synthetic=True)
        subset=[r for r in rows if event_time(r)<opened+timedelta(hours=2)]
        for now in sorted({event_time(r) for r in subset}):engine.apply_batch([r for r in subset if event_time(r)==now],now)
        before=len(engine.broker.orders)
        restored=SessionEngine(repo,universe,synthetic=True,restore=True)
        now=opened+timedelta(minutes=75,seconds=3)
        duplicate=json.loads(json.dumps(next(r for r in reversed(subset) if r['kind']=='bar' and r['data']['symbol']=='AAPL')))
        duplicate['data']['received_at']=now.isoformat()
        q=replace(quote(0,'101.35','101.37',at=now),timestamp=now,received_at=now)
        restored.apply_batch([duplicate,{'kind':'quote','data':jsonable(q)}],now)
        assert len(restored.broker.orders)==before
    finally:repo.close()
