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


def focus_is_monitored(row, now):
    """Collection membership for display only; this never grants entry eligibility."""
    try:
        deadline = row.get('focus_monitor_until') or row.get('focus_valid_until')
        return bool(row.get('focused') and deadline and utc(deadline) > now)
    except (ValueError, TypeError, AttributeError):
        return False


def history_problem_text(problems):
    parts = []
    for problem in problems:
        if 'missing_minutes' in problem and 'conflicting_minutes' in problem:
            detail = f"누락 {problem['missing_minutes']}분 · 값 충돌 {problem['conflicting_minutes']}분"
            other = [r for r in problem.get('reasons',[]) if r != 'history_minutes_missing_or_conflicting']
            if other:
                detail += ' · '+', '.join(other)
        else:
            detail = ', '.join(problem.get('reasons',[]))
        parts.append(problem['session']+': '+detail)
    return '; '.join(parts)


def render_watchlist(state, candidates):
    import streamlit as st
    from .session_app import timestamp, usd, _table
    state = state or {}
    now = datetime.now(timezone.utc)
    status, active = current_status(state, now)
    items = state.get('items', [])
    focus = {row['ticker'] for row in items if focus_is_monitored(row, now)} if active and status == 'VALID' else set()
    pending = {row['ticker'] for row in items if row['ticker'] in focus and row.get('focus_refresh_pending')}
    signals = [row for row in candidates or [] if row.get('symbol') in focus-pending and row.get('eligible') and row.get('signal')
               and row.get('timestamp') and 0 <= (now-utc(row['timestamp'])).total_seconds() <= 120]
    st.subheader(f"시총 후보 최대 {state.get('watchlist_limit',50)} → 분봉 준비 종목 운영 → 집중 분석 최대 {state.get('focus_limit',20)}")
    cols = st.columns(5)
    cols[0].metric('S&P 500 전체', str(state.get('universe_count')) if state.get('universe_count') else '—')
    cols[1].metric('유효 운영 종목', f"{state.get('operating_count',len(items)) if status=='VALID' else 0} / {state.get('watchlist_limit',50)}")
    expired = len(items) if status == 'EXPIRED' else 0
    unknown = state.get('watchlist_limit',50) if status not in {'VALID','EXPIRED'} else 0
    cols[2].metric('만료 / 미확인 상한', f'{expired} / {unknown}')
    st.caption('선정 정책 상태: '+LABELS.get(status,status)+' · 미확인 수는 선정 상한 기준이며 확정 종목 수가 아닙니다.')
    cols[3].metric('현재 집중 분석', f"{len(focus)} / {state.get('focus_limit',20)}")
    cols[4].metric('현재 진입 신호', str(len(signals)) if candidates is not None else '—')
    if pending:
        st.info(f'집중 유지 {len(pending)}종목은 자료 갱신을 기다립니다. 계속 조회하며, 새 자료 확인 전에는 매수하지 않습니다.')
    if candidates is None:
        st.caption('진입 신호 기록 조회 미완료 · 신호가 0개라는 뜻은 아닙니다.')
    if active and not focus:
        from collections import Counter
        reasons=Counter(row.get('focus_reason') or '선정 자료 확인 중'
            for row in items if row.get('operating'))
        if reasons:
            st.info('집중 대기 · '+' / '.join(f'{reason} ({count}개)' for reason,count in reasons.most_common(3)))
    history = state.get('history_selection') or {}
    if history.get('preparing'):
        st.info('시작 자료를 준비 중입니다. 보충 검사가 끝나면 운영 종목을 정합니다.')
    elif history:
        phase = '장중 고정' if history.get('frozen') else '장전 준비 · 개장 시 재검사'
        st.caption(f"{history['session']} 거래일 · {phase} · 분봉 준비 {history['included_count']}개 / "
                   f"후보 {history['base_count']}개 · 제외 {history['excluded_count']}개 · 검사 {timestamp(history['checked_at'])}")
        st.caption('분봉 준비 여부는 매수 허가가 아닙니다. 제외 종목도 보유·미체결이면 위험 관리를 계속하며, 복구 자료는 다음 시작 예열 또는 다음 거래일에 재검사합니다.')
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
    excluded_caps = snapshot.get('excluded_market_cap_symbols',[])
    full_ranking = status=='VALID' and snapshot.get('ranking_verified',True)
    if excluded_caps:
        st.info(f"시총 확인 종목 중 상위 후보입니다. 전체 S&P 500 상위 순위는 미확정이며 시총 미확인 {len(excluded_caps)}개를 제외했습니다: "+', '.join(excluded_caps))
    if snapshot.get('currency')=='KRW':
        st.caption('토스 보고 종목 시총(원화 환산·일별 갱신). 주식 클래스별 집계이며 발행사 합산이 아닙니다. 공급자의 정확한 평가 시각은 미제공입니다.')
    rows = []
    for row in items:
        last = row.get('last_detail_at')
        age = max(0, int((now-utc(last)).total_seconds())) if last and utc(last) <= now else None
        blockers = list(row.get('entry_blockers',[]))
        if status != 'VALID': blockers.append('watchlist_'+status.lower())
        if row['ticker'] not in focus: blockers.append('focus_unknown_or_stale')
        rows.append({'종목':row['ticker'], '시총 순위':row.get('rank') if full_ranking else None, '검증자료 내 순위':row.get('rank'), '시가총액':(row.get('market_cap_value')+' KRW') if row.get('currency')=='KRW' and row.get('market_cap_value') else usd(row.get('market_cap_usd')),
            '일일 분봉 준비':row.get('history_ready') if history and not history.get('preparing') else None,
            '운영 대상':bool(row.get('operating')) and status=='VALID' and active,
            '분봉 제외 사유':history_problem_text((row.get('history_selection') or {}).get('problems',[]))
                or ('시작 자료 준비 중' if history.get('preparing') else '당일 고정 목록 밖' if history and not row.get('history_ready') else ''),
            '선정 상태':LABELS.get(status,status),'시총 선택 이유':row.get('selection_reason'),
            '현재 집중':row['ticker'] in focus, '자료 갱신 대기':row['ticker'] in pending, '집중 근거':row.get('focus_reason'),
            '시총 기준일/수집 시작':timestamp(row.get('as_of_at')), '시총 수신일':timestamp(row.get('received_at')),
            '알려진 시각':timestamp(row.get('known_at')), '최종 상세 조회':timestamp(last),
            '최근 조회 시도':timestamp(row.get('last_detail_attempt_at')), '상세 자료 지연(초)':age,
            '신규 진입 차단':', '.join(dict.fromkeys(blockers)) if active else '현재 수집 상태 미확인'})
    _table(rows, '시가총액 공급 자료를 확인한 뒤 감시·집중 후보를 표시합니다.')
    st.caption('관찰 점수는 매수 승인이 아닙니다. 선택한 Paper 거래 조건과 RVOL·확정봉·시장·손실 제한을 별도로 통과해야 합니다. 보유·미체결·청산 예정 및 SPY·QQQ는 이 상한과 별도로 관리합니다.')
