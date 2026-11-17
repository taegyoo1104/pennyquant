"""시장가 근사와 기존 제한 가격 모델을 실제 주문 없이 비교한다."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from sessionquant.models import Quote
from sessionquant.portfolio import PaperBroker, PortfolioSettings, OrderIntent, serialize
from sessionquant.portfolio.execution_audit import comparison_record
from sessionquant.portfolio.fill_comparison import synthetic_gap_comparison

D = Decimal
NOW = datetime(2026,11,13,16,tzinfo=timezone.utc)


def quote(seconds, bid='99.9',ask='100', *, at=NOW, size=100, **kwargs):
    stamp = at+timedelta(seconds=seconds)
    return Quote('AAPL',stamp,stamp,float(bid),float(ask),size,size,**kwargs)


def intent(ident='buy', *, at=NOW, amount='20',symbol='AAPL', expected='100'):
    return OrderIntent(ident,symbol,'buy',at,D('100.1'),amount=D(amount),stop_price=D('98'),
                       expected_price=D(expected) if expected is not None else None)


def bought(settings=None):
    broker = PaperBroker(settings)
    broker.submit_order(intent())
    assert broker.process_quote(quote(1))
    return broker


@pytest.mark.parametrize('ask',['105','150','90'])
def test_market_amount_buy_has_no_internal_price_ceiling_even_after_gap(ask):
    broker = PaperBroker();order = broker.submit_order(intent())
    assert order.status=='submitted' and broker.reserved_cash==D('20.02')
    fill = broker.process_quote(quote(1,str(D(ask)-D('.1')),ask))[0]
    assert fill.price==D(ask)*D('1.001') and order.status=='filled'
    assert fill.qty*fill.price<=D('20') and fill.fee==D('.02')
    assert fill.expected_price==D('100') and fill.fill_model_version=='market_v2'
    assert fill.slippage_bps==(fill.price/D('100')-1)*10000
    assert fill.simulated_fill_price==fill.price and fill.assumed_latency==1 and fill.observed_latency==1
    assert order.fee_breakdown['total']==order.fee and broker.reconcile()['ok']
    if D(ask)>100:
        legacy = PaperBroker(PortfolioSettings(paper_fill_model='legacy_limit_v1'))
        legacy.submit_order(intent())
        assert legacy.process_quote(quote(1,str(D(ask)-D('.1')),ask))==[]
        assert legacy.cash==100 and legacy.get_order('buy').status=='open'


def test_after_fractional_deadline_marks_remaining_position_without_filling():
    broker=bought()
    cutoff=NOW.replace(hour=20)  # 15:00 ET: order window closed, regular market still open.
    qty=broker.positions['AAPL'].qty
    cash=broker.cash
    fills=len(broker.fills)
    assert not broker.process_quote(quote(0,'80','80.1',at=cutoff))
    assert broker.positions['AAPL'].mark_price==D('80')
    assert broker.equity==cash+qty*D('80')
    assert broker.halted and broker.cash==cash and len(broker.fills)==fills
    assert not broker.process_quote(quote(0,'90','90.1',at=cutoff))
    assert broker.positions['AAPL'].mark_price==D('80')  # Duplicate quote cannot revalue again.


def test_market_sell_down_gap_realizes_loss_instead_of_waiting_for_rebound():
    broker = bought();qty = broker.positions['AAPL'].qty
    order = broker.submit_order(OrderIntent('sell','AAPL','sell',NOW+timedelta(seconds=2),D('99.8'),
                                           qty=qty,expected_price=D('100')))
    fill = broker.process_quote(quote(3,'80','80.1'))[0]
    assert fill.price==D('79.920') and fill.price<order.intent.limit_price
    assert not broker.positions and fill.realized_pnl<0
    assert broker.realized_pnl==broker.cash-100
    assert fill.fee_breakdown['commission']+fill.fee_breakdown['regulatory']==fill.fee


def test_intent_time_submission_time_acceptance_and_quote_time_are_distinct():
    broker = PaperBroker(PortfolioSettings(paper_latency_seconds=D('2')))
    order = broker.submit_order(intent())
    assert broker.process_quote(quote(0))==[] and order.status=='submitted'
    broker.expire_orders(NOW+timedelta(seconds=2))
    assert order.status=='accepted' and order.accepted_at==NOW+timedelta(seconds=2)
    delayed = replace(quote(1),received_at=NOW+timedelta(seconds=2))
    assert broker.process_quote(delayed)==[]  # 접수 전에 생성된 가격을 나중에 받아도 사용하지 않는다.
    fill = broker.process_quote(quote(2))[0]
    assert fill.quote_timestamp>order.submitted_at and fill.quote_timestamp>=order.accepted_at
    assert [e['status'] for e in order.lifecycle[:2]]==['submitted','accepted']


def test_zero_latency_still_requires_strictly_later_quote():
    broker = PaperBroker(PortfolioSettings(paper_latency_seconds=D('0')))
    order = broker.submit_order(intent())
    assert broker.process_quote(quote(0))==[] and order.status=='accepted'
    assert broker.process_quote(quote(1))


def test_already_observed_quote_cannot_fill_late_submission_with_old_intent_timestamp():
    broker = PaperBroker()
    broker.process_quote(quote(2))
    order = broker.submit_order(intent())
    assert order.intent.created_at==NOW and order.submitted_at==NOW+timedelta(seconds=2)
    assert broker.process_quote(quote(2))==[]
    assert broker.process_quote(quote(3))


def test_duplicate_out_of_order_and_same_timestamp_changed_books_never_reuse_depth_after_restart():
    broker = PaperBroker();broker.submit_order(intent())
    broker.process_quote(quote(2),max_fill_qty=D('.05'))
    qty,fee = broker.positions['AAPL'].qty,broker.orders['buy'].fee
    broker = PaperBroker.from_state(json.loads(json.dumps(broker.to_state())))
    for tick in [quote(2),quote(2,'80','81'),replace(quote(1),received_at=NOW+timedelta(seconds=3))]:
        assert broker.process_quote(tick)==[]
    assert broker.positions['AAPL'].qty==qty and broker.orders['buy'].fee==fee
    assert len(broker.fills)==1 and broker.positions['AAPL'].mark_price==D('99.9')


def test_side_depth_is_shared_across_orders_and_participation_is_explicit():
    broker = bought(PortfolioSettings(paper_depth_participation=D('.001')))
    # 첫 매수 호가의 100주 중 가정 참여율 0.001 = 0.1주만 체결된다.
    broker.cancel_order('buy',NOW+timedelta(seconds=2))
    qty = broker.positions['AAPL'].qty
    assert qty==D('.1')
    for cid in ['s1','s2']:
        broker.submit_order(OrderIntent(cid,'AAPL','sell',NOW+timedelta(seconds=2),D('99'),qty=D('.05')))
    fills = broker.process_quote(quote(3,'101','101.1',size=60))
    assert sum(f.qty for f in fills)==D('.06')
    assert broker.get_order('s2').status=='partial' and broker.positions['AAPL'].qty==D('.04')
    assert broker.reconcile()['ok']


def test_partial_cancellation_releases_only_unfilled_reservation_and_cannot_fill_after_confirmation():
    broker = PaperBroker();order = broker.submit_order(intent())
    broker.process_quote(quote(1),max_fill_qty=D('.05'))
    qty = broker.positions['AAPL'].qty
    broker.cancel_order('buy',NOW+timedelta(seconds=2))
    assert order.status=='cancelled' and broker.reserved_cash==0
    assert broker.process_quote(quote(3))==[] and broker.positions['AAPL'].qty==qty
    assert order.lifecycle[-2]['status']=='cancel_pending' and order.lifecycle[-1]['status']=='cancelled'


def test_unconfirmed_cancellation_can_race_fill_but_confirmed_cancellation_cannot():
    broker = PaperBroker(PortfolioSettings(paper_cancel_latency_seconds=D('2')))
    order = broker.submit_order(intent())
    broker.process_quote(quote(1),max_fill_qty=D('.05'))
    broker.cancel_order('buy',NOW+timedelta(seconds=2))
    assert order.status=='cancel_pending' and broker.reserved_cash>0
    assert broker.process_quote(quote(3),max_fill_qty=D('.05'))
    assert order.status=='partial'  # 체결은 취소 확인 전이다. 취소 요청 시각은 보존한다.
    broker.expire_orders(NOW+timedelta(seconds=4))
    assert order.status=='cancelled' and broker.process_quote(quote(5))==[]
    assert broker.positions['AAPL'].qty==D('.1')
    assert broker.reconcile()['ok']


def test_unknown_result_keeps_reservation_and_never_auto_fills_cancels_or_retransmits():
    broker = PaperBroker();order = broker.submit_order(intent())
    broker.process_quote(quote(1),max_fill_qty=D('.05'))
    broker.mark_unknown('buy',NOW+timedelta(seconds=2))
    reserved = broker.reserved_cash
    broker.cancel_order('buy',NOW+timedelta(seconds=3))
    broker.expire_orders(NOW+timedelta(seconds=70))
    assert broker.process_quote(quote(71))==[] and order.status=='unknown'
    assert broker.reserved_cash==reserved and broker.positions['AAPL'].qty==D('.05')
    restored = PaperBroker.from_state(broker.to_state())
    another = restored.submit_order(intent('other',at=NOW+timedelta(seconds=72),symbol='MSFT'))
    assert another.status=='rejected' and another.rejection_reason=='account_order_state_unknown'
    assert len(restored.fills)==1 and restored.get_order('buy').status=='unknown'


def test_fee_reservation_and_fixed_amount_prevent_overspending_other_orders():
    broker = PaperBroker()
    rejected = broker.submit_order(intent(amount='100'))
    assert rejected.status=='rejected' and rejected.rejection_reason=='insufficient_cash'
    broker = PaperBroker()
    accepted = broker.submit_order(intent(amount='99.90'))
    assert accepted.status=='submitted' and broker.reserved_cash==100
    other = broker.submit_order(intent('other',amount='1',symbol='MSFT'))
    assert other.status=='rejected'
    broker.process_quote(quote(1,'149.9','150'))
    assert broker.cash>=0 and broker.reconcile()['ok']
    assert accepted.filled_notional<=D('99.90')


@pytest.mark.parametrize('amount,reason',[('0.50','below_minimum_order'),('1.001','amount_step_unsupported')])
def test_rejection_is_separate_from_submission_and_does_not_reserve_cash(amount,reason):
    broker = PaperBroker();order = broker.submit_order(intent(amount=amount))
    assert order.status=='rejected' and order.unfilled_reason==reason and broker.reserved_cash==0
    assert [e['status'] for e in order.lifecycle]==['submitted','rejected']


def test_sell_quantity_must_match_six_decimal_step_without_silent_rounding():
    broker = bought()
    order = broker.submit_order(OrderIntent('bad','AAPL','sell',NOW+timedelta(seconds=2),D('99'),qty=D('.0000001')))
    assert order.status=='rejected' and order.rejection_reason=='quantity_step_unsupported'


@pytest.mark.parametrize('case',['halted','stale','inverted','no_depth','untradable','future'])
def test_invalid_halted_stale_or_missing_depth_cannot_generate_optimistic_fills(case):
    broker = PaperBroker();order = broker.submit_order(intent())
    tick = quote(1)
    now = NOW+timedelta(seconds=1)
    if case=='halted':tick=replace(tick,halted=True)
    if case=='stale':now+=timedelta(seconds=11)
    if case=='inverted':object.__setattr__(tick,'ask',99)
    if case=='no_depth':tick=replace(tick,bid_size=0,ask_size=0)
    if case=='untradable':tick=replace(tick,tradable=False)
    if case=='future':tick=quote(2)
    assert broker.process_quote(tick,now)==[]
    assert not broker.positions and order.unfilled_reason and broker.cash==100


def test_early_close_holiday_and_fractional_deadline_are_not_simulated_as_successful_liquidation():
    early = datetime(2026,11,27,16,tzinfo=timezone.utc)
    blocked = PaperBroker();blocked.submit_order(intent(at=early))
    assert blocked.process_quote(quote(1,at=early))==[]  # 기존 시간표는 짧은 장의 신규 진입을 허용하지 않는다.
    broker = bought();broker.roll_session(early)  # 합성 이월 잔량도 청산 성공으로 만들지 않는다.
    cutoff = early.replace(hour=17)
    sell = broker.submit_order(OrderIntent('sell','AAPL','sell',cutoff-timedelta(seconds=1),D('90'),qty=broker.positions['AAPL'].qty))
    assert broker.process_quote(quote(0,at=cutoff))==[] and broker.positions
    assert sell.unfilled_reason=='outside_fractional_execution_window'
    holiday = datetime(2026,11,26,16,tzinfo=timezone.utc)
    rejected = PaperBroker().submit_order(intent(at=holiday))
    assert rejected.status=='rejected' and rejected.rejection_reason=='outside_fractional_order_window'


def test_roundtrip_preserves_model_financial_audit_and_lifecycle_without_float_conversion():
    broker = bought()
    encoded = json.loads(json.dumps(broker.to_state()))
    restored = PaperBroker.from_state(encoded)
    assert restored.to_state()==encoded and encoded['version']==2
    assert isinstance(restored.fills[0].slippage_bps,D) and isinstance(restored.orders['buy'].assumed_latency,D)
    assert restored.orders['buy'].simulated_fill_price==restored.fills[0].price
    assert restored.orders['buy'].fee_breakdown['total']==restored.fills[0].fee
    tampered = deepcopy(encoded);tampered['orders']['buy']['submitted_at']=(NOW-timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError):PaperBroker.from_state(tampered)


def test_original_v1_state_restores_legacy_without_relabelling_fills_or_reusing_for_market():
    old = bought(PortfolioSettings(paper_fill_model='legacy_limit_v1')).to_state()
    old['version']=1
    old['settings']={k:v for k,v in old['settings'].items() if not k.startswith('paper_')}
    old.pop('quote_sequences');old.pop('last_event_at')
    for row in old['orders'].values():
        row['intent'].pop('expected_price')
        for key in list(row):
            if key not in {'intent','status','filled_qty','filled_notional','fee','rejection_reason'}:row.pop(key)
    for row in old['fills']:
        for key in list(row):
            if key not in {'id','client_id','symbol','side','qty','price','fee','timestamp','realized_pnl'}:row.pop(key)
    source = deepcopy(old)
    restored = PaperBroker.from_state(old)
    assert old==source and restored.cash==D(old['cash'])
    assert restored.settings.paper_fill_model=='legacy_limit_v1'
    assert restored.fills[0].id==old['fills'][0]['id'] and restored.fills[0].expected_price is None
    with pytest.raises(ValueError,match='이전 가격제한 장부'):
        PaperBroker.from_state(old,PortfolioSettings())
    assert PaperBroker.from_state(old,PortfolioSettings(paper_fill_model='legacy_limit_v1')).reconcile()['ok']


@pytest.mark.parametrize('kwargs',[{'paper_fill_model':'unknown'},{'paper_latency_seconds':'-1'},
                                  {'paper_cancel_latency_seconds':'-1'},{'paper_depth_participation':'0'},
                                  {'paper_depth_participation':'1.01'},{'paper_latency_seconds':'Infinity'}])
def test_invalid_paper_assumptions_are_rejected(kwargs):
    with pytest.raises(ValueError):PortfolioSettings(**kwargs)


def test_synthetic_gap_comparison_quantifies_optimism_without_claiming_real_performance():
    report = synthetic_gap_comparison()
    assert report['synthetic'] and not report['live_orders_enabled']
    assert report['results']['market_v2']['filled_in_down_gap']
    assert not report['results']['legacy_limit_v1']['filled_in_down_gap']
    assert D(report['legacy_minus_market_net_pnl'])==D('3.9920040000')


def test_paper_and_broker_comparison_format_keeps_missing_actual_values_unknown():
    fill = serialize(bought().fills[0])
    paper = comparison_record(fill)
    assert paper['execution_mode']=='paper' and paper['simulated_fill_price']==fill['price'] and paper['actual_fill_price'] is None
    live = comparison_record({'execution_mode':'live','aggregation':'broker_cumulative','side':'buy','price':'101','fee':None,'tax':None})
    assert live['actual_fill_price']=='101' and live['simulated_fill_price'] is None
    assert live['fee_breakdown']['total'] is None and live['slippage_bps'] is None


def test_paper_broker_never_calls_real_broker_or_network(monkeypatch):
    import httpx
    def forbidden(*args,**kwargs):pytest.fail('모의 체결에서 네트워크·실주문을 호출했습니다.')
    monkeypatch.setattr(httpx.AsyncClient,'request',forbidden)
    monkeypatch.setattr(httpx.Client,'request',forbidden)
    broker = bought();assert broker.fills


def test_demo_report_exposes_model_comparison_net_cost_scope_and_unfilled_orders(tmp_path):
    from sessionquant.session_demo import write_session_demo
    result = write_session_demo(tmp_path/'new-demo')
    report = json.loads(__import__('pathlib').Path(result['report']).read_text())
    assert result['synthetic'] and not result['live_orders_enabled'] and result['fill_model_version']=='market_v2'
    assert report['report_version']==2 and report['synthetic_gap_execution_check']['synthetic']
    market,legacy = report['results']['pullback'],report['results']['pullback_legacy_limit_v1']
    assert market['fill_model_version']=='market_v2' and legacy['fill_model_version']=='legacy_limit_v1'
    assert market['cost_scope']=={'spread_slippage_and_fill_fees_included':True,'llm_cost_included':False,
                                  'infrastructure_cost_included':False,'future_exit_cost_included':False}
    assert market['fees_usd']!='0' and 'unfilled_reason_counts' in market
    assert '실제 수익률' in __import__('pathlib').Path(result['report']).with_suffix('.md').read_text()


def test_broker_clock_alone_does_not_force_financial_journal_writes_each_second(tmp_path):
    from sessionquant.database.repository import Repository
    from sessionquant.market.universe import UniverseStore
    from sessionquant.session_engine import SessionEngine
    repo = Repository(tmp_path/'clock.db',backend='sqlite')
    try:
        engine = SessionEngine(repo,UniverseStore(),synthetic=True)
        engine.tick(NOW)
        assert len(repo.all('account_snapshots'))==1
        engine.tick(NOW+timedelta(seconds=1))
        assert len(repo.all('account_snapshots'))==1
        engine.tick(NOW+timedelta(seconds=31))
        assert len(repo.all('account_snapshots'))==2
    finally:repo.close()


def test_day_expiry_cancels_residual_at_regular_close_even_with_long_assumed_timeout():
    broker = bought(PortfolioSettings(order_ttl_seconds=86400))
    order = broker.submit_order(OrderIntent('sell','AAPL','sell',NOW+timedelta(seconds=2),D('99'),qty=broker.positions['AAPL'].qty))
    close = NOW.replace(hour=21)
    broker.expire_orders(close)
    assert order.status=='cancelled' and order.unfilled_reason=='day_expired'
    assert broker.positions and not broker.get_open_orders()
