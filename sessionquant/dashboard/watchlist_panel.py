"""감시·집중·진입 신호와 과거 분석 기록을 분리해서 표시한다."""
from datetime import datetime, timezone
from ..models import utc

LABELS = {'VALID':'유효', 'UNKNOWN':'미확인', 'PARTIAL':'자료 일부 미확인', 'EXPIRED':'만료',
          'MEMBERSHIP_UNKNOWN':'편입 정보 미확인', 'MEMBERSHIP_CHANGED':'편입 목록 변경', 'SYNTHETIC_BLOCKED':'합성 자료 차단'}


def current_status(state, now=None):
    now = now or datetime.now(timezone.utc)
    status = state.get('status', 'UNKNOWN')
    snapshot = state.get('snapshot') or {}
    try:
        if snapshot:
            as_of = utc(snapshot['as_of_at'])
            if as_of > now:
                return 'UNKNOWN', False
            if (now-as_of).total_seconds() > state.get('max_age_days',7)*86400 or (snapshot.get('valid_until') and now>=utc(snapshot['valid_until'])):
                status = 'EXPIRED'
        updated = state.get('updated_at')
        active = bool(updated and 0 <= (now-utc(updated)).total_seconds() <= state.get('data_max_age_seconds',120))
    except (ValueError, TypeError, KeyError, AttributeError):
        return 'UNKNOWN', False
    return status, active


def render_watchlist(state, candidates):
    import streamlit as st
    from .session_app import timestamp, usd, _table
    state = state or {}
    now = datetime.now(timezone.utc)
    status, active = current_status(state, now)
    items = state.get('items', [])
    focus = {row['ticker'] for row in items if row.get('focused') and row.get('focus_valid_until')
             and utc(row['focus_valid_until']) >= now} if active and status == 'VALID' else set()
    signals = [row for row in candidates if row.get('symbol') in focus and row.get('eligible') and row.get('signal')
               and row.get('timestamp') and 0 <= (now-utc(row['timestamp'])).total_seconds() <= 120]
    st.subheader(f"시총 감시 {state.get('watchlist_limit',50)} → 집중 분석 최대 {state.get('focus_limit',20)}")
    cols = st.columns(5)
    cols[0].metric('S&P 500 전체', str(state.get('universe_count')) if state.get('universe_count') else '—')
    cols[1].metric('유효 감시 종목', f"{len(items) if status=='VALID' else 0} / {state.get('watchlist_limit',50)}")
    expired = len(items) if status == 'EXPIRED' else 0
    unknown = state.get('watchlist_limit',50) if status not in {'VALID','EXPIRED'} else 0
    cols[2].metric('만료 / 미확인 상한', f'{expired} / {unknown}')
    st.caption('선정 상태: '+LABELS.get(status,status)+' · 미확인 수는 선정 상한 기준이며 확정 종목 수가 아닙니다.')
    cols[3].metric('현재 집중 분석', f"{len(focus)} / {state.get('focus_limit',20)}")
    cols[4].metric('현재 진입 신호', str(len(signals)))
    if (state.get('snapshot') or {}).get('synthetic'):
        st.warning('합성 감시목록 검증 자료입니다. 실제 시총 순위·시장 성과가 아닙니다.')
    if status != 'VALID':
        st.warning('검증된 최신 시총·편입 목록이 필요합니다. 현재 자료로 신규 종목을 임의 선정하지 않습니다.')
    if not active:
        st.caption('현재 조회 상태는 미확인입니다. 아래 항목은 마지막 저장 기록이며 과거 집중 분석을 현재 상태로 표시하지 않습니다.')
    refresh = state.get('refresh') or {}
    if refresh.get('status') in {'error','partial'}:
        st.info(refresh.get('message','시총 공급 자료 확인이 필요합니다.'))
    source = state.get('source_state') or {}
    st.caption(f"마지막 수집: {timestamp(state.get('updated_at'))} · 주기 읽기 호출: {source.get('last_cycle_calls','—')} / {source.get('cycle_call_budget','—')} · OAuth 별도 · 실측 주기 {source.get('last_cycle_duration_seconds','—')}초")
    if source.get('budget_exhausted'):
        st.warning('시세 조회 예산을 소진했습니다. 조회가 늦어진 종목은 최신성 검사에서 차단됩니다.')
    if source.get('backoff_until'):
        st.caption('공급자 요청 제한 대기 시각: '+timestamp(source['backoff_until']))
    snapshot=state.get('snapshot') or {}
    if snapshot.get('currency')=='KRW':
        st.caption('토스 보고 종목 시총(원화 환산·일별 갱신). 주식 클래스별 집계이며 발행사 합산이 아닙니다. 공급자의 정확한 평가 시각은 미제공입니다.')
    rows = []
    for row in items:
        last = row.get('last_detail_at')
        age = max(0, int((now-utc(last)).total_seconds())) if last and utc(last) <= now else None
        blockers = list(row.get('entry_blockers',[]))
        if status != 'VALID': blockers.append('watchlist_'+status.lower())
        if row['ticker'] not in focus: blockers.append('focus_unknown_or_stale')
        rows.append({'종목':row['ticker'], '시총 순위':row.get('rank') if status == 'VALID' else None, '검증자료 내 순위':row.get('rank'), '시가총액':(row.get('market_cap_value')+' KRW') if row.get('currency')=='KRW' and row.get('market_cap_value') else usd(row.get('market_cap_usd')),
            '선정 상태':LABELS.get(status,status),'시총 선택 이유':row.get('selection_reason'),
            '현재 집중':row['ticker'] in focus, '집중 근거':row.get('focus_reason'),
            '시총 기준일/수집 시작':timestamp(row.get('as_of_at')), '시총 수신일':timestamp(row.get('received_at')),
            '알려진 시각':timestamp(row.get('known_at')), '최종 상세 조회':timestamp(last),
            '최근 조회 시도':timestamp(row.get('last_detail_attempt_at')), '상세 자료 지연(초)':age,
            '신규 진입 차단':', '.join(dict.fromkeys(blockers)) if active else '현재 수집 상태 미확인'})
    _table(rows, '시가총액 공급 자료를 확인한 뒤 감시·집중 후보를 표시합니다.')
    st.caption('관찰 점수는 매수 승인이 아닙니다. RVOL·계좌 거래 가능·소수점 지원·확정봉·시장·손실 제한은 기존 검사를 별도로 통과해야 합니다. 보유·미체결·청산 예정 및 SPY·QQQ는 이 상한과 별도로 관리합니다.')
