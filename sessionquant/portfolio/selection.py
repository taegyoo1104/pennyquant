"""동일 시점의 집중 후보를 순위화하고 포트폴리오의 선택 사유를 남긴다."""
from dataclasses import dataclass,asdict,replace
from datetime import timedelta
import math

from ..market.bars import session_bounds
from ..market.scanner import SCORE_WEIGHTS
from ..market_reference.contracts import REFERENCE_SYMBOLS,identity
from ..models import utc
from ..news.ranking import score_value
from .strength import RelativeStrength

VIEW='portfolio_selection_v1'
VERSION='portfolio-candidates-v1'


@dataclass(frozen=True)
class CandidateSettings:
    enabled: bool=True
    use_news_score: bool=True
    use_sector_score: bool=True
    final_candidate_limit: int=5
    quote_alignment_seconds: int=2
    allow_above_preferred: bool=False
    max_same_sector: int | None=None
    max_high_index_correlation: int | None=None
    index_correlation_threshold: float=.85
    correlation_minutes: int=30
    fail_closed_diversification: bool=True

    def __post_init__(self):
        for key in ('enabled','use_news_score','use_sector_score','allow_above_preferred','fail_closed_diversification'):
            if type(getattr(self,key)) is not bool:raise ValueError('후보 선정 기능 설정은 불리언이어야 합니다.')
        for key,low,high in [('final_candidate_limit',1,5),('quote_alignment_seconds',0,10),('correlation_minutes',20,120)]:
            if type(getattr(self,key)) is not int or not low<=getattr(self,key)<=high:raise ValueError('후보 수·시간 정렬·상관 표본 설정을 확인하세요.')
        for key in ('max_same_sector','max_high_index_correlation'):
            value=getattr(self,key)
            if value is not None and (type(value) is not int or not 1<=value<=5):raise ValueError('분산 제한은 1~5 정수 또는 null이어야 합니다.')
        if type(self.index_correlation_threshold) not in (int,float) or not math.isfinite(self.index_correlation_threshold) or not .5<=self.index_correlation_threshold<=1:
            raise ValueError('지수 상관 임계값은 0.5~1이어야 합니다.')


