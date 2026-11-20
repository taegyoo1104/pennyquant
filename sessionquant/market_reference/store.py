"""참조 원장을 기존 주문·시세·FRED 일별 원장과 분리해 보존한다."""
from copy import deepcopy
from ..models import utc
from .contracts import identity

STATE = 'market_reference_state_v1'
VIEW = 'market_reference_view_v1'
OBSERVATIONS = 'market_reference_observations'
METRICS = 'market_reference_metrics'


class ReferenceStore:
    def __init__(self,repo):
        self.repo = repo
        self.state = repo.get_control(STATE,{})
        self.state.setdefault('rows',{})
        self.state.setdefault('latest',{})
        self.state.setdefault('revised_keys',[])
        self.state.setdefault('schedule',{})
        self.state.setdefault('capabilities',{})
        self.state.setdefault('duplicate_count',0)

    def ingest(self,row,*,save=True):
        key = row['symbol']+':'+row['stream']
        rows = self.state['rows'].setdefault(key,[])
        existing = next((r for r in rows if r['id']==row['id']),None)
        if existing:
            self.state['duplicate_count'] += 1
            if save:self.save()
            return existing
        revision_key = key+':'+str(row['event_at'])
        prior = [r for r in rows if r['event_at'] == row['event_at'] and row['event_at'] is not None]
        if prior:
            row['previous_revision_id'] = prior[-1]['revision_id']
            self.state['revised_keys'] = list(dict.fromkeys([*self.state['revised_keys'],revision_key]))[-4000:]
            row['quality_reasons'] = [*row['quality_reasons'],'revision_unresolved']
            row['quality_status'] = 'REVISION_PENDING'
        self.repo.record(OBSERVATIONS,row)
        rows.append(deepcopy(row))
        # 최근 계산용 캐시만 제한한다. 원본 SQL 원장은 삭제하지 않는다.
        self.state['rows'][key] = sorted(rows,key=lambda r:(r['event_at'] or '',r['available_at']))[-120:]
        previous_latest = self.state['latest'].get(key)
        if (not previous_latest or row['event_at'] is None or previous_latest['event_at'] is None
                or row['event_at'] >= previous_latest['event_at']):
            self.state['latest'][key] = deepcopy(row)
        if row['stream'] == 'candle' and row['market_session'] and row['event_at']:
            from datetime import timedelta
            if utc(row['event_at']) == utc(row['market_session']['opens_at'])+timedelta(minutes=1):
                self.state.setdefault('day_opens',{})[row['symbol']] = deepcopy(row)
        if save:self.save()
        return row

    def save(self):
        self.repo.set_control(STATE,self.state)

    def publish(self,now,*,symbols=None):
        from .metrics import build_metrics
        from .contracts import DEFAULT_REFERENCE_SYMBOLS
        symbols=list(DEFAULT_REFERENCE_SYMBOLS) if symbols is None else list(symbols)
        result = {'updated_at':utc(now).isoformat(),'purpose':'MARKET_REFERENCE','live_orders_enabled':False,
                  'active_symbols':symbols,
                  'duplicate_count':self.state['duplicate_count'],'capabilities':self.state['capabilities'],'items':{}}
        for symbol in symbols:
            metrics = build_metrics(self,symbol,now)
            result['items'][symbol] = metrics
            previous = self.state.setdefault('metrics',{}).get(symbol)
            if not previous or previous['id'] != metrics['id']:
                self.repo.record(METRICS,metrics)
                self.state['metrics'][symbol] = metrics
            else:
                result['items'][symbol] = previous
        self.save()
        self.repo.set_control(VIEW,result)
        return result

    def rows(self,symbol,stream,now):
        revised = set(self.state['revised_keys'])
        return [r for r in self.state['rows'].get(symbol+':'+stream,[])
                if utc(r['available_at']) <= now and r['event_at'] is not None and utc(r['event_at']) <= now
                and symbol+':'+stream+':'+r['event_at'] not in revised]
