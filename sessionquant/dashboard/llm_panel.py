"""호출 목록·정제 결과·제한된 감사 본문을 읽고 비용 메모를 구분한다."""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import stat

from ..llm.audit import redact
from ..models import utc


def filter_calls(rows,*,day=None,model=(),stage=(),status=(),query=''):
    result=[]
    for row in rows:
        stamp=row.get('request_started_at') or row.get('recorded_at','')
        if day and not stamp.startswith(str(day)):continue
        if model and row.get('model') not in model:continue
        if stage and row.get('stage') not in stage:continue
        if status and row.get('status') not in status:continue
        if query.lower() not in (' '.join(str(row.get(key) or '') for key in ('ticker','macro_topic','article_id','reference_event_id'))).lower():continue
        result.append(row)
    return result


def read_saved_body(repo,call_id,now=None):
    if os.getenv('SQ_LLM_AUDIT_ALLOW_READ')!='1':return None
    policy=repo.get_control('llm_audit_policy_v1',{}) or {}
    if not policy.get('enabled') or policy.get('environment')!='PAPER_LOCAL':return None
    path=Path(policy['key_file'])
    if not path.is_file() or path.is_symlink() or (os.name!='nt' and stat.S_IMODE(path.stat().st_mode)&0o077):return None
    row=repo.get_record('llm_audit_bodies',call_id)
    if not row or utc(row['expires_at'])<=utc(now or datetime.now(timezone.utc)):return None
    from cryptography.fernet import Fernet
    return redact(json.loads(Fernet(path.read_bytes().strip()).decrypt(row['ciphertext'].encode())))