class PortfolioCandidateSelector:
    def __init__(self,settings=None):
        self.settings=settings if isinstance(settings,CandidateSettings) else CandidateSettings(**(settings or {}))

    def rank(self,decisions,market,broker,now,*,synthetic=False,manual_halt=False,consumed=(),scope_expiries=None):
        now=utc(now);settings=self.settings
        strength=RelativeStrength(market,now,synthetic=synthetic)
        expected=None;bounds=session_bounds(now)
        if bounds:expected=bounds[0]+timedelta(minutes=int((now-bounds[0]).total_seconds()//300)*5)
        fresh=[q.timestamp for decision,_ in decisions if (q:=market.quotes.get(decision.symbol))
            and q.timestamp<=q.received_at<=now and (now-q.timestamp).total_seconds()<=broker.settings.max_quote_age_seconds]
        anchor=max(fresh) if fresh else None
        ranked=[]
        for decision,risk in decisions:
            symbol=decision.symbol;quote=market.quotes.get(symbol)
            news=risk.get('candidate_score_signal') or {}
            if not isinstance(news,dict):news={'status':'UNKNOWN','value':None,'reason':'invalid_news_score_contract'}
            news_value=score_value(news,symbol,now) if settings.use_news_score else None
            sector=strength.sector_strength(symbol)
            sector_value=sector['value'] if settings.use_sector_score and sector['status']=='VERIFIED' else None
            components={**decision.indicators.get('score_components',{}),'news':news_value or 0.,'sector':sector_value or 0.}
            score=round(sum(components.get(key,0.)*weight for key,weight in SCORE_WEIGHTS.items())*100,4)
            blockers=list(decision.blockers)
            if symbol in REFERENCE_SYMBOLS:blockers.append('market_reference_not_trading_candidate')
            if not expected or decision.indicators.get('last_closed_at')!=expected.isoformat():blockers.append('candidate_bar_time_mismatch')
            if not quote or not quote.timestamp<=quote.received_at<=now or (now-quote.timestamp).total_seconds()>broker.settings.max_quote_age_seconds:
                blockers.append('candidate_quote_stale_or_unknown')
            elif anchor and (anchor-quote.timestamp).total_seconds()>settings.quote_alignment_seconds:blockers.append('candidate_quote_time_mismatch')
            if not decision.signal:blockers.append('technical_signal_absent')
            elif decision.signal.id in consumed:blockers.append('signal_already_consumed')
            if risk.get('block_entries') or risk.get('blackout'):blockers.append('confirmed_news_entry_restriction')
            if manual_halt:blockers.append('trading_not_started_or_halted')
            indicators={**decision.indicators,'score_components':components,'score_component_status':{
                'news':'DISABLED' if not settings.use_news_score else 'VERIFIED' if news_value is not None else 'UNKNOWN',
                'sector':'DISABLED' if not settings.use_sector_score else 'VERIFIED' if sector_value is not None else 'UNKNOWN'}}
            updated=replace(decision,score=score,indicators=indicators)
            row={**updated.to_dict(),'blockers':list(dict.fromkeys(blockers)),
                'candidate_score':score,'news_score_signal':news,'sector_strength':sector,
                'index_correlation':strength.index_correlation(symbol,settings.correlation_minutes),
                'sector':strength.sector(symbol),'evaluated_at':now.isoformat(),
                'scope_valid_until':(scope_expiries or {}).get(symbol),
                'quote_at':quote.timestamp.isoformat() if quote else None,
                'quote_received_at':quote.received_at.isoformat() if quote else None,
                'portfolio_status':'BLOCKED' if blockers else 'RANKED','allocation_reason':None,
                'approved':False,'order_intent_id':None,'macro_score_applied':False}
            ranked.append((updated,risk,row))
        ranked.sort(key=lambda value:(-value[0].score,value[0].symbol,value[0].strategy))
        seen=set()
        for index,(_,_,row) in enumerate(ranked,1):
            row['rank']=index
            if row['symbol'] in seen:row['blockers'].append('duplicate_symbol_signal')
            seen.add(row['symbol'])
            row['comparison_eligible']=not row['blockers']
            row['final_candidate']=False
        expiry=[now+timedelta(seconds=broker.settings.max_quote_age_seconds)]
        snapshot=market.universe.snapshot(now)
        if snapshot:expiry.append(utc(snapshot['effective_at'])+timedelta(days=market.universe.max_age_days))
        for _,_,row in ranked:
            if not row['comparison_eligible']:continue
            symbol=row['symbol'];quote=market.quotes.get(symbol);eligibility=market.eligibility.get(symbol)
            if quote:expiry.extend([quote.timestamp+timedelta(seconds=broker.settings.max_quote_age_seconds),
                quote.received_at+timedelta(seconds=broker.settings.max_quote_age_seconds)])
            if eligibility:
                age = 86400 if market.paper_data_policy == 'observed_paper_v1' else market.max_eligibility_age_seconds
                expiry.append(eligibility.checked_at+timedelta(seconds=age))
                if eligibility.valid_until:expiry.append(eligibility.valid_until)
                if market.paper_data_policy == 'observed_paper_v1':
                    listing=eligibility.field_evidence.get('listing_status',{})
                    if listing.get('received_at'):expiry.append(utc(listing['received_at'])+timedelta(days=1))
                    if listing.get('valid_until'):expiry.append(utc(listing['valid_until']))
            rvol=row['indicators'].get('data_quality',{}).get('rvol',{})
            if row['indicators'].get('rvol_required',True) and rvol.get('valid_until'):expiry.append(utc(rvol['valid_until']))
            news=row['news_score_signal']
            if row['indicators']['score_component_status']['news']=='VERIFIED':expiry.append(utc(news['valid_until']))
            sector=row['sector_strength']
            if row['indicators']['score_component_status']['sector']=='VERIFIED':expiry.append(utc(sector['valid_until']))
            if row.get('scope_valid_until'):expiry.append(utc(row['scope_valid_until']))
            if settings.max_high_index_correlation:
                for peer in sorted(set(broker.positions)|{order.intent.symbol for order in broker.get_open_orders() if order.intent.side=='buy'}|{symbol}):
                    correlated=strength.index_correlation(peer,settings.correlation_minutes)
                    if correlated.get('valid_until'):expiry.append(utc(correlated['valid_until']))
        portfolio_hash=identity(broker.to_state())
        frame={'id':'portfolio-selection:'+identity([VERSION,now.isoformat(),asdict(settings),portfolio_hash,[item[2] for item in ranked]]),
            'portfolio_state_hash':portfolio_hash,
            'kind':'portfolio_selection','policy_version':VERSION,'settings':asdict(settings),'evaluated_at':now.isoformat(),
            'valid_until':min(expiry).isoformat(),
            'quote_anchor_at':anchor.isoformat() if anchor else None,'common_closed_bar_at':expected.isoformat() if expected else None,
            'synthetic':synthetic,'preferred_positions':broker.settings.preferred_positions,'max_positions':broker.settings.max_positions,
            'evaluated_count':len(ranked),'comparison_eligible_count':sum(item[2]['comparison_eligible'] for item in ranked),
            'final_candidate_count':0,
            'approved_count':0,'approved_new_count':0,'approved_add_count':0,'rows':[item[2] for item in ranked]}
        return ranked,frame,strength

    def exposure_overlap(self,row,broker,strength):
        symbol=row['symbol'];sector=strength.sector(symbol);settings=self.settings
        exposed=set(broker.positions)|{order.intent.symbol for order in broker.get_open_orders() if order.intent.side=='buy'}
        peers=sorted(exposed-{symbol})
        current=strength.index_correlation(symbol,settings.correlation_minutes)
        correlations={peer:strength.index_correlation(peer,settings.correlation_minutes) for peer in peers}
        return {'same_sector_symbols':[peer for peer in peers if sector and strength.sector(peer)==sector],
            'sector_unknown_symbols':[peer for peer in [symbol,*peers] if not strength.sector(peer)],
            'high_index_correlation_symbols':[peer for peer,value in correlations.items()
                if current['status']=='VERIFIED' and current['value']>=settings.index_correlation_threshold
                and value['status']=='VERIFIED' and value['value']>=settings.index_correlation_threshold],
            'index_correlation_unknown_symbols':[peer for peer,value in {symbol:current,**correlations}.items() if value['status']!='VERIFIED'],
            'max_same_sector':settings.max_same_sector,'max_high_index_correlation':settings.max_high_index_correlation}

    def admission_blockers(self,row,broker,strength):
        settings=self.settings;symbol=row['symbol']
        orders=broker.get_open_orders()
        exposed=set(broker.positions)|{order.intent.symbol for order in orders if order.intent.side=='buy'}
        if symbol in broker.exit_intents or any(order.intent.symbol==symbol for order in orders):return ['pending_order_or_exit']
        if symbol in broker.positions:return []
        if len(exposed)>=broker.settings.max_positions:return ['position_limit']
        if not settings.allow_above_preferred and len(exposed)>=broker.settings.preferred_positions:return ['preferred_position_target_reached']
        blockers=[]
        sector=strength.sector(symbol)
        other_sectors=[strength.sector(other) for other in exposed]
        if settings.max_same_sector:
            if (not sector or None in other_sectors) and settings.fail_closed_diversification:blockers.append('sector_diversification_unknown')
            elif sector and other_sectors.count(sector)>=settings.max_same_sector:blockers.append('same_sector_limit')
        if settings.max_high_index_correlation:
            scores=[strength.index_correlation(other,settings.correlation_minutes) for other in [*sorted(exposed),symbol]]
            if any(score['status']!='VERIFIED' for score in scores):
                if settings.fail_closed_diversification:blockers.append('index_correlation_unknown')
            elif scores[-1]['value']>=settings.index_correlation_threshold and sum(score['value']>=settings.index_correlation_threshold for score in scores[:-1])>=settings.max_high_index_correlation:
                blockers.append('high_index_correlation_limit')
        return blockers
