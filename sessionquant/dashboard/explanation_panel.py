"""Saved AI narration, separate from authoritative code decisions."""
import re
from html import escape

import streamlit as st

from .labels import LABELS

_STRATEGY_CODE = re.compile(
    r'(?<![A-Za-z0-9_])(?:pullback_simple|pullback|momentum|gap_breakout|vwap)(?![A-Za-z0-9_])'
)
SESSION_WAITS = {'PREOPEN', 'POSTCLOSE', 'SESSION_WAIT'}


def missing_strategy_decision(view):
    """Only an explicit empty-decision fact supports the recorded-state fallback."""
    facts = view.get('facts')
    rows = facts.get('strategy') if isinstance(facts, dict) else None
    return (isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict)
            and rows[0].get('id') == 'strategy:none'
            and rows[0].get('status') == 'NO_RECORDED_DECISION')


def explanation_text(view, key):
    """Present saved narration without internal codes or invented missing-record causes."""
    if key == 'strategy' and missing_strategy_decision(view):
        return '입력 기준 시점에 기록된 전략 판단이 없습니다. 이 기록만으로는 매수 대기 이유를 알 수 없습니다.'
    text = str(((view.get('sections') or {}).get(key) or {}).get('text') or '').strip()
    # Translate only strategy IDs: words like LOW may be stock tickers, not statuses.
    text = _STRATEGY_CODE.sub(lambda m: LABELS[m.group()], text)
    return text.replace('종이 거래 모드', '모의매매').replace('종이 거래', '모의매매')


