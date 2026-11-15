"""수집과 유료 분석의 경계. 게시 시각·본문·출처를 독립적으로 확인한다."""
from datetime import timedelta
from collections import Counter

from ..models import utc
from ..macro.models import stable_hash

LEGACY_REASONS={'unverified_source_or_ticker':'unverified_article_identity',
    'stale_or_future_article':'stale_article','stale_or_inconsistent_time':'invalid_timestamp'}


def time_reason(published,received,now,max_age_hours):
    if published is None:return 'published_at_unknown'
    try:
        published,received,now=utc(published),utc(received),utc(now)
        if not published<=received<=now:return 'invalid_timestamp'
        if now-published>timedelta(hours=max_age_hours):return 'stale_article'
    except (TypeError,ValueError,AttributeError):return 'invalid_timestamp'
    return ''


def company_reason(event,ticker,kind,primary,now,max_age_hours=24):
    if ticker not in event.symbols:return 'ticker_not_in_article_symbols'
    if not event.verified:return 'unverified_article_identity'
    if not event.source.strip():return 'source_missing'
    if kind=='alpaca' and event.source.strip().lower()=='alpaca':return 'publisher_missing'
    if kind in {'sec','rss'} and primary is not True:return 'primary_source_unverified'
    if kind=='sec':
        from urllib.parse import urlsplit
        if urlsplit(event.url).hostname!='www.sec.gov' or not event.id.startswith('sec:'):return 'primary_source_unverified'
    if kind=='rss' and primary and not event.id.startswith('rss:'):return 'primary_source_unverified' 
    reason=time_reason(event.published_at,event.received_at,now,max_age_hours)
    if reason:return reason
    if not event.body.strip() or event.body.startswith('Filing metadata only;'):return 'article_body_missing'
    if ' '.join(event.body.split())==' '.join(event.title.split()):return 'title_only_article'
    return ''


def macro_reason(article,now,max_age_hours=72):
    reason=time_reason(article.published_at,article.received_at,now,max_age_hours)
    if reason:return reason
    if not article.source.strip():return 'publisher_missing'
    if not article.body.strip():return 'article_body_missing'
    if ' '.join(article.body.split())==' '.join(article.title.split()):return 'title_only_article'
    from ..macro.sources import official_url
    if article.official:
        if not official_url(article.url):return 'primary_source_unverified'
    elif not (article.publisher_verified and article.llm_use_allowed):return 'unverified_article_identity'
    return ''


def archive(repo,raw,*,provider,reason='',ticker=None,body_allowed=True):
    """기존 뉴스 ID를 유지하며 수신·콘텐츠 버전별 감사 기록을 덧붙인다."""
    data=dict(raw);article_id=data.get('id') or 'unidentified:'+stable_hash(data)[:32]
    content_hash=data.get('content_hash') or stable_hash({k:v for k,v in data.items() if k not in {'received_at','discovered_at'}})
    key='news_first_available:'+stable_hash([article_id,content_hash])
    duplicate=False
    def remember(first):
        nonlocal duplicate
        duplicate=first is not None
        return first if duplicate else data.get('received_at')
    first=repo.update_control(key,remember)
    if not body_allowed:data.pop('body',None)
    payload={'article_id':article_id,'content_hash':content_hash,'provider':provider,'ticker':ticker,
        'published_at':data.get('published_at'),'received_at':data.get('received_at'),'first_available_at':first,
        'eligible_for_llm':not reason,'skip_reason':reason,'duplicate':duplicate,'raw':data,
        'provenance':{'data_provider':provider,'publisher':data.get('source'),
            'primary_source_verified':data.get('official',False)}}
    payload['id']='news-ingestion:'+stable_hash([article_id,content_hash,data.get('received_at'),ticker,reason])
    repo.record('news_ingestions',payload)
    return payload


def age_bucket(published,now):
    if published is None:return '게시시각 미확인'
    try:
        age=(utc(now)-utc(published)).total_seconds()
        if age<0:return '시각 오류'
        if age<=7200:return '발표 후 0~2h'
        if age<=86400:return '발표 후 2~24h'
        if age<=259200:return '거시 24~72h'
        return '과거'
    except (ValueError,TypeError,AttributeError):return '게시시각 미확인'


def statistics(rows,now):
    return {'records':len(rows),'pre_llm_exclusions':dict(Counter(r.get('skip_reason') for r in rows if r.get('skip_reason'))),
        'duplicates':sum(r.get('duplicate',False) for r in rows),
        'llm_candidates':sum(r.get('eligible_for_llm',False) and not r.get('duplicate',False) for r in rows),
        'age_buckets':dict(Counter(age_bucket(r.get('published_at'),now) for r in rows))}