def render_llm_details(path,*,usage_path=None,usage_environment=None):
    import streamlit as st
    from .labels import label
    from .session_app import usd,timestamp,_table
    from .summary_panels import open_cost_repository,cost_summary
    from ..llm.costs import set_manual_credit
    from ..portfolio.models import decimal
    repo=None
    try:
        repo=open_cost_repository(usage_path or path,environment=usage_environment)
        if repo is None:st.info('LLM 감사 원장이 없습니다.');return
        summary=cost_summary(repo)
        cols=st.columns(3)
        cols[0].metric('프로그램 누적 비용 · 추정',usd(summary['spent']))
        cols[1].metric('OpenAI 충전 누계 · 직접 기록',usd(summary['deposited']))
        cols[2].metric('공식 조직 비용 · 별도 범위',usd(summary['official_organization_cost_usd']))
        st.caption('프로그램 비용은 토큰·설정 단가의 추정이며 공식 청구액이 아닙니다. 자체 예산은 결제 강제 상한이 아닙니다.')
        st.caption('수동 확인 '+timestamp(summary['manual_checked_at'])+' · 확인 잔액 '+usd(summary['manual_balance_usd'])+
            ' · 이후 자체 비용 '+usd(summary['local_cost_since_check_usd'])+' · 추정 잔액 '+usd(summary['estimated_credit_usd']))
        st.caption('실시간 선불 잔액 API는 연결하지 않았습니다. 다른 앱 사용량·조정액은 추정에 포함되지 않습니다.')
        from ..dashboard_mode import inspection_mode
        if inspection_mode():
            st.caption('점검 모드에서는 저장된 비용 기록만 조회합니다.')
        else:
            with st.expander('수동 잔액·충전 메모'):
                value=st.text_input('OpenAI에 충전한 총 달러 금액',value=str(summary['deposited']) if summary['deposited'] is not None else '',key='openai-deposit')
                if st.button('충전 누계 저장',key='save-openai-deposit'):
                    try:
                        from ..dashboard_mode import require_writable_dashboard
                        require_writable_dashboard()
                        amount=decimal(value,nonnegative=True)
                        repo.set_control('openai_funding_note',{'total_usd':str(amount),'updated_at':datetime.now(timezone.utc).isoformat(),'source':'user_entered'})
                        st.success('수동 충전 메모를 저장했습니다. 실제 결제는 하지 않았습니다.');st.rerun()
                    except ValueError:st.error('0 이상의 금액을 입력하세요.')
                balance=st.text_input('마지막 확인 크레딧 잔액 USD',key='manual-credit-balance')
                checked=st.text_input('확인 시각 ISO 8601 · 시간대 필수',key='manual-credit-at')
                if st.button('수동 확인 잔액 저장',key='manual-credit-save'):
                    try:
                        from ..dashboard_mode import require_writable_dashboard
                        require_writable_dashboard()
                        set_manual_credit(repo,balance,checked)
                        st.success('수동 확인 기준만 저장했습니다.');st.rerun()
                    except (ValueError,TypeError,AttributeError):st.error('금액과 과거의 시간대 포함 확인 시각을 입력하세요.')
        rows=repo.recent('llm_calls',limit=500)
        st.caption('최근 최대 500개 감사 기록 기준입니다. 전체 집계는 진단 SQL을 사용하세요.')
        columns=st.columns(3)
        model=columns[0].multiselect('모델',[value for value in sorted({r.get('model','') for r in rows if r.get('model')})],key='llm-filter-model')
        stage=columns[1].multiselect('분석 단계',sorted({r.get('stage','') for r in rows if r.get('stage')}),format_func=label,key='llm-filter-stage')
        status=columns[2].multiselect('처리 결과',sorted({r.get('status','UNKNOWN') for r in rows}),format_func=label,key='llm-filter-status')
        day=st.date_input('호출일 UTC',value=None,key='llm-filter-date')
        query=st.text_input('종목·주제·기사 ID 검색',key='llm-filter-query')
        visible=filter_calls(rows,day=day,model=model,stage=stage,status=status,query=query)
        _table([{'호출 ID':r.get('llm_call_id') or '요청 없음', '기사·사건':r.get('article_id') or r.get('reference_event_id'),
            '모델':r.get('model'),'단계':r.get('stage'),'상태':r.get('status'),'시각':timestamp(r.get('request_started_at') or r.get('recorded_at')),
            '입력 토큰':r.get('input_tokens'),'출력 토큰':r.get('output_tokens'),'비용 USD':r.get('cost_usd'),
            '비용 구분':r.get('cost_status'),'오류':r.get('validation_error_code') or r.get('skipped_precheck_reason'),
            '소요 ms':r.get('latency_ms')} for r in visible])
        by_id={row['id']:row for row in visible}
        selected=st.selectbox('분석 기록 자세히',[None,*reversed(list(by_id))],format_func=lambda key:'선택하세요' if key is None else
            key[:12]+' · '+label(by_id[key].get('status','UNKNOWN')),key='llm-call-detail')
        if selected:
            row=by_id[selected]
            st.json(redact(row))
            st.caption('과거 미보존 원본은 해시·토큰 수만으로 복원할 수 없습니다. 요청 옵션/스키마와 정제 결과를 구분하세요.')
            if st.checkbox('허용되어 보관된 감사 원문 보기',key='llm-show-saved-body'):
                body=read_saved_body(repo,row.get('llm_call_id') or '')
                if body is None:st.info('원문 미보존·만료 또는 로컬 읽기 권한 미설정입니다.')
                else:st.json(body)
        legacy=[row for row in repo.recent('llm_usage',limit=2000) if not row.get('llm_call_id')]
        if legacy:
            st.caption('이전 버전 사용량 '+str(len(legacy))+'건: 기사/호출 매핑 불가. 추정 연결하지 않습니다.')
            _table([{'모델':r.get('model'),'시각':timestamp(r.get('timestamp')),'입력':r.get('input_tokens'),
                '출력':r.get('output_tokens'),'비용 USD':r.get('cost_usd'),'연결 상태':'LEGACY_UNMAPPED'} for r in legacy])
        contexts=repo.recent('macro_context_analysis',limit=100)
        if contexts:
            with st.expander('이벤트 기반 거시 맥락 분석'):_table([{'사건':r.get('reference_event_id'),'상태':r.get('status'),
                '제외 사유':r.get('skipped_precheck_reason'),'오류':r.get('validation_error_code'),'유료 요청':r.get('llm_requested'),
                '호출 ID':', '.join(r.get('llm_call_ids',[]))} for r in contexts])
    except Exception:st.warning('LLM 감사 상세 조회 미확인. 인증정보나 원문 오류를 출력하지 않습니다.')
    finally:
        if repo:repo.close()
