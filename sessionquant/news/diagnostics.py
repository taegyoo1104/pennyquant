"""수집·생성 요청·응답 검증을 구분하는 읽기 전용 요약. 평가 정확도를 추정하지 않는다."""
from collections import Counter
from datetime import datetime, timedelta, timezone

from ..models import utc


def recent_stamp(value, now, seconds=86400):
    try:return bool(value and 0 <= (now-utc(value)).total_seconds() <= seconds)
    except (ValueError,TypeError,AttributeError):return False


def summarize_news(ingestions, calls, *, schedule=None, health=None, engine=None, now=None,
                   ingestion_limit=2000, audit_limit=500, network_calls=None):
    now=utc(now or datetime.now(timezone.utc))
    schedule,health,engine=schedule or {},health or {},engine or {}
    receipts=[r for r in ingestions if recent_stamp(r.get('received_at'),now)]
    visible=[r for r in calls if recent_stamp(r.get('request_started_at') or r.get('recorded_at'),now)]
    request_rows=visible if network_calls is None else [r for r in network_calls
        if recent_stamp(r.get('request_started_at') or r.get('recorded_at'),now)]
    attempts=[r for r in request_rows if r.get('network_attempted') is True and r.get('llm_call_id')]
    # The precheck window and actual-request window are independent. Count a
    # request only once even when it also appears in the recent audit rows.
    audit_rows=[r for r in visible if not (r.get('network_attempted') is True and r.get('llm_call_id'))]+attempts
    success=[r for r in attempts if r.get('status')=='SUCCEEDED']
    pending=[r for r in attempts if r.get('status') in {'REQUESTED','RESPONSE_RECEIVED','OUTCOME_UNKNOWN'}]
    excluded=Counter(r.get('skip_reason') for r in receipts if r.get('skip_reason'))
    providers=[]
    for name,row in sorted(schedule.items()):
        state=row.get('status','UNKNOWN')
        if recent_stamp(row.get('last_attempt_at'),now,300) and row.get('in_flight_until'):
            try:
                if utc(row['in_flight_until'])>now:state='IN_PROGRESS'
            except (ValueError,TypeError):pass
        providers.append({'provider':name,'state':state,'last_attempt_at':row.get('last_attempt_at'),
            'last_success_at':row.get('last_success_at'),'last_count':row.get('last_count'),
            'last_error':row.get('last_error'),'retry_after':row.get('cooldown_until')})
    latest=[]
    for row in sorted(attempts,key=lambda r:utc(r.get('request_started_at') or r['recorded_at']),reverse=True)[:5]:
        result=row.get('normalized_result') or {}
        latest.append({'article_id':row.get('article_id'),'call_id':row['llm_call_id'],
            'ticker_or_topic':row.get('ticker') or row.get('macro_topic'),'status':row.get('status'),
            'requested_at':row.get('request_started_at'),'response_received_at':row.get('response_received_at'),
            'model':row.get('model'),'input_tokens':row.get('input_tokens'),'output_tokens':row.get('output_tokens'),
            'direction':result.get('sentiment',result.get('direction','UNKNOWN')),
            'event_types':[e.get('type') for e in result.get('events',[])] or ([result['event_type']] if result.get('event_type') else []),
            'company_impacts':[e.get('company_impact') for e in result.get('events',[])],
            'shareholder_impacts':[e.get('shareholder_impact') for e in result.get('events',[])],
            'event_statuses':[e.get('status') for e in result.get('events',[])] or ([result['status']] if result.get('status') else []),
            'reason':result.get('reason'),'requires_verification':result.get('requires_verification'),
            'evidence':[e.get('evidence') for e in result.get('events',[])] or ([result['evidence']] if result.get('evidence') else []),
            'error':row.get('validation_error_code') or result.get('error'),
            'failed_field':row.get('failed_field') or result.get('failed_field'),
            'article_url':row.get('article_url')})
    if success:message='LLM 응답 검증을 통과한 기록이 있습니다. 분류 결과와 원문 근거를 대조하세요.'
    elif attempts:message='LLM 요청 시도는 있으나 조회 범위에 검증 성공이 없습니다. 호출 상태·오류를 확인하세요.'
    elif health.get('code')=='NO_KEY':message='OpenAI 키가 없어 LLM 생성 요청을 하지 못합니다.'
    elif receipts:message='뉴스 수신 기록은 있지만 생성 요청은 없습니다. 제외 사유·캐시·예산 상태를 확인하세요.'
    elif providers:message='공급자 작업 기록은 있지만 최근 뉴스 수신 기록은 없습니다. 최근 성공·새 기사 없음·오류를 구분하세요.'
    else:message='뉴스 수집·분석 기록이 없습니다. 실행 설정과 조회 DB가 같은지 확인하세요.'
    return {'observed_at':now.isoformat(),'message':message,
        'scope':{'since':(now-timedelta(hours=24)).isoformat(),'basis':'최근 24시간 수신/요청 기록 중 조회 범위',
                 'ingestion_limit':ingestion_limit,'audit_limit':audit_limit,
                 'request_limit':audit_limit,
                 'requests_may_be_truncated':len(calls if network_calls is None else network_calls)>=audit_limit,
                 'may_be_truncated':len(ingestions)>=ingestion_limit or len(calls)>=audit_limit},
        'engine':{'status':engine.get('status','NOT_STARTED'),
            'connected':bool(engine.get('armed') and recent_stamp(engine.get('heartbeat'),now,60)),
            'heartbeat':engine.get('heartbeat')},
        'counts':{'news_receipts':len(receipts),'unique_articles':len({r['article_id'] for r in receipts if r.get('article_id')}),
            'llm_request_attempts':len(attempts),'validated_responses':len(success),
            'pending_or_unconfirmed':len(pending),'failed_or_rejected':len(attempts)-len(success)-len(pending),
            'cache_hits':sum(r.get('status')=='CACHE_HIT' for r in visible)},
        'last_news_received_at':max((r['received_at'] for r in receipts),key=utc,default=None),
        'last_llm_request_at':max((r.get('request_started_at') or r.get('recorded_at') for r in attempts),key=utc,default=None),
        'pre_llm_exclusions':dict(excluded),'audit_status_counts':dict(Counter(r.get('status','UNKNOWN') for r in audit_rows)),
        'llm_health':{k:health.get(k) for k in ('code','message','updated_at','retry_after')},
        'providers':providers,'latest_calls':latest,
        'classification_accuracy':'NOT_MEASURED','read_only':True,'provider_calls':0,'llm_generation_calls':0,'order_writes':0}


def read_news_status(repo, now=None):
    return summarize_news(repo.recent('news_ingestions',limit=2000),repo.recent('llm_calls',limit=500),
        network_calls=repo.recent_llm_attempts(limit=500),
        schedule=repo.get_control('news_provider_schedule_v1',{}),health=repo.get_control('llm_provider_health',{}),
        engine=repo.get_control('trade_control_state',{}),now=now)
