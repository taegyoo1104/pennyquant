"""동일 시각·단위가 확인된 일별 표본만 비교한다. 장중 금리/현물을 추정하지 않는다."""
from decimal import Decimal
from math import sqrt

from ..models import utc
from .bundle import events
from .quality import NY
from ..market.bars import session_bounds
from datetime import datetime, timedelta


def proxy_report(path):
    daily, fred = {}, {}
    quality_rejections, corporate_jumps = 0, 0
    for row in events(path):
        data = row['data']
        try:
            if row['kind'] == 'market_reference_observations':
                if data.get('series_comparability') != 'VERIFIED':quality_rejections += 1
                if data.get('corporate_action_confirmed') is True:corporate_jumps += 1
                if (data.get('stream') == 'daily' and data.get('series_comparability') == 'VERIFIED'
                        and data.get('source') == 'toss:market_reference:v1.2.28'
                        and data.get('quality_status') not in {'INVALID','UNAVAILABLE','REVISION_PENDING'}
                        and data.get('event_at') and data.get('price') is not None):
                    day = utc(data['event_at']).astimezone(NY).date().isoformat()
                    price = Decimal(data['price'])
                    bounds = session_bounds(datetime.fromisoformat(day).replace(tzinfo=NY)+timedelta(hours=12))
                    if bounds and utc(row['available_at']) >= bounds[1] and price.is_finite() and price>0:
                        daily.setdefault(data['symbol'],{}).setdefault(day,[]).append((utc(row['available_at']),price))
            if row['kind'] == 'fred_reference_observations':
                # 날짜만 있는 스냅샷·다른 공급자의 미확인 수치를 FRED 기준선으로 사용하지 않는다.
                if (data.get('point_in_time_verified') is True and data.get('published_at') and data.get('source') == 'FRED'
                        and utc(data['published_at']) <= utc(row['available_at'])):
                    value = Decimal(data['value'])
                    if value.is_finite():
                        fred.setdefault(data['series'],{}).setdefault(data['observation_date'],[]).append(
                            (utc(row['available_at']),value))
        except (ValueError,TypeError,KeyError,ArithmeticError):
            quality_rejections += 1
    pairs = {}
    for symbol,series in [('SHY','DGS2'),('IEF','DGS10'),('TLT','DGS10'),('USO','DCOILWTICO')]:
        samples = []
        common = sorted(set(daily.get(symbol,{})) & set(fred.get(series,{})))
        for previous,current in zip(common,common[1:]):
            # 같은 관측일도 나중에 받았다면 소급하지 않는다. 각 날짜 끝에 이미 받은 값만 비교한다.
            cutoffs = [datetime.fromisoformat(day).replace(tzinfo=NY)+timedelta(days=1) for day in (previous,current)]
            selected = []
            for day,cutoff in zip((previous,current),cutoffs):
                p = [v for t,v in daily[symbol][day] if t < cutoff]
                f = [v for t,v in fred[series][day] if t < cutoff]
                if p and f:selected.append((p[-1],f[-1]))
            if len(selected)==2:
                (a,x),(b,y) = selected
                samples.append({'from_date':previous,'to_date':current,'etf_return_pct':str((b/a-1)*100),
                    'benchmark_change':str((y-x)*100 if series.startswith('DGS') else y-x),
                    'benchmark_unit':'basis_points' if series.startswith('DGS') else 'usd_per_barrel'})
        correlation,normalized_error = None,None
        decoupling = []
        if len(samples)>=3:
            xs=[float(s['etf_return_pct']) for s in samples];ys=[float(s['benchmark_change']) for s in samples]
            mx,my=sum(xs)/len(xs),sum(ys)/len(ys)
            numerator=sum((x-mx)*(y-my) for x,y in zip(xs,ys))
            denominator=sqrt(sum((x-mx)**2 for x in xs)*sum((y-my)**2 for y in ys))
            correlation=numerator/denominator if denominator else None
            sx=sqrt(sum((x-mx)**2 for x in xs)/len(xs));sy=sqrt(sum((y-my)**2 for y in ys)/len(ys))
            sign=1 if symbol=='USO' else -1
            normalized_error=sqrt(sum(((x-mx)/sx-sign*(y-my)/sy)**2 for x,y in zip(xs,ys))/len(xs)) if sx and sy else None
        for sample in samples:
            a,b=Decimal(sample['etf_return_pct']),Decimal(sample['benchmark_change'])
            if a and b and ((a*b<0) if symbol=='USO' else (a*b>0)):decoupling.append(sample)
        pairs[symbol+':'+series]={'samples':samples,'sample_count':len(samples),'correlation':correlation,
            'standardized_direction_error_rmse':normalized_error,'decoupling_cases':decoupling,
            'status':'DESCRIPTIVE_ONLY' if correlation is not None else 'UNVERIFIABLE',
            'price_to_benchmark_conversion':None,'intraday_validation':'UNVERIFIABLE',
            'note':'TLT는 장기 듀레이션 비교이며 10년물 금리 대용이 아닙니다.' if symbol=='TLT' else
                '일별 공개 시점이 맞는 표본의 기술 통계입니다. ETF 수익률을 금리 bp·현물 유가로 환산하지 않습니다.'}
    return {'pairs':pairs,'quality_rejected_observations':quality_rejections,
        'confirmed_corporate_action_observations':corporate_jumps,
        'false_signal_rate':None,'decoupling_cases':[],
        'limitation':'롤오버·배당·분할의 확정 원장과 동일 시각의 실제 금리/현물 자료가 없으면 가짜 신호율·장중 대리 효과는 미평가입니다.'}
