"""모의매매 홈: 현재 상태 네 가지와 보유 장부만 요약한다."""
from datetime import datetime, timezone

from .session_app import age_seconds, decimal_value, timestamp, usd
from .watchlist_panel import current_status


def _count(value):
    return str(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else '—'


def operation_cards(data, view, now=None):
    """조회 기록과 현재 연결을 구분한다. 저장값만으로 정상·완료를 만들지 않는다."""
    now = now or datetime.now(timezone.utc)
    engine = data.engine_state
    age = age_seconds(engine.get('heartbeat'), now)
    online = bool(engine.get('mode')=='paper' and engine.get('armed') and age is not None and age <= 60)
    fresh = []
    for row in view['quotes'].values():
        received = age_seconds(row.get('received_at'), now)
        event = age_seconds(row.get('timestamp'), now)
        if received is not None and event is not None and received <= 60 and event <= 60:
            fresh.append(received)
    market = {'label':'시세', 'value':'최근 수신' if online and fresh else '시세 대기' if online else '연결 미확인',
              'detail':f'{int(min(fresh))}초 전 · {len(fresh)}종목' if online and fresh else '새 시세 기록을 기다립니다.' if online else '현재 엔진 연결을 확인하세요.',
              'help':'최근 60초 안에 수신한 최신 호가 기준입니다. 전체 종목의 실시간 권한이나 모든 입력의 적격성을 뜻하지 않습니다.', 'warning':False}
    warmup = data.warmup
    status, current = current_status(data.watchlist_state, now)
    phase = warmup.get('status')
    selection = {'label':'종목', 'value':'확인 대기', 'detail':'감시 목록과 분봉을 확인합니다.', 'warning':False,
                 'help':'분봉 준비는 매수 허가가 아닙니다. 전략·위험 조건도 통과해야 합니다.'}
    if not online:
        selection.update(value='저장 기록' if warmup else '기록 없음', detail='프로그램 연결 후 다시 확인합니다.')
    elif phase == 'WAITING_WATCHLIST':
        selection.update(value='목록 대기', detail='사용 가능한 감시 목록을 기다립니다.')
    elif phase in {'READY','PARTIAL'}:
        if status == 'VALID' and current:
            count = warmup.get('history_ready_count')
            from ..models import utc
            items=data.watchlist_state.get('items')
            focused=None
            if isinstance(items,list):
                focused=0
                for row in items:
                    try:
                        if row.get('focused') and utc(row['focus_valid_until']) >= now:focused+=1
                    except (ValueError,TypeError,KeyError,AttributeError):pass
            selection.update(value=f'{_count(count)}개 준비', detail=f"집중 {_count(focused)}개 · 분봉 부족 {_count(warmup.get('history_excluded_count'))}개 제외",
                             warning=not isinstance(count,int) or isinstance(count,bool) or count <= 0)
        else:
            selection.update(value='목록 확인 필요', detail='목록이 만료됐거나 최신 상태를 확인할 수 없습니다.', warning=True)
    elif warmup:
        total = warmup.get('required_tasks')
        selection.update(value='조회 대기' if phase=='RETRY_WAIT' else '분봉 확인',
                         detail=f"{_count(warmup.get('completed_tasks'))} / {_count(total)}구간 확인" if isinstance(total,int) and total>0 else '확인할 구간을 계산하고 있습니다.')
    flow = data.news_flow
    counts = flow.get('counts') or {}
    scope = flow.get('scope') or {}
    truncated = scope.get('may_be_truncated') or scope.get('requests_may_be_truncated')
    news = {'label':'뉴스', 'value':f"{_count(counts.get('unique_articles'))}건 수집" if flow else '기록 없음',
            'detail':('최근 24시간 · 조회 범위' if truncated else '최근 24시간') if flow else '수집 기록을 기다립니다.',
            'help':'중복을 제외한 기사 수입니다. 최근 수신 '+timestamp(flow.get('last_news_received_at'))+'. 저장 기록이며 현재 수집 연결의 증거는 아닙니다.', 'warning':False}
    failed = counts.get('failed_or_rejected') or 0
    health = (flow.get('llm_health') or {}).get('code')
    problems = {'NO_KEY':'API 키 확인 필요', 'NO_CREDIT':'사용 한도·잔액 확인 필요', 'AUTH_ERROR':'인증 확인 필요'}
    ai = {'label':'AI 분석', 'value':f"{_count(counts.get('validated_responses'))}건 완료" if flow else '기록 없음',
          'detail':problems.get(health) or (f'실패·거부 {_count(failed)}건 · 확인 필요' if failed else f"요청 {_count(counts.get('llm_request_attempts'))}건 · 결과 대기 {_count(counts.get('pending_or_unconfirmed'))}건" if flow else '분석 기록을 기다립니다.'),
          'warning':bool(failed or health in problems),
          'help':'최근 24시간 조회 범위의 응답 검증 통과 건수입니다. 분류의 정답률이나 매수 승인을 뜻하지 않습니다. 최근 요청 '+timestamp(flow.get('last_llm_request_at'))}
    return [market, selection, news, ai]


def render_warmup_details(warmup):
    import streamlit as st
    from .session_app import _table
    if not warmup:
        return
    from .labels import label
    with st.expander('자료 준비 자세히'):
        st.write(label(warmup.get('status','UNKNOWN')))
        st.caption(warmup.get('message','')+' · 마지막 확인 '+timestamp(warmup.get('updated_at')))
        if warmup.get('required_tasks'):
            st.caption(f"{warmup.get('completed_tasks',0)} / {warmup['required_tasks']}구간 확인")
        st.caption('검사 기준 '+timestamp(warmup.get('cutoff_at')))
        if warmup.get('reused_history_tasks'):
            st.caption(f"완료 검사 재사용 {warmup['reused_history_tasks']}구간")
        if warmup.get('deferred_history_tasks'):
            st.caption(f"최근 조회한 누락 {warmup['deferred_history_tasks']}구간은 재조회 대기 · 해당 종목 제외 유지")
        if warmup.get('issues'):
            _table(warmup['issues'])
        elif warmup.get('status') in {'READY','PARTIAL'}:
            st.caption('보충 검사에서 기록된 누락이 없습니다.')
        else:
            st.caption('검사가 끝나면 누락 결과가 표시됩니다.')


def render_operations(data, view, path):
    import streamlit as st
    from contextlib import closing
    from ..database.repository import Repository
    from ..trading_controls import render_controls
    from .labels import market_hours_label
    from .overview import link
    from .session_app import _table

    now = datetime.now(timezone.utc)
    st.subheader('프로그램 상태')
    st.caption('모의매매 · '+market_hours_label(now))
    if data.connected:
        try:
            with closing(Repository(path, initialize=False)) as repo:
                positions = (data.state.get('broker') or {}).get('positions') or {}
                render_controls(repo, positions=[{'symbol':s,**r} for s,r in positions.items()],
                                environment=view['environment'], compact=True)
        except Exception:
            st.warning('매매 제어 연결을 확인하세요. 시스템·설정에서 상태를 볼 수 있습니다.')
    else:
        st.caption('프로그램 연결 대기')
        controls = st.columns(2)
        controls[0].button('매매 시작', disabled=True, key='offline-start')
        controls[1].button('전체 매도', disabled=True, key='offline-stop')

    for column, card in zip(st.columns(4), operation_cards(data,view,now)):
        with column:
            with st.container(border=True):
                st.metric(card['label'],card['value'],help=card.get('help'))
                st.caption((':orange['+card['detail']+']') if card['warning'] else card['detail'])

    account = view['account']
    cash = decimal_value(account.get('cash'))
    reserved = decimal_value(account.get('reserved_cash'))
    summary = st.columns(2)
    summary[0].metric('모의 총자산',usd(account.get('equity')),help='저장된 모의 장부 기준입니다. 최신 시세를 받기 전에는 이전 평가가 남아 있을 수 있습니다.')
    summary[1].metric('매수 가능 금액',usd(cash-reserved) if cash is not None and reserved is not None else '—')
    st.subheader('보유 종목')
    _table([{k:r[k] for k in ('종목','평가액','평가 손익')} for r in view['positions']], '보유 종목이 없습니다.')
    fills=data.tables.get('executions',[])
    if fills:
        latest=max(fills,key=lambda r:str(r.get('timestamp') or r.get('filled_at') or ''))
        st.caption('최근 모의 체결 · '+str(latest.get('symbol','—'))+' · '+timestamp(latest.get('timestamp') or latest.get('filled_at')))
    else:
        st.caption('조회 범위에 모의 체결 기록이 없습니다.')
    from .labels import label
    strategy = data.context.get('strategy') or data.state.get('strategy')
    if strategy:
        st.caption('전략 · '+label(strategy))
    footer = st.columns(2)
    with footer[0]:
        link('매매 전체 보기','매매 내역')
    with footer[1]:
        link('자료·연결 자세히','시스템·설정')
