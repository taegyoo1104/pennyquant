"""동일 관측열에서 불리한 갭을 피하는 편향을 합성 자료로 비교한다."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ..models import Quote
from .broker import PaperBroker
from .models import OrderIntent, PortfolioSettings, serialize


def synthetic_gap_comparison():
    """성과·예측이 아닌 가격제한으로 인한 체결 차이를 확인하는 고정 시나리오."""
    at = datetime(2026,11,13,16,tzinfo=timezone.utc)
    results = {}
    for model in ('market_v2','legacy_limit_v1'):
        broker = PaperBroker(PortfolioSettings(paper_fill_model=model))
        broker.submit_order(OrderIntent('fixture-buy','TEST','buy',at,Decimal('101'),amount=Decimal('20'),
                                       stop_price=Decimal('90'),expected_price=Decimal('100')))
        def tick(seconds,bid,ask):
            now = at+timedelta(seconds=seconds)
            return broker.process_quote(Quote('TEST',now,now,float(bid),float(ask),100,100))
        tick(1,'99.9','100')
        broker.submit_order(OrderIntent('fixture-sell','TEST','sell',at+timedelta(seconds=2),Decimal('99.9'),
                                       qty=broker.positions['TEST'].qty,expected_price=Decimal('100')))
        gap = tick(3,'80','80.1')
        tick(4,'100','100.1')
        results[model] = {'filled_in_down_gap':bool(gap),'net_pnl':broker.equity-Decimal('100'),
            'final_equity':broker.equity,'fees':sum((f.fee for f in broker.fills),Decimal(0)),
            'unfilled_orders':len(broker.get_open_orders()),'open_positions':len(broker.positions)}
    return serialize({'synthetic':True,'live_orders_enabled':False,'purpose':'gap_execution_bias_check',
        'note':'합성 동작 검증입니다. 실제 수익률·미래 성과·증권사 매칭 재현이 아닙니다.',
        'results':results,'legacy_minus_market_net_pnl':results['legacy_limit_v1']['net_pnl']-results['market_v2']['net_pnl']})
