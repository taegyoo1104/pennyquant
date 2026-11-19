"""Saved AI narration, separate from authoritative code decisions."""
import streamlit as st


def render_explanation_summary(view):
    """Concise saved AI narration for the home; provenance stays in News·LLM."""
    from html import escape
    from .session_app import timestamp
    view = view or {}
    st.markdown('<div class="sq-section"><h2>AI가 읽은 흐름</h2><p>뉴스와 전략을 쉽게 설명합니다</p></div>', unsafe_allow_html=True)
    if view.get('status') == 'SUCCEEDED':
        sections = view.get('sections') or {}
        paragraphs = []
        for key, title in (('market', '시장'), ('strategy', '전략'), ('trades', '매매')):
            text = str((sections.get(key) or {}).get('text') or '').strip()
            if text:
                # Only shorten an existing explanation; never generate a fresh claim.
                short = text[:220] + ('…' if len(text) > 220 else '')
                paragraphs.append('<p><strong>'+title+'</strong> · '+escape(short)+'</p>')
        content = ''.join(paragraphs) or '<p>저장된 설명 본문이 없습니다.</p>'
        detail = '마지막 분석 · 입력 기준 '+timestamp(view.get('as_of'))
        if view.get('synthetic_inputs'):
            detail += ' · 합성 자료 설명'
    else:
        waiting = {
            'NO_KEY': 'AI 연결 설정이 필요합니다.',
            'DAILY_LIMIT': '오늘의 AI 설명 횟수를 모두 사용했습니다.',
            'BUDGET_BLOCKED': '프로그램의 AI 사용 한도로 설명을 잠시 쉬고 있습니다.',
            'INVALID_RESPONSE': 'AI 답변을 검증하지 못해 표시하지 않았습니다.',
            'AUDIT_UNAVAILABLE': 'AI 호출 기록을 읽지 못했습니다.',
            'FAILED': 'AI 요청에 실패했습니다. 뉴스·AI 상세에서 확인할 수 있습니다.',
            'PENDING': 'AI가 설명할 자료를 기다리고 있습니다.',
        }
        message = waiting.get(view.get('status'), '아직 저장된 AI 설명이 없습니다.' if not view else 'AI 분석 상태를 읽지 못했습니다.')
        content = '<p>'+escape(message)+'</p>'
        detail = '설명이 준비되면 여기에 표시합니다.'
    st.markdown('<div class="sq-ai"><h3>AI 브리핑</h3>'+content+'<small>'+escape(detail)+'</small></div>', unsafe_allow_html=True)
    st.caption('AI는 판단을 설명합니다. 매수·매도는 정한 전략과 주문 제한에 따라 결정합니다.')


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
