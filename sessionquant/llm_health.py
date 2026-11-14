"""LLM 연결 장애는 뉴스 분석만 제한한다. 충전 메모로 거래를 차단하지 않는다."""
from datetime import datetime,timedelta,timezone
from hashlib import sha256

CREDIT_CODES={'insufficient_quota','credit_balance_exhausted','billing_hard_limit_reached','billing_not_active'}
MESSAGES={'NO_CREDIT':'OpenAI 잔액·사용 한도가 부족합니다. 차트 시그널 기반 매매와 위험 관리는 계속합니다.',
    'NO_KEY':'OpenAI 키가 없습니다. 차트 시그널 기반 매매와 위험 관리는 계속합니다.',
    'AUTH_ERROR':'OpenAI 인증·권한 오류입니다. 차트 기반 처리는 계속합니다.',
    'RATE_LIMIT':'OpenAI 요청 제한입니다. 차트 기반 처리는 계속합니다.',
    'TEMPORARY_ERROR':'OpenAI 연결 장애입니다. 차트 기반 처리는 계속합니다.',
    'AVAILABLE':'OpenAI 분석 연결이 정상입니다.','UNKNOWN':'OpenAI 생성 연결 상태는 아직 미확인입니다.'}


def fingerprint(key): return sha256(key.encode()).hexdigest()[:16] if key else ''


def set_health(repo,code,*,now=None,key='',retry_seconds=0):
    now=now or datetime.now(timezone.utc)
    old=repo.get_control('llm_provider_health',{})
    state={'code':code,'message':MESSAGES[code],'updated_at':now.isoformat(),
        'retry_after':(now+timedelta(seconds=retry_seconds)).isoformat(),
        'credential_fingerprint':fingerprint(key),'technical_only':code!='AVAILABLE'}
    repo.set_control('llm_provider_health',state)
    if code not in {'AVAILABLE','UNKNOWN'} and old.get('code')!=code:
        repo.record('alerts',{'id':'llm-health-'+code+'-'+now.date().isoformat(),'timestamp':now,
            'severity':'warning','code':'llm_'+code.lower(),'message':state['message']})
        print(state['message'],flush=True)
    return state


def response_health(repo,response,*,key,now):
    if response.is_success: return set_health(repo,'AVAILABLE',now=now,key=key)
    error={}
    try:
        body=response.json()
        if isinstance(body,dict) and isinstance(body.get('error'),dict): error=body['error']
    except ValueError: pass
    if error.get('code') in CREDIT_CODES or error.get('type') in CREDIT_CODES:
        return set_health(repo,'NO_CREDIT',now=now,key=key,retry_seconds=600)
    if response.status_code in (401,403): return set_health(repo,'AUTH_ERROR',now=now,key=key,retry_seconds=600)
    if response.status_code==429: return set_health(repo,'RATE_LIMIT',now=now,key=key,retry_seconds=60)
    return set_health(repo,'TEMPORARY_ERROR',now=now,key=key,retry_seconds=60)
