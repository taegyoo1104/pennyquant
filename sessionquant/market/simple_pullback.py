"""Versioned, inspectable three-rule Paper strategy. No claim of profitability."""
from .bars import contiguous

NAME = 'pullback_simple'
VERSION = 'simple-pullback-v1'


def progress(data):
    """Describe the latest CLOSED-bar shape, not a latched historical checklist."""
    bars, upper=list(data.five),list(data.fifteen)
    result={'stage':'DATA_WAIT','path':'확정봉 자료 확인 중','correction_bars':0,
        'five_at':bars[-1].timestamp.isoformat() if bars else None,
        'fifteen_at':upper[-1].timestamp.isoformat() if upper else None,
        'entry_history_ready':len(bars)>=15 and bool(data.atr14)}
    if len(bars)<2 or len(upper)<2 or not contiguous(bars,5) or not contiguous(upper[-2:],15):return result
    if upper[-1].close<=upper[-2].close:
        return {**result,'stage':'TREND_MISSING','path':'상승 조건 미충족 · 매수 대기'}
    # Include the newest candle while a correction is forming; the signal rule
    # instead excludes the newest candle because it tests that candle as rebound.
    end=len(bars)-1 if bars[-1].close<=bars[-2].close else len(bars)-2
    start=end
    while start>0 and bars[start].close<=bars[start-1].close:start-=1
    count=end-start
    if not count or not any(bars[i].close<bars[i-1].close for i in range(start+1,end+1)):
        return {**result,'stage':'TREND','path':'상승 확인 → 조정 대기'}
    result['correction_bars']=count
    if count>4:
        return {**result,'stage':'TOO_LONG','path':f'상승 확인 → 조정 {count}봉 · 허용 범위 초과'}
    low=min(b.low for b in bars[start+1:end+1])
    if data.atr14 and not 0<bars[start].high-low<=2.5*data.atr14:
        return {**result,'stage':'TOO_DEEP','path':'상승 확인 → 조정 하락 폭 범위 초과'}
    path='상승 확인 → '+' → '.join(f'조정 {i}봉' for i in range(1,count+1))
    rebound=end==len(bars)-2 and bars[-1].close>bars[-2].high
    result.update(stage='REBOUND' if rebound else 'CORRECTION',
                  path=path+(' → 반등 확인' if rebound else ' → 반등 대기'))
    if not result['entry_history_ready']:result['path']+=' · 진입용 자료 준비 중'
    return result


def inspect_pattern(data):
    bars, upper = list(data.five), list(data.fifteen)
    if len(bars)<15 or len(upper)<2 or not data.atr14 or not contiguous(bars,5):
        return {'version':VERSION,'status':'UNAVAILABLE','checks':[], 'setup_id':None}
    trend = contiguous(upper[-2:],15) and upper[-1].close > upper[-2].close
    # A maximal run of non-increasing closes, including at least one strict fall.
    end = len(bars)-2
    start = end
    while start>0 and bars[start].close <= bars[start-1].close:
        start -= 1
    count = end-start
    correction = bars[start+1:end+1]
    low = min((b.low for b in correction),default=bars[end].low)
    depth = bars[start].high-low
    adjusted = (1<=count<=4 and any(bars[i].close<bars[i-1].close for i in range(start+1,end+1))
                and 0<depth<=2.5*data.atr14)
    rebound = bars[-1].close>bars[-2].high
    checks=[{'key':'trend','label':'15분 상승 추세','passed':bool(trend)},
            {'key':'correction','label':'1~4개 조정봉·과도하지 않은 하락','passed':bool(adjusted)},
            {'key':'rebound','label':'직전 고가 위에서 5분봉 마감','passed':bool(rebound)}]
    return {'version':VERSION,'status':'SIGNAL' if all(c['passed'] for c in checks) else 'WAITING',
            'checks':checks,'correction_bars':count,'depth_atr':depth/data.atr14,
            'support':low,'setup_id':bars[start].timestamp.isoformat() if adjusted else None,
            'volume_confirmation':bool(data.volume_ratio is not None and data.volume_ratio>=1)}
