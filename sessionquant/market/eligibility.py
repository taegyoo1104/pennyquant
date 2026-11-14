"""공식 필드가 확인해 주는 사실과 계좌별 미확인 주문 자격을 분리한다."""
from datetime import timedelta

from ..models import utc
from .scanner import EXCHANGES, InstrumentEligibility


def field_verified(item, name, expected, now):
    """출처·유효 시각·값·계좌 범위가 모두 맞는 근거만 진입에 사용한다."""
    row = item.field_evidence.get(name, {})
    try:
        return bool(row.get('status') == 'VERIFIED' and row.get('value') is expected
                    and isinstance(row.get('source'),str) and row['source'].strip().upper() not in {'','UNKNOWN'}
                    and utc(row['received_at']) <= now <= utc(row['valid_until'])
                    and utc(row['as_of_at']) <= utc(row['received_at'])
                    and (name not in {'tradable', 'fractional'} or
                         (item.account_scope != 'UNKNOWN' and row.get('account_scope') == item.account_scope)))
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def toss_eligibility(symbol, stock, received_at, *, ttl_seconds=86400):
    """ACTIVE·빈 유의사항·조회 성공을 소수점/계좌 거래 가능으로 승격하지 않는다."""
    stamp = utc(received_at)
    stock = stock if isinstance(stock, dict) and stock.get('symbol') == symbol else {}
    exchange = stock.get('market') if stock.get('currency') == 'USD' and stock.get('market') in EXCHANGES else 'UNKNOWN'
    expires = stamp+timedelta(seconds=ttl_seconds)
    evidence = {name: {'status':'UNKNOWN', 'value':None, 'source':'toss:StockInfo:v1.2.28',
                'as_of_at':None, 'received_at':stamp.isoformat(), 'valid_until':expires.isoformat(),
                'account_scope':'UNKNOWN', 'reason':'공식 응답에 미국 계좌별 자격·정지·기업행동 증명 필드 없음'}
                for name in ('tradable','fractional','halted','corporate_action')}
    evidence['exchange'] = {'status':'VERIFIED' if exchange != 'UNKNOWN' else 'UNKNOWN', 'value':exchange,
        'source':'toss:StockInfo.market', 'as_of_at':None, 'received_at':stamp.isoformat(),
        'valid_until':expires.isoformat(), 'scope':'listing_reference', 'reason':'원본 갱신 시각은 미제공'}
    evidence['listing_status'] = {'status':'VERIFIED' if stock.get('status') in {'ACTIVE','DELISTED','SCHEDULED'} else 'UNKNOWN',
        'value':stock.get('status') if stock.get('status') in {'ACTIVE','DELISTED','SCHEDULED'} else None,
        'source':'toss:StockInfo.status','as_of_at':None,'received_at':stamp.isoformat(),
        'valid_until':expires.isoformat(),'scope':'listing_reference','reason':'상장 상태만 증명하며 계좌 주문 자격과 구분'}
    # 상장 폐지·예정은 제한 근거다. ACTIVE는 주문 허가 근거가 아니다.
    restricted = stock.get('status') in {'DELISTED','SCHEDULED'}
    return InstrumentEligibility(symbol, stamp, 'toss:StockInfo:v1.2.28', tradable=False if restricted else None,
        exchange=exchange, valid_until=expires, account_scope='UNKNOWN', field_evidence=evidence)