def explanation_items(text):
    """Keep all saved words; turn older single-paragraph narration into readable items."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) == 1:
        # Decimal prices and quantities have no space after the dot. Do not
        # reformat their precision or infer which trades/news belong together.
        lines = re.split(r'(?<=[.!?])\s+', lines[0])
    return [re.sub(r'^(?:[-•]|\d+[.)])\s+', '', line) for line in lines]


def explanation_section(view, key, title):
    """Escaped, unabridged narration shared by the home and detailed views."""
    items = explanation_items(explanation_text(view, key))
    if not items:
        return ''
    rows = ''.join('<li>'+escape(item)+'</li>' for item in items)
    return ('<section class="sq-ai-section"><h4>'+escape(title)+'</h4>'
            '<ul class="sq-ai-items">'+rows+'</ul></section>')


def explanation_status(view):
    """Use bounded reason codes, never raw provider errors or credentials."""
    if view.get('status') == 'PREOPEN':
        from .session_app import timestamp
        return '개장 대기 · '+timestamp(view.get('opens_at'))+'부터 새 AI 브리핑을 준비합니다.'
    if view.get('status') == 'BUDGET_BLOCKED':
        return {
            'HOURLY_CALL_CAP': '시간당 AI 호출 한도에 도달했습니다. 다음 UTC 정시 이후 다시 확인합니다.',
            'DAILY_CALL_CAP': '오늘의 AI 호출 한도에 도달했습니다. 한국시간 오전 9시 이후 다시 확인합니다.',
            'MONTHLY_COST_CAP': '프로그램의 월 AI 금액 한도에 도달했습니다. OpenAI 충전 잔액과는 별개입니다.',
            'PROVIDER_COOLDOWN': 'AI 제공 서비스의 오류·요청 제한으로 재시도를 기다립니다.',
        }.get(view.get('limit_reason'), 'AI 호출·금액 한도 또는 제공 서비스의 대기 제한에 걸렸습니다. 이전 기록에는 상세 사유가 없습니다.')
    return {
        'POSTCLOSE': '정규장이 끝나 새 AI 브리핑을 쉬고 있습니다. 다음 정규장에 다시 준비합니다.',
        'SESSION_WAIT': '정규장 일정 확인 대기 · 정규장 시간이 확인되면 새 AI 브리핑을 준비합니다.',
        'NO_KEY': 'AI 연결 설정이 필요합니다.',
        'DAILY_LIMIT': '오늘의 AI 설명 횟수를 모두 사용했습니다. 한국시간 오전 9시 이후 다시 확인합니다.',
        'INVALID_RESPONSE': '새 AI 답변을 검증하지 못했습니다.',
        'AUDIT_UNAVAILABLE': 'AI 호출 기록을 읽지 못했습니다.',
        'FAILED': '새 AI 요청에 실패했습니다. 뉴스·AI 상세에서 확인할 수 있습니다.',
        'PENDING': 'AI가 설명할 자료를 기다리고 있습니다.',
    }.get(view.get('status'), '아직 저장된 AI 설명이 없습니다.' if not view else 'AI 분석 상태를 읽지 못했습니다.')


def saved_explanation(view):
    if view.get('status') == 'SUCCEEDED':
        return view
    saved = view.get('last_success') or {}
    return saved if saved.get('status') == 'SUCCEEDED' else {}


def render_previous_attempt(view):
    from .session_app import timestamp
    previous = view.get('previous_attempt') or {}
    if view.get('status') in SESSION_WAITS and previous:
        st.caption('이전 분석 시도 · '+timestamp(previous.get('as_of'))+' · '+explanation_status(previous)+
                   ' 현재 한도 상태를 다시 확인한 결과는 아닙니다.')


def render_explanation_summary(view):
    """Retain the last successful narration while exposing refresh failures."""
    from .session_app import timestamp
    view = view or {}
    saved = saved_explanation(view)
    st.markdown('<div class="sq-section"><h2>AI가 읽은 흐름</h2><p>뉴스와 전략을 쉽게 설명합니다</p></div>', unsafe_allow_html=True)
    if saved:
        content = ''.join(explanation_section(saved, key, title) for key, title in
                          (('market', '시장 뉴스'), ('strategy', '전략 상태'), ('trades', '최근 모의매매')))
        content = content or '<p>저장된 설명 본문이 없습니다.</p>'
        detail = '마지막 분석 · 입력 기준 '+timestamp(saved.get('as_of'))
        if saved.get('synthetic_inputs'):
            detail += ' · 합성 자료 설명'
    else:
        content = '<p>'+escape(explanation_status(view))+'</p>'
        detail = ('장전에는 새 AI 브리핑을 요청하지 않습니다. 뉴스 기사 수집은 별도로 진행합니다.'
                  if view.get('status') == 'PREOPEN' else '새 설명이 준비되면 표시합니다.')
    st.markdown('<div class="sq-ai"><h3>AI 브리핑</h3>'+content+'<small>'+escape(detail)+'</small></div>', unsafe_allow_html=True)
    if saved and view.get('status') != 'SUCCEEDED':
        st.caption('새 분석 대기 · '+explanation_status(view)+' 위 내용은 이전 분석 시점의 설명입니다.')
    if saved and missing_strategy_decision(saved):
        st.caption('전략 판단 기록이 없어 해당 부분은 AI의 원인 추정 대신 기록 상태를 표시합니다.')
    render_previous_attempt(view)
    st.caption('AI는 판단을 설명합니다. 매수·매도는 정한 전략과 주문 제한에 따라 결정합니다.')


def render_explanation(view):
    if not view:return
    from .session_app import timestamp
    with st.expander('AI 설명 · 시장 / 전략 판단 / 모의 체결',expanded=True):
        if view.get('status')!='SUCCEEDED':
            st.caption(('대기 상태 확인 · ' if view.get('status') in SESSION_WAITS else '새 분석 시도 · 입력 시점 ')+timestamp(view.get('as_of')))
            st.info(explanation_status(view))
            render_previous_attempt(view)
            view = saved_explanation(view)
            if not view:
                return
            st.caption('이전 분석 시점의 설명을 유지하고 있습니다.')
        st.caption('입력 기준 '+timestamp(view.get('as_of'))+' · 저장된 시점의 AI 설명이며 주문을 결정하지 않습니다.')
        if view.get('synthetic_inputs'):st.info('합성 자료에 대한 설명 · 실제 시장 상황이 아닙니다.')
        st.caption('생성 '+timestamp(view.get('generated_at'))+' · 모델 '+str(view.get('model'))+
                   ' · 실제 호출 '+', '.join(view.get('llm_call_ids',[])))
        for key,label in [('market','시장 상황'),('strategy','전략 판단'),('trades','매매 결과')]:
            part=view.get('sections',{}).get(key,{})
            content = explanation_section(view, key, label)
            st.markdown('<div class="sq-ai">'+content+'</div>' if content else '**'+label+'** · 미확인',
                        unsafe_allow_html=True)
            st.caption('근거: '+', '.join(part.get('evidence_ids',[])))
        if missing_strategy_decision(view):
            st.caption('전략 판단 기록이 없어 해당 부분은 AI의 원인 추정 대신 기록 상태를 표시합니다.')
        with st.expander('설명에 사용한 기록'):
            st.json(view.get('facts',{}))
            st.caption('저장된 AI 원문 · 화면의 쉬운 이름과 기록 상태 안내를 적용하기 전입니다.')
            st.json(view.get('sections',{}))
