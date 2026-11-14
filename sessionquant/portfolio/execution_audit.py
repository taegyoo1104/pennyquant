"""모의 개별 체결과 실제 브로커 누적 응답을 비교 가능한 필드로 읽는다."""
from .models import decimal, serialize


def comparison_record(record):
    """없는 실측값·수수료·기준 가격을 모의 값으로 채우지 않는다."""
    mode = record.get('execution_mode','paper' if record.get('fill_model_version') else 'UNKNOWN')
    price = decimal(record['price'],positive=True) if record.get('price') is not None else None
    expected = decimal(record['expected_price'],positive=True) if record.get('expected_price') is not None else None
    fee = decimal(record['fee'],nonnegative=True) if record.get('fee') is not None else None
    tax = decimal(record['tax'],nonnegative=True) if record.get('tax') is not None else None
    is_live = mode == 'live'
    total = fee+tax if is_live and fee is not None and tax is not None else None if is_live else fee
    bps = ((price/expected-1)*10000*(1 if record['side']=='buy' else -1)) if price and expected else None
    return serialize({'client_id':record.get('client_id'),'symbol':record.get('symbol'),'side':record.get('side'),
        'execution_mode':mode,'aggregation':record.get('aggregation','individual_fill'),
        'fill_model_version':record.get('fill_model_version'), 'expected_price':expected,
        'simulated_fill_price':price if mode=='paper' else None,'actual_fill_price':price if is_live else None,
        'assumed_latency':record.get('assumed_latency') if mode=='paper' else None,
        'slippage_bps':bps,'fee_breakdown':record.get('fee_breakdown') if mode=='paper' else
            {'commission':fee,'tax':tax,'total':total,'regulatory':None},
        'fee_is_estimate':mode=='paper','timestamp':record.get('timestamp')})
