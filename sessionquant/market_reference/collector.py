"""보유 안전 조회 뒤 남은 읽기 예산만 사용하는 별도 ETF 관측 스케줄."""
from datetime import timedelta
from time import monotonic
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..models import utc
from ..market.bars import session_bounds
from .contracts import DEFAULT_REFERENCE_SYMBOLS, observation
from .store import ReferenceStore


class MarketReferenceMonitor:
    """주문·적격성 인터페이스가 없다. 공급자 권한 증거 미등록은 UNKNOWN이다."""
    def __init__(self,repo,*,permissions=None,clock,price_seconds=30,candle_seconds=180,calls_per_cycle=2,synthetic=False,
                 symbols=None,display_only=False,stream_prices=False):
        from .settings import reference_settings
        config=reference_settings({'symbols':list(DEFAULT_REFERENCE_SYMBOLS) if symbols is None else symbols,'display_only':display_only})
        self.symbols=tuple(config['symbols'])
        self.display_only=config['display_only']
        self.stream_prices=bool(stream_prices and self.display_only)
        self.store = ReferenceStore(repo)
        self.permissions = permissions or {}
        self.clock = clock
        self.price_seconds,self.candle_seconds,self.calls_per_cycle = price_seconds,candle_seconds,calls_per_cycle
        self.synthetic = synthetic

    def _record(self,symbol,stream,values,event,received,*,error=None):
        row = observation(symbol,stream,values,event,received,self.clock(),proof=self.permissions.get(symbol),
                          max_age=300 if stream=='candle' else 86400*7 if stream=='daily' else 60,error=error,synthetic=self.synthetic)
        capability = self.store.state['capabilities'].setdefault(symbol,{})
        good_shape = row['quality_status'] not in {'INVALID','UNAVAILABLE'} and row['event_at'] is not None
        capability[stream] = {'status':'OBSERVED' if good_shape else 'UNKNOWN','instrument_supported':True if good_shape else None,
            'received_at':row['received_at'],'valid_until':row['valid_until'],'quality_status':row['quality_status'],
            'latency_class':row['latency_class'],'realtime_entitled':row['provider_permissions'].get('realtime_entitled'),
            'scope':'endpoint_response_only','synthetic':self.synthetic}
        self._pending.append(row)
        return row

    async def poll(self,read,*,limit=None):
        """read는 남은 주기 예산·대기를 적용한 읽기 함수이며 토큰을 새로 만들지 않는다."""
        self._pending = []
        now = utc(self.clock());bounds = session_bounds(now)
        preopen = bool(bounds and now < bounds[0] and self.display_only)
        if not bounds or not (preopen or bounds[0] <= now < bounds[1]):
            return self.store.publish(now,symbols=self.symbols)
        schedules = self.store.state['schedule']
        jobs = [('prices','*',self.price_seconds)] if self.display_only else [('stocks','*',86400),('prices','*',self.price_seconds)]
        if self.stream_prices:jobs=[]
        for symbol in self.symbols:
            # A failed optional request must not hide the daily comparison all day.
            from .daily import previous_close
            daily_interval = 300 if self.display_only and previous_close(self.store, symbol, now) is None else 86400
            jobs += [('daily',symbol,daily_interval)] if self.stream_prices or preopen else [('candle',symbol,self.candle_seconds),('daily',symbol,daily_interval)] if self.display_only else [
                ('orderbook',symbol,60),('candle',symbol,self.candle_seconds),('daily',symbol,86400)]
        # Refresh yesterday's close once in preopen and again at the open, not
        # on every preopen cycle simply because the open is still in the future.
        daily_boundary = bounds[0].astimezone(ZoneInfo('America/New_York')).replace(hour=0,minute=0) if preopen else bounds[0]
        due = [(stream,symbol,interval) for stream,symbol,interval in jobs
               if not schedules.get(stream+':'+symbol) or (now-utc(schedules[stream+':'+symbol])).total_seconds()>=interval
               or (stream=='daily' and utc(schedules[stream+':'+symbol])<daily_boundary)]
        due.sort(key=lambda row:(schedules.get(row[0]+':'+row[1],''),jobs.index(row)))
        for stream,symbol,_ in due[:min(self.calls_per_cycle,self.calls_per_cycle if limit is None else limit)]:
            schedules[stream+':'+symbol] = utc(self.clock()).isoformat()
            began = monotonic()
            result = await read(stream,symbol)
            received = utc(result.get('received_at') or self.clock())
            self.store.state['last_request'] = {'stream':stream,'symbol':symbol,'received_at':received.isoformat(),
                                              'duration_seconds':round(monotonic()-began,3),'error':result.get('error')}
            data = result.get('data')
            targets = list(self.symbols) if symbol=='*' else [symbol]
            if result.get('error') or data is None:
                for target in targets:
                    self._record(target,stream,{},None,received,error=result.get('error') or 'response_unavailable')
                with self.store.repo.transaction():
                    for row in self._pending:self.store.ingest(row,save=False)
                    self.store.save()
                self._pending = []
                continue
            try:
                if stream == 'stocks':
                    if not isinstance(data,list):raise ValueError('종목 응답 형식 미확인')
                    rows = {}
                    for item in data:
                        if not isinstance(item,dict) or item.get('symbol') in rows:raise ValueError('중복 또는 비정상 종목 응답')
                        rows[item.get('symbol')] = item
                    for target in targets:
                        row=rows.get(target,{})
                        supported = row.get('currency')=='USD' and row.get('securityType') in {'ETF','FOREIGN_ETF'} and row.get('status')=='ACTIVE'
                        self.store.state['capabilities'].setdefault(target,{})['stocks'] = {'status':'VERIFIED' if supported else 'UNKNOWN',
                            'received_at':received.isoformat(),'scope':'instrument_reference_only','realtime_entitled':None}
                elif stream == 'prices':
                    if not isinstance(data,list):raise ValueError('현재가 응답 형식 미확인')
                    rows = {}
                    for item in data:
                        if not isinstance(item,dict) or item.get('symbol') in rows:raise ValueError('중복 또는 비정상 종목 응답')
                        rows[item.get('symbol')] = item
                    for target in targets:
                        row=rows.get(target,{})
                        self._record(target,'price',{'price':row.get('lastPrice'),'currency':row.get('currency')},row.get('timestamp'),received,
                                     error=None if row else 'symbol_response_missing')
                elif stream == 'orderbook':
                    if not isinstance(data,dict):raise ValueError('호가 응답 형식 미확인')
                    asks,bids = data.get('asks',[]),data.get('bids',[])
                    ask = min(asks,key=lambda r:Decimal(r['price']))
                    bid = max(bids,key=lambda r:Decimal(r['price']))
                    self._record(symbol,stream,{'bid':bid['price'],'ask':ask['price'],'bid_size':bid['volume'],
                        'ask_size':ask['volume'],'currency':data.get('currency')},data.get('timestamp'),received)
                else:
                    if not isinstance(data,dict) or not isinstance(data.get('candles'),list):raise ValueError('분봉 응답 형식 미확인')
                    if not data['candles']:
                        self._record(symbol,stream,{},None,received,error='empty_candles')
                    for row in data['candles']:
                        self._record(symbol,stream,{'open':row['openPrice'],'high':row['highPrice'],'low':row['lowPrice'],
                            'close':row['closePrice'],'volume':row['volume'],'currency':row.get('currency')},row.get('timestamp'),received)
            except (ValueError,KeyError,TypeError,ArithmeticError):
                self._pending = []
                for target in targets:self._record(target,stream,{},None,received,error='response_validation_failed')
            # DB 트랜잭션 안에서 await하지 않는다. 작은 청크 사이에 청산 시계를 실행할 수 있게 한다.
            import asyncio
            for start in range(0,len(self._pending),20):
                with self.store.repo.transaction():
                    for row in self._pending[start:start+20]:self.store.ingest(row,save=False)
                    self.store.save()
                await asyncio.sleep(0)
            self._pending = []
        self.store.save()
        return self.store.publish(utc(self.clock()),symbols=self.symbols)
