"""동일 구간·출처·구성 근거가 있는 가격만 업종/지수 비교에 사용한다."""
from datetime import timedelta
from statistics import correlation, StatisticsError
import math

from ..market.bars import available_minutes, session_bounds
from ..market.quality import volume_evidence_valid
from ..market_reference.contracts import REFERENCE_SYMBOLS, identity
from ..models import utc, jsonable


class RelativeStrength:
    def __init__(self,market,now,*,synthetic=False):
        self.market,self.now,self.synthetic=market,utc(now),synthetic
        self.snapshot=market.universe.snapshot(now)
        self.members=market.universe.members(now)
        self._windows={};self._sectors={};self._correlations={}

    def sector(self,symbol):
        item=self.members.get(symbol)
        value=item.sector.strip() if item and isinstance(item.sector,str) else ''
        return value if value.upper() not in {'','UNKNOWN','N/A'} else None

    def window(self,symbol,minutes):
        key=(symbol,minutes)
        if key in self._windows:return self._windows[key]
        rows=available_minutes(self.market.bars.get(symbol,[]),self.now,symbol)
        bounds=session_bounds(self.now)
        end=self.now.replace(second=0,microsecond=0)
        wanted=[end-timedelta(minutes=i) for i in reversed(range(minutes+1))]
        by_time={row.timestamp:row for row in rows}
        selected=[by_time[t] for t in wanted if t in by_time]
        valid=bool(bounds and wanted[0]>bounds[0] and len(selected)==minutes+1
            and len({r.source for r in selected})==1 and selected[0].source not in {'','UNKNOWN'}
            and len({r.price_adjustment for r in selected})==1 and selected[0].price_adjustment in {'raw','adjusted'}
            and 0<=(self.now-end).total_seconds()<=60)
        if valid:
            proof=(self.market.eligibility.get(symbol).volume_evidence if self.market.eligibility.get(symbol) else {})
            valid=volume_evidence_valid(proof,selected,wanted[0],end,self.now)
            if self.synthetic and all(r.source.startswith('SYNTHETIC') for r in selected):valid=True
            if not self.synthetic and any(r.source.startswith('SYNTHETIC') for r in selected):valid=False
        result=selected if valid else None
        self._windows[key]=result
        return result

    def valid_until(self,symbols):
        expiry=[self.now.replace(second=0,microsecond=0)+timedelta(minutes=1)]
        for symbol in symbols:
            item=self.market.eligibility.get(symbol)
            if item and item.volume_evidence.get('valid_until'):expiry.append(utc(item.volume_evidence['valid_until']))
        return min(expiry).isoformat()

    def sector_strength(self,symbol):
        sector=self.sector(symbol)
        if sector in self._sectors:return self._sectors[sector]
        result={'status':'UNKNOWN','value':None,'sector':sector,'observed_at':self.now.isoformat(),
            'reason':'sector_membership_or_comparable_window_missing','basis':'COMPLETE_SP500_SECTOR_EQUAL_WEIGHT_15M',
            'membership_id':self.snapshot.get('id') if self.snapshot else None,'synthetic':self.synthetic}
        members=sorted(s for s in self.members if self.sector(s)==sector and s not in REFERENCE_SYMBOLS) if sector else []
        result['required_members']=members
        benchmark=self.window('SPY',15)
        series=[self.window(s,15) for s in members]
        result['observed_members']=[symbol for symbol,rows in zip(members,series) if rows]
        if not sector:result['reason']='sector_membership_unknown'
        elif len(members)<2:result['reason']='sector_constituent_count_insufficient'
        elif not benchmark:result['reason']='benchmark_window_unverified'
        elif not all(series):result['reason']='sector_constituent_windows_incomplete'
        if len(members)>=2 and benchmark and all(series):
            sources={row.source for rows in [benchmark,*series] for row in rows}
            adjustments={row.price_adjustment for rows in [benchmark,*series] for row in rows}
            if len(sources)==len(adjustments)==1:
                sector_return=sum(rows[-1].close/rows[0].close-1 for rows in series)/len(series)
                benchmark_return=benchmark[-1].close/benchmark[0].close-1
                relative=sector_return-benchmark_return
                if not all(math.isfinite(value) for value in (sector_return,benchmark_return,relative)):
                    result['reason']='invalid_calculated_return'
                    self._sectors[sector]=result
                    return result
                result.update(status='VERIFIED',value=max(0.,min(1.,.5+relative/.04)),reason='complete_same_window_sector_vs_spy',
                    sector_return=sector_return,benchmark_return=benchmark_return,relative_return=relative,
                    start_at=benchmark[0].timestamp.isoformat(),end_at=benchmark[-1].timestamp.isoformat(),
                    available_at=max(r.received_at for rows in [benchmark,*series] for r in rows).isoformat(),
                    valid_until=self.valid_until(['SPY',*members]),
                    source=next(iter(sources)),price_adjustment=next(iter(adjustments)),
                    input_hash=identity([[jsonable(r) for r in rows] for rows in [benchmark,*series]]))
            else:result['reason']='mixed_sources_or_price_adjustments'
        self._sectors[sector]=result
        return result

    def index_correlation(self,symbol,minutes=30):
        key=(symbol,minutes)
        if key in self._correlations:return self._correlations[key]
        result={'status':'UNKNOWN','value':None,'benchmark':'SPY','sample_count':0,
                'observed_at':self.now.isoformat(),'reason':'comparable_return_series_missing'}
        stock,benchmark=self.window(symbol,minutes),self.window('SPY',minutes)
        if stock and benchmark and stock[0].source==benchmark[0].source and stock[0].price_adjustment==benchmark[0].price_adjustment:
            returns=lambda rows:[b.close/a.close-1 for a,b in zip(rows,rows[1:])]
            try:
                left,right=returns(stock),returns(benchmark)
                if not all(math.isfinite(value) for value in [*left,*right]):raise StatisticsError('invalid_calculated_return')
                value=correlation(left,right)
                result.update(status='VERIFIED',value=value,sample_count=minutes,reason='same_minute_returns_vs_spy',
                    start_at=stock[0].timestamp.isoformat(),end_at=stock[-1].timestamp.isoformat(),
                    available_at=max(row.received_at for row in [*stock,*benchmark]).isoformat(),
                    valid_until=self.valid_until([symbol,'SPY']),
                    source=stock[0].source,input_hash=identity([jsonable(stock),jsonable(benchmark)]))
            except StatisticsError:result['reason']='constant_return_series'
        self._correlations[key]=result
        return result
