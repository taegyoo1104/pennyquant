"""시장 참조 ETF의 정의·권한·원본 시각을 거래 적격성과 분리한다."""
from datetime import timedelta
from hashlib import sha256
import json
import re

from ..models import utc
from ..portfolio.models import decimal, serialize

REFERENCES = {
    'SHY':('SHORT_TREASURY','미국 1~3년 국채 ETF'),
    'IEF':('INTERMEDIATE_TREASURY','미국 7~10년 국채 ETF'),
    'TLT':('LONG_TREASURY','미국 20년 초과 국채 ETF'),
    'USO':('OIL_FUTURES_PROXY','원유 선물 노출 ETF·WTI 현물 아님'),
    'SPY':('INDEX','S&P 500 ETF'), 'QQQ':('INDEX','나스닥 100 ETF'),
    'SOXX':('SEMICONDUCTOR_ETF','미국 반도체 ETF·SOX 지수 아님'),
}
# Optional display ETFs must not expand the legacy risk filter or default reads.
DEFAULT_REFERENCE_SYMBOLS = ('SHY', 'IEF', 'TLT', 'USO', 'SPY', 'QQQ')
REFERENCE_SYMBOLS = frozenset(REFERENCES)
SOURCE = 'toss:market_reference:v1.2.28'


def identity(value):
    return sha256(json.dumps(serialize(value),sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def permission_status(proof,symbol,now):
    """조회 성공으로 실시간 권한을 추측하지 않는다. 운영 기본 증거는 미등록이다."""
    try:
        valid = (proof.get('verified') is True and proof.get('source') and proof.get('evidence_url')
                 and proof.get('scope') == 'MARKET_REFERENCE' and symbol in proof.get('symbols',[])
                 and utc(proof['checked_at']) <= now <= utc(proof['valid_until']))
        classification = proof.get('latency_class','UNKNOWN')
        if not valid or classification not in {'REALTIME','DELAYED'}:
            return 'UNKNOWN'
        if classification == 'REALTIME' and proof.get('realtime_entitled') is not True:
            return 'UNKNOWN'
        return classification
    except (ValueError,TypeError,KeyError,AttributeError):
        return 'UNKNOWN'


def observation(symbol,stream,values,event_at,received_at,ingested_at,*,proof=None,max_age=60,error=None,synthetic=False):
    """시각·통화·숫자 오류를 보존하되 참조값을 임의 생성하지 않는다."""
    from ..market.bars import session_bounds
    if symbol not in REFERENCES:
        raise ValueError('등록된 시장 참조 ETF가 아닙니다.')
    received,ingested = utc(received_at),utc(ingested_at)
    proof = dict(proof or {})
    for field in ('source','evidence_url','comparability_source'):
        value = proof.get(field)
        if value is not None and (not isinstance(value,str) or re.search(r'(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]',value) or re.search(r'https?://[^/]*@',value)):
            proof[field] = None
            proof['verified'] = False
    reasons,stamp = [],None
    try:
        stamp = utc(event_at) if event_at else None
    except (ValueError,TypeError,AttributeError):
        reasons.append('invalid_event_time')
    if stamp is None:reasons.append('event_time_unknown')
    if received > ingested or (stamp and stamp > received):reasons.append('time_reversed_or_future')
    classification = permission_status(proof,symbol,ingested)
    if classification == 'UNKNOWN':reasons.append('provider_latency_permission_unverified')
    if classification == 'DELAYED':reasons.append('provider_declared_delayed')
    currency = values.get('currency')
    if currency != 'USD':reasons.append('currency_unknown_or_mismatch')
    normalized = {}
    for key in ('price','open','high','low','close','volume','bid','ask','bid_size','ask_size'):
        if key not in values:continue
        try:normalized[key] = decimal(values[key],key,nonnegative=key in {'volume','bid_size','ask_size'},positive=key not in {'volume','bid_size','ask_size'})
        except ValueError:normalized[key]=None;reasons.append('invalid_'+key)
    if stream == 'orderbook' and (normalized.get('bid') is None or normalized.get('ask') is None or normalized['bid'] > normalized['ask']):
        reasons.append('orderbook_inverted_or_incomplete')
    if stream == 'candle':
        if stamp and (stamp.second or stamp.microsecond):reasons.append('inexact_minute_boundary')
        if all(normalized.get(k) is not None for k in ['open','high','low','close']):
            if normalized['low'] > min(normalized['open'],normalized['close']) or normalized['high'] < max(normalized['open'],normalized['close']):reasons.append('invalid_ohlc')
        else:reasons.append('ohlc_missing')
        if normalized.get('volume') is None:reasons.append('volume_missing')
        elif normalized['volume'] == 0:reasons.append('zero_volume_observation')
    bounds = session_bounds(stamp) if stamp else None
    regular = bool(bounds and bounds[0] <= stamp <= bounds[1])
    if stamp and stream != 'daily' and not regular:reasons.append('outside_regular_session')
    age = (ingested-stamp).total_seconds() if stamp else None
    if age is not None and age > max_age:reasons.append('event_stale')
    invalid = any(r.startswith('invalid_') or r in {'time_reversed_or_future','currency_unknown_or_mismatch','orderbook_inverted_or_incomplete','inexact_minute_boundary','ohlc_missing','volume_missing'} for r in reasons)
    status = ('UNAVAILABLE' if error else 'INVALID' if invalid else 'STALE' if 'event_stale' in reasons or 'outside_regular_session' in reasons
              else 'DELAYED' if classification=='DELAYED' else 'UNKNOWN_LATENCY' if classification=='UNKNOWN' or stamp is None else 'GOOD')
    if error:reasons.append(error)
    # 증거의 고정된 계약 필드만 저장한다. 키·토큰·계좌 번호를 넘겨 저장하지 않는다.
    permissions = {k:proof.get(k) for k in ('verified','scope','symbols','source','evidence_url','checked_at','valid_until','latency_class',
                                            'realtime_entitled','declared_delay_seconds','price_comparability_verified','comparability_source','range_start','range_end')}
    body = {'symbol':symbol,'purpose':'MARKET_REFERENCE','reference_kind':REFERENCES[symbol][0],'label':REFERENCES[symbol][1],
        'stream':stream,'source':SOURCE,'event_at':stamp,'as_of_at':stamp,'received_at':received,'ingested_at':ingested,
        'available_at':max(received,ingested),'currency':currency,'price':normalized.get('price',normalized.get('close')),
        'values':normalized,'market_session':{'opens_at':bounds[0],'closes_at':bounds[1]} if bounds else None,
        'latency_class':classification,'freshness_seconds':age,'max_age_seconds':max_age,
        'valid_until':stamp+timedelta(seconds=max_age) if stamp else None,'quality_status':status,
        'quality_reasons':reasons,'provider_permissions':permissions,'synthetic':synthetic,'price_adjustment':'raw',
        'series_comparability':'UNKNOWN'}
    try:
        if (classification=='REALTIME' and proof.get('price_comparability_verified') is True and proof.get('comparability_source')
                and stamp and utc(proof['range_start']) <= stamp <= utc(proof['range_end']) <= utc(proof['checked_at'])):
            body['series_comparability'] = 'VERIFIED'
    except (ValueError,TypeError,KeyError,AttributeError):
        pass
    revision = identity({'symbol':symbol,'stream':stream,'event_at':stamp,'values':normalized,'currency':currency,'error':error})
    body.update(id='reference:'+revision,revision_id=revision,previous_revision_id=None)
    return serialize(body)
