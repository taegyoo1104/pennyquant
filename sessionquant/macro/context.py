"""일별 공식 자료와 장중 ETF를 원본 단위·관측 시점별로 분리한 감사 맥락."""
from dataclasses import dataclass,asdict
from datetime import timedelta

from ..models import utc
from .models import stable_hash


@dataclass(frozen=True)
class MacroContextSnapshot:
    context_at: str
    known_at: str
    official_daily_indicators: list
    intraday_market_proxies: list
    related_verified_news_ids: list
    economic_events: list
    quality: str
    reasons: list


def build_context(repo,now,*,analyses=(),events=()):
    now=utc(now);daily=[];proxies=[];reasons=[];news=[]
    for row in repo.recent('fred_reference_observations',limit=30):
        try:
            known=utc(row.get('ingested_at') or row['received_at'])
            received=utc(row['received_at'])
            if not received<=known<=now:raise ValueError('미래 또는 역전된 수신')
            if row.get('observation_date') and row['observation_date']>now.date().isoformat():raise ValueError('미래 관측')
            value={**row,'role':'OFFICIAL_DAILY_REFERENCE','intraday_tick':False}
            if row.get('published_at') is None or not row.get('point_in_time_verified'):
                value['eligible_for_market_inference']=False;reasons.append('fred_publication_time_unknown')
            elif utc(row['published_at'])>received:raise ValueError('공표 시각 역전')
            else:value['eligible_for_market_inference']=True
            daily.append(value)
        except (ValueError,TypeError,KeyError,AttributeError):reasons.append('fred_time_mismatch_rejected')
    view=repo.get_control('market_reference_view_v1',{}) or {}
    if view:
        # 02단계의 검증 결과를 재사용한다. 수익률을 금리 bp·WTI 현물로 바꾸지 않는다.
        values=view.get('items',{})
        for row in values.values() if isinstance(values,dict) else values:
            try:
                stamp=row.get('available_at')
                if stamp is None or utc(stamp)>now or now-utc(stamp)>timedelta(minutes=15):raise ValueError('시각 불일치')
                if (row.get('event_at') is None or utc(row['event_at'])>utc(stamp)
                        or row.get('quality_status')!='GOOD' or utc(row['valid_until'])<now):raise ValueError('검증 자료 부족')
                windows=row.get('windows',{})
                if not windows or not any(w.get('quality_status')=='GOOD' for w in windows.values()):
                    raise ValueError('시간창 미확인')
                verified_windows={}
                for minutes,window in windows.items():
                    if window.get('quality_status')!='GOOD':continue
                    if (not utc(window['start_at'])<=utc(window['event_at'])<=utc(window['available_at'])<=now
                            or utc(window['available_at'])>utc(stamp)
                            or utc(window['event_at'])!=utc(row['event_at'])
                            or utc(window['event_at'])-utc(window['start_at'])!=timedelta(minutes=int(minutes))
                            or window.get('sample_count')!=int(minutes)+1
                            or window.get('required_samples')!=int(minutes)+1):raise ValueError('시간창 불일치')
                    verified_windows[minutes]=window
                proxies.append({**row,'windows':verified_windows,'role':'ETF_PROXY_ONLY','source_window_preserved':True})
            except (ValueError,TypeError,KeyError,AttributeError):reasons.append('proxy_time_mismatch_rejected')
    for row in analyses:
        try:
            if (row.get('source_verified') is True and row.get('status')=='CONFIRMED' and not row.get('error')
                    and not row.get('context_only') and now-utc(row['published_at'])<=timedelta(hours=24)
                    and utc(row['published_at'])<=utc(row['received_at'])<=utc(row['analyzed_at'])<=now):news.append(row['article_id'])
        except (ValueError,TypeError,KeyError,AttributeError):reasons.append('news_time_mismatch_rejected')
    if not news:reasons.append('verified_news_cause_unknown')
    event_rows=[asdict(event) for event in events if utc(event.known_at)<=now]
    from ..portfolio.models import serialize
    payload=serialize(asdict(MacroContextSnapshot(now.isoformat(),now.isoformat(),daily,proxies,sorted(set(news)),event_rows,
        'PARTIAL' if reasons else 'VERIFIED',sorted(set(reasons)))))
    payload.update(id='macro-context:'+stable_hash(payload),causal_inference_verified=False)
    return payload
