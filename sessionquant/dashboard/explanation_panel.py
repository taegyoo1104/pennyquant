"""Saved AI narration, separate from authoritative code decisions."""
import streamlit as st


def render_explanation(view):
    if not view:return
    from .session_app import timestamp
    with st.expander('AI 설명 · 시장 / 전략 판단 / 모의 체결',expanded=True):
        st.caption('입력 기준 '+timestamp(view.get('as_of'))+' · 저장된 시점의 AI 설명이며 주문을 결정하지 않습니다.')
        if view.get('synthetic_inputs'):st.info('합성 자료에 대한 설명 · 실제 시장 상황이 아닙니다.')
        labels={'NO_KEY':'API 키 없음','DAILY_LIMIT':'설명 일일 시도 한도 도달',
                'BUDGET_BLOCKED':'공유 LLM 예산·요청 제한','INVALID_RESPONSE':'응답 형식·근거 검증 실패',
                'AUDIT_UNAVAILABLE':'호출 기록 확인 실패','FAILED':'요청 실패','PENDING':'분석 대기'}
        if view.get('status')!='SUCCEEDED':
            st.info(labels.get(view.get('status'),'설명 미확인')+' · 규칙 기반 매매는 계속합니다.')
            return
        st.caption('생성 '+timestamp(view.get('generated_at'))+' · 모델 '+str(view.get('model'))+
                   ' · 실제 호출 '+', '.join(view.get('llm_call_ids',[])))
        for key,label in [('market','시장 상황'),('strategy','전략 판단'),('trades','매매 결과')]:
            part=view.get('sections',{}).get(key,{})
            st.markdown('**'+label+'**')
            st.text(part.get('text','미확인'))
            st.caption('근거: '+', '.join(part.get('evidence_ids',[])))
        with st.expander('설명에 사용한 기록'):
            st.json(view.get('facts',{}))
