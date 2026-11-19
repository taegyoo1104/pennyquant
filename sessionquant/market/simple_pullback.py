"""Versioned, inspectable three-rule Paper strategy. No claim of profitability."""
from .bars import contiguous

NAME = 'pullback_simple'
VERSION = 'simple-pullback-v1'


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
