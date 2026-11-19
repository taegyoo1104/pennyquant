"""금리 bp·현물 유가로 환산하지 않는 Decimal ETF 시계열 계산."""
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from ..models import utc
from ..portfolio.models import serialize
from .contracts import REFERENCES, identity, permission_status


def series_verified(rows,now):
    if not rows:return False
    for row in rows:
        if row.get('stream') == 'candle' and Decimal(row.get('values',{}).get('volume') or '0') <= 0:return False
        proof = row['provider_permissions']
        try:
            if (permission_status(proof,row['symbol'],now) != 'REALTIME' or proof.get('price_comparability_verified') is not True
                    or not proof.get('comparability_source') or not utc(proof['range_start']) <= utc(row['event_at']) <= utc(proof['range_end'])
                    or utc(proof['range_end']) > utc(proof['checked_at'])):return False
        except (ValueError,TypeError,KeyError,AttributeError):return False
    return True


def build_metrics(store,symbol,now):
    from ..market.bars import session_bounds
    now = utc(now);bounds = session_bounds(now)
    candles = [r for r in store.rows(symbol,'candle',now) if r['currency']=='USD' and r['price'] is not None
               and r['quality_status'] not in {'INVALID','UNAVAILABLE','REVISION_PENDING'}
               and bounds and bounds[0] < utc(r['event_at']) <= bounds[1]]
    lookup = {utc(r['event_at']):r for r in candles}
    last = max(candles,key=lambda r:r['event_at']) if candles else None
    live_session = bool(bounds and bounds[0] <= now < bounds[1])
    failed = store.state['latest'].get(symbol+':candle',{}).get('quality_status') in {'INVALID','UNAVAILABLE','REVISION_PENDING'}
    fresh = bool(not failed and last and 0 <= (now-utc(last['event_at'])).total_seconds() <= last['max_age_seconds'] and live_session)
    windows = {}
    for minutes in [1,5,15,30]:
        points = [lookup.get(utc(last['event_at'])-timedelta(minutes=i)) for i in range(minutes,-1,-1)] if last else []
        present = [r for r in points if r]
        value = (Decimal(present[-1]['price'])/Decimal(present[0]['price'])-1)*100 if len(present)==minutes+1 else None
        valid = value is not None and fresh and series_verified(present,now)
        returns = [Decimal(b['price'])/Decimal(a['price'])-1 for a,b in zip(present,present[1:])] if value is not None else []
        mean = sum(returns,Decimal(0))/len(returns) if returns else None
        volatility = (sum(((r-mean)**2 for r in returns),Decimal(0))/len(returns)).sqrt()*100 if returns else None
        windows[str(minutes)] = {'pct_change':value,'volatility_pct':volatility,'sample_count':len(present),
            'required_samples':minutes+1,'quality_status':'GOOD' if valid else 'UNKNOWN',
            'quality_reasons':[] if valid else ['window_incomplete_or_stale_or_comparability_unverified'],
            'event_at':last['event_at'] if last else None,'start_at':points[0]['event_at'] if points and points[0] else None,
            'available_at':max((r['available_at'] for r in present),default=None),'input_ids':[r['id'] for r in present]}
    prices = [r for r in store.rows(symbol,'price',now) if r['currency']=='USD' and r['quality_status'] not in {'INVALID','UNAVAILABLE','REVISION_PENDING'}]
    current = max(prices,key=lambda r:r['event_at']) if prices else last
    daily = [r for r in store.rows(symbol,'daily',now) if r['currency']=='USD' and r['price'] is not None
             and r['quality_status'] not in {'INVALID','UNAVAILABLE','REVISION_PENDING'}
             and utc(r['event_at']).astimezone(ZoneInfo('America/New_York')).date() < now.astimezone(ZoneInfo('America/New_York')).date()]
    previous = max(daily,key=lambda r:r['event_at']) if daily else None
    opened = lookup.get(bounds[0]+timedelta(minutes=1)) if bounds else None
    if opened is None:
        candidate = store.state.get('day_opens',{}).get(symbol)
        if candidate and bounds and utc(candidate['event_at']) == bounds[0]+timedelta(minutes=1) and utc(candidate['available_at']) <= now:opened = candidate
    open_value = Decimal(opened['values']['open']) if opened and opened['values'].get('open') else None
    prior_value = Decimal(previous['price']) if previous else None
    current_value = Decimal(current['price']) if current and current['price'] is not None else None
    book = store.state['latest'].get(symbol+':orderbook')
    spread = None
    if book and book['quality_status'] not in {'INVALID','UNAVAILABLE','REVISION_PENDING'} and book['values'].get('bid') and book['values'].get('ask'):
        bid,ask = Decimal(book['values']['bid']),Decimal(book['values']['ask'])
        spread = (ask-bid)/((ask+bid)/2)*100
    result = {'symbol':symbol,'purpose':'MARKET_REFERENCE','reference_kind':REFERENCES[symbol][0],'label':REFERENCES[symbol][1],
        'observed_at':now,'event_at':last['event_at'] if last else None,'available_at':last['available_at'] if last else None,
        'valid_until':last['valid_until'] if last else None,'synthetic':bool(last and last.get('synthetic')),
        'quality_status':'GOOD' if windows['30']['quality_status']=='GOOD' else 'UNKNOWN',
        'latency_class':last['latency_class'] if last else 'UNKNOWN','windows':windows,
        'current_price':current_value,'day_open':open_value,'previous_close':prior_value,
        'previous_close_at':previous['event_at'] if previous else None,
        'day_return_pct':(current_value/prior_value-1)*100 if current_value is not None and prior_value else None,
        'opening_gap_pct':(open_value/prior_value-1)*100 if open_value is not None and prior_value else None,
        'cross_day_comparability':'VERIFIED' if previous and opened and series_verified([previous,opened],now) else 'UNKNOWN',
        'spread_pct':spread,'spread_valid_until':book.get('valid_until') if book else None,
        'quality_reasons':[] if fresh else ['closed_session_or_no_fresh_candle'],
        # Bounded display snapshot, never evidence that a trading gate passed.
        'trend_points':[{'at':at.isoformat(),'price':r['price'],'available_at':r['available_at']}
                        for at,r in sorted(lookup.items()) if now-timedelta(hours=1)<=at<=now][-61:],
        'capabilities':store.state['capabilities'].get(symbol,{}),'latest_observations':{stream:store.state['latest'].get(symbol+':'+stream) for stream in ['price','orderbook','candle','daily']}}
    result['available_at'] = max((r['available_at'] for r in windows.values() if r['available_at']),default=result['available_at'])
    result = serialize(result)
    result['id'] = 'reference-metrics:'+identity({k:v for k,v in result.items() if k != 'observed_at'})
    return result
