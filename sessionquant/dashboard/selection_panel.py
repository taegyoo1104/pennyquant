"""저장된 후보 선정만 읽으며 만료된 순위를 현재 순위로 표시하지 않는다."""
from datetime import datetime,timezone

from ..models import utc


def selection_view(frame,now=None):
    now=utc(now or datetime.now(timezone.utc))
    try:active=bool(frame and utc(frame['evaluated_at'])<=now<utc(frame['valid_until']))
    except (KeyError,ValueError,TypeError,AttributeError):active=False
    return {'active':active,'rows':frame.get('rows',[]) if frame else [],
        'final_candidates':frame.get('final_candidate_count',0) if active else None,
        'approved_new':frame.get('approved_new_count',0) if active else None,
        'approved_add':frame.get('approved_add_count',0) if active else None}


def entry_rows(view):
    """Show why each saved candidate stopped, without turning old approval into a live state."""
    from .session_app import timestamp
    from .status_labels import blocker_summary, llm_status_label
    statuses={'BLOCKED':'입력·시장 조건 미충족','SIGNAL':'전략 신호 있음','WAITING':'전략 조건 대기'}
    result=[]
    for row in view['rows']:
        indicators=row.get('indicators') or {}
        reasons=list(dict.fromkeys([*row.get('blockers',[]),
            *indicators.get('strategy_diagnostics',[]),
            *([row['allocation_reason']] if row.get('allocation_reason') else [])]))
        if indicators.get('strategy_diagnostics'):
            reasons=[reason for reason in reasons if reason!='technical_signal_absent']
        pattern=indicators.get('strategy_pattern') or {}
        checks={c['key']:('통과' if c['passed'] else '미충족') for c in pattern.get('checks',[])}
        news=row.get('news_score_signal') or {}
        result.append({'종목':row.get('symbol'),'기록 시각':timestamp(row.get('evaluated_at')),
            '현재성':'현재 평가' if view['active'] else '이전 평가 · 갱신 대기',
            '시장 방향':row.get('regime','UNKNOWN'),'전략 RVOL':indicators.get('relative_volume'),
            '전략 판단':statuses.get(indicators.get('strategy_status'),'이전 버전 · 상세 기록 확인'),
            '상승 추세':checks.get('trend','—'),'조정':checks.get('correction','—'),
            '반등':checks.get('rebound','—'),
            'LLM 분석 상태':llm_status_label(news.get('status','UNKNOWN')),
            'LLM 가점':round(indicators.get('score_components',{}).get('news',0)*10,4),
            'LLM 근거':', '.join(news.get('analysis_ids',[])) or '없음',
            '주문 의도':row.get('order_intent_id') or '없음',
            '주문 판단':row.get('portfolio_status','UNKNOWN'),'대기·차단 이유':blocker_summary(reasons) or '—'})
    return result


def render_selection(frame):
    import streamlit as st
    from .session_app import _table,timestamp
    from .status_labels import blocker_details, blocker_summary
    frame=frame or {};view=selection_view(frame)
    st.subheader('집중 목록 → 최종 후보 → Paper 진입 승인')
    columns=st.columns(3)
    columns[0].metric('현재 최종 후보',str(view['final_candidates'])+' / '+str(frame.get('settings',{}).get('final_candidate_limit',5)) if view['active'] else '—')
    columns[1].metric('이번 선정 신규 승인',str(view['approved_new']) if view['active'] else '—')
    columns[2].metric('이번 선정 추가매수 승인',str(view['approved_add']) if view['active'] else '—')
    st.caption(f"권장 {frame.get('preferred_positions',2)}종목 중심 · 최대 {frame.get('max_positions',5)}종목 · 보유와 매수 예약을 함께 계산합니다. 승인과 체결은 각각 기록합니다.")
    st.caption('평가 시각: '+timestamp(frame.get('evaluated_at'))+' · 호가 기준: '+timestamp(frame.get('quote_anchor_at'))+
        ' · 공통 확정봉: '+timestamp(frame.get('common_closed_bar_at'))+' · 순위 만료: '+timestamp(frame.get('valid_until')))
    if not view['active']:st.caption('현재 순위는 미확인입니다. 아래 내용은 마지막 저장된 선정 이력입니다.')
    if frame.get('synthetic'):st.caption('합성 후보 선정 검사 자료이며 실제 종목 순위·매매 성과가 아닙니다.')
    _table(entry_rows(view),'집중 후보의 매수 판단 기록이 없습니다.')
    rows=[]
    for row in view['rows']:
        news=row.get('news_score_signal') or {};sector=row.get('sector_strength') or {}
        status=row.get('indicators',{}).get('score_component_status',{})
        rows.append({'기록 순위':row.get('rank'),'종목':row.get('symbol'),'후보 점수':row.get('candidate_score'),
            '기업 뉴스':status.get('news','UNKNOWN'),'뉴스 값':news.get('value') if status.get('news')=='VERIFIED' else None,
            '업종':row.get('sector'),'업종 비교':status.get('sector','UNKNOWN'),
            '업종 값':sector.get('value') if status.get('sector')=='VERIFIED' else None,
            '동일 업종 노출':', '.join(row.get('exposure_overlap',{}).get('same_sector_symbols',[])),
            '높은 지수 상관 노출':', '.join(row.get('exposure_overlap',{}).get('high_index_correlation_symbols',[])),
            '포트폴리오 결과':row.get('portfolio_status'),'차단·거절':blocker_summary([*row.get('blockers',[]),*([row['allocation_reason']] if row.get('allocation_reason') else [])]),
            '호가 발생':timestamp(row.get('quote_at')),'확정봉':timestamp(row.get('indicators',{}).get('last_closed_at')),
            '주문 의도':row.get('order_intent_id')})
    with st.expander('후보 점수·뉴스·업종 상세'):
        _table(rows,'집중 후보의 동일 시점 선정 기록이 없습니다.')
    with st.expander('대기 이유 구분해서 보기'):
        reasons = []
        for row in view['rows']:
            codes = [*row.get('blockers', []), *(row.get('indicators') or {}).get('strategy_diagnostics', [])]
            if row.get('allocation_reason'):
                codes.append(row['allocation_reason'])
            for reason in blocker_details(codes):
                detail = {'종목': row.get('symbol'), '구분': reason['category'], '대기 이유': reason['label']}
                # Internal identifiers belong only in explicitly enabled diagnostics.
                if st.session_state.get('dashboard_raw_data', False):
                    detail['사유 코드'] = reason['code']
                reasons.append(detail)
        if reasons:
            st.dataframe(reasons, hide_index=True, width='stretch')
        else:
            st.caption('저장된 대기 사유가 없습니다. 매수 승인을 뜻하지는 않습니다.')
    st.caption('미확인 뉴스·업종은 가점 0이며 검증된 중립과 구분합니다. 시장 참고 ETF와 거시 LLM 해설은 종목 가점에 포함하지 않습니다.')
    if frame and st.session_state.get('dashboard_raw_data', False):
        with st.expander('선택 근거·뉴스/업종 입력·시장 위험 상세'):
            st.json(frame)
