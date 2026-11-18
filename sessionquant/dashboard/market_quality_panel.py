"""현재 확보율과 부족 입력을 보여 주며 과거 조회 성공을 실시간으로 표시하지 않는다."""
from ..market.reporting import current_report


def render_quality(state, *, performance=None):
    import streamlit as st
    from .session_app import _table, timestamp
    report = current_report(state)
    st.subheader('시세·거래 적격성·RVOL 데이터 품질')
    st.caption('최근 평가 시각: '+timestamp(report.get('captured_at'))+' · 대상: 현재 집중 후보와 위험 관리 종목. SPY·QQQ는 별도 참조입니다.')
    st.caption('당일 분봉 수집 상태와 전략에 쓰는 확정봉 상태를 구분합니다. 실제 매수 판단은 아래 후보 선정 표에서 확인할 수 있습니다.')
    if report['synthetic']:
        st.warning('합성 입력의 데이터 품질 검사입니다. 실제 계좌 자격·시장 확보율이 아닙니다.')
    columns = st.columns(6)
    labels = {'eligibility':'거래 자격','quote':'최신 호가','minute_history':'1분봉 연속성',
              'five_bars':'5분봉 확보','fifteen_bars':'15분봉 확보','rvol':'RVOL 근거'}
    for column,(key,label) in zip(columns,labels.items()):
        metric = report['required_data_coverage'][key]
        column.metric(label,f"{metric['verified']} / {metric['required']}" if metric['required'] else '미평가')
    _table([{'차단 사유':code,'종목 수':count} for code,count in report['blocker_counts'].items()],
           '아직 평가한 종목이 없습니다. 입력 미확인은 매수 허가가 아닙니다.')
    rows = []
    for item in report['items']:
        bars, volume = item['bars'], item['rvol']
        strategy_bars=item.get('strategy_bars',{})
        rows.append({'종목':item['symbol'],'당일 분봉 수집':bars['status'],
            '전략 확정봉 상태':strategy_bars.get('status','UNKNOWN'),
            '전략 기준 시각':timestamp(strategy_bars.get('cutoff_at')),'제공처':', '.join(bars.get('source',[])),
            '평가 시각':timestamp(item.get('observed_at')),'호가 시각':timestamp(item.get('quote_at')),
            '누락 분':bars.get('missing_minutes'),'누락률':bars.get('missing_rate'),
            '정정 충돌 분':bars.get('conflicting_minutes'),'명시적 거래량 0분':bars.get('explicit_zero_volume_minutes'),
            '분봉 지연(초)':bars.get('age_seconds'),'수신 지연(초)':bars.get('latest_receipt_lag_seconds'),
            'RVOL(검증됨)':volume.get('value'),'RVOL(계산값·비교 근거 미확인 가능)':volume.get('calculated_value'),
            'Paper 거래 조건':(item.get('paper_eligibility') or {}).get('status','strict'),
            'Paper 가정':', '.join((item.get('paper_eligibility') or {}).get('assumptions',[])),
            '전략에 전달한 RVOL':item.get('strategy_rvol'),'전략 RVOL 근거 상태':item.get('strategy_rvol_status'),
            '완전한 과거 거래일':volume.get('complete_sessions'),
            '필요 거래일':volume.get('required_sessions',20),'RVOL 부족 입력':', '.join(volume.get('reasons',[])),
            '계좌 검증 범위':(item.get('eligibility') or {}).get('account_scope','UNKNOWN'),
            '제한 사유':', '.join(item.get('blockers',[]))})
    _table(rows)
    with st.expander('우선 조회·요청 지연·과거 봉 보충 근거'):
        source = report.get('source_state') or {}
        st.caption('위험 호가 전달: '+timestamp(source.get('priority_delivered_at'))+' · 후보보다 먼저 엔진에 전달합니다. 네트워크 오류·거래소 지연까지 성공을 보장하지 않습니다.')
        _table(source.get('read_measurements',[]),'수집 주기 계측 자료가 없습니다.')
        _table(history_progress_rows(source.get('history_progress',{})),'과거 봉 보충 이력이 없습니다.')
        _table([{'종목':s,**row} for s,row in report.get('market_reference_quality',{}).items()])
    with st.expander('처리 속도 확인'):
        latest = (performance or {}).get('latest',{})
        st.caption('마지막 수집: '+timestamp(latest.get('observed_at'))+
            f" · 전체 {latest.get('elapsed_seconds','—')}초 · 프로세스 CPU {latest.get('process_cpu_seconds','—')}초")
        _table(performance_rows(latest),'새 버전으로 수집을 실행하면 측정 결과가 나옵니다.')
        st.caption('DB 작업별 내역 · 느린 작업부터 표시합니다. SQL 원문·접속 정보는 저장하지 않습니다.')
        _table(database_rows(latest),'DB 작업별 측정 결과가 아직 없습니다.')
        st.caption('아래 작업 시간은 서로 포함될 수 있어 합산하지 않습니다. API 시간에는 호출 간격·재시도를 분리하며, HTTP 응답 대기 중 다른 작업에 밀린 시간은 포함될 수 있습니다. DB 요청 시간은 MySQL에서 측정합니다. CPU 시간은 같은 프로세스의 뉴스 작업 등도 포함합니다.')
        slow = (performance or {}).get('slowest_this_process',[])
        _table([{'확인 시각':timestamp(r.get('observed_at')),'전체 시간(초)':r['elapsed_seconds'],
            '프로세스 CPU(초)':r['process_cpu_seconds']} for r in slow], '느린 수집 기록이 없습니다.')
    st.caption('실제 계좌 자격과 거래량 비교 근거는 별도로 표시합니다. observed_paper_v1에서는 상장 상태와 시세 품질을 확인한 뒤 가상의 소수점 거래를 허용하고, 완전한 20거래일의 계산 RVOL을 비교 근거 미검증 상태로 사용합니다. 누락·충돌 봉은 보간하지 않습니다.')


def history_progress_rows(progress):
    from .session_app import timestamp
    states={'SESSION_WINDOW_REACHED':'당일 조회 범위 도달','WINDOW_CACHED':'저장 자료 재사용',
            'PAGING':'과거 구간 조회 중','DONE':'조회 종료','EXHAUSTED':'더 이전 자료 없음',
            'WINDOW_REACHED':'필요 조회 범위 도달','CURSOR_STALLED':'다음 조회 위치 확인 필요'}
    return [{'종목':symbol,'보충 상태':states.get(row.get('status'),row.get('status','UNKNOWN')),
             '추가 조회 횟수':row.get('pages',0),'다음 조회 기준':timestamp(row.get('cursor')),
             '마지막 시도':timestamp(row.get('last_attempt_at')),
             '마지막 수신':timestamp(row.get('last_received_at'))}
            for symbol,row in progress.items()]


def performance_rows(snapshot):
    labels = {'api_pacing':'API 호출 간격 대기','api_http':'API 응답 대기','api_retry_wait':'API 재시도 대기',
        'db_query':'DB 요청·응답','db_commit':'DB 저장 확정','json_encode':'DB 자료 변환',
        'engine_batch':'엔진 판단·저장 전체','engine_persist':'엔진 상태 저장','history_save':'분봉 합치기·저장',
        'history_load':'과거 분봉 읽기·기록','indicators':'매매 지표 계산','rvol':'RVOL 계산',
        'bar_quality':'분봉 품질 검사','warmup':'예열 전체','watchlist_publish':'감시 목록 저장'}
    return [{'작업':labels.get(row['phase'],'기타 작업'),'횟수':row['calls'],
             '누적 시간(초)':row['seconds'],'최장 시간(초)':row['max_seconds']}
            for row in snapshot.get('phases',[])]


def database_rows(snapshot):
    operations={'SELECT':'조회','INSERT':'기록','UPDATE':'갱신','DELETE':'삭제',
                'CREATE':'구조 준비','ALTER':'구조 변경','SET':'연결 설정','START':'저장 묶음 시작'}
    tables={'pq_controls':'제어·감시 목록','pq_engine_state':'엔진 복구 상태',
        'pq_market_bars':'분봉','pq_quote_snapshots':'호가','pq_market_history_sessions':'과거 분봉',
        'pq_research_events':'재현용 입력 기록','pq_candidates':'매수 후보',
        'pq_pipeline_status':'처리 과정','pq_news_analysis':'종목 뉴스 분석',
        'pq_macro_analysis':'시장 뉴스 분석','pq_economic_events':'경제 일정',
        'pq_order_intents':'주문·화면 요청','pq_orders':'주문','pq_executions':'체결',
        'pq_positions':'보유 종목','pq_account_snapshots':'자산 기록',
        'pq_reconciliations':'장부 확인','pq_session_state':'거래 상태',
        'pq_daily_performance':'일별 성과','pq_market_regimes':'시장 방향',
        'sq_financial_ledger':'금액 장부','pq_system_logs':'운영 기록'}
    return [{'DB 작업':operations.get(r['operation'],'기타'),
             '자료':tables.get(r['table'],'연결·기타 자료'),'횟수':r['calls'],
             '누적 시간(초)':r['seconds'],'최장 시간(초)':r['max_seconds']}
            for r in sorted(snapshot.get('database',[]),key=lambda r:r['seconds'],reverse=True)]


def watchlist_rvol_rows(view, now=None):
    """Keep stale observations visible as history; never re-enable trading values."""
    from datetime import datetime, timezone
    from ..models import utc
    from ..market.history import current_view
    from .session_app import timestamp, decimal_value
    now=utc(now or datetime.now(timezone.utc))
    saved={r['symbol']:r for r in (view or {}).get('items',[])}
    result=[]
    for row in current_view(view,now).get('items',[]):
        original=saved.get(row['symbol'],{})
        prior=decimal_value(original.get('calculated_value'))
        age=None
        try:
            cutoff,observed=utc(row['cutoff_at']),utc(row['observed_at'])
            if not cutoff <= observed <= now:raise ValueError('future observation')
            age=int((now-cutoff).total_seconds())
        except (ValueError,TypeError,KeyError):
            prior=None
        if prior is not None and prior < 0:prior=None
        status=('최신' if row.get('fresh') else '이전 계산 · 갱신 대기' if prior is not None
                else '계산 자료 부족')
        result.append({'종목':row['symbol'],'RVOL 계산값':row.get('calculated_value'),
            '마지막 계산값·참고':float(prior) if prior is not None else None,
            '매매용 검증값':row.get('value'),'현재성':status,'기준 시각 경과(초)':age,
            '계산 기준 시각':timestamp(row.get('cutoff_at')),'수신·평가 시각':timestamp(row.get('observed_at')),
            '과거 확보 거래일':row.get('complete_sessions'),'현재 누적 거래량':row.get('current_cumulative_volume'),
            '과거 동시간 평균':row.get('historical_mean_cumulative_volume'),
            '확인 필요':', '.join(row.get('reasons',[]))})
    return result


def render_watchlist_rvol(view, backfill):
    import streamlit as st
    from .session_app import _table
    st.subheader('감시목록 과거 분봉·RVOL')
    st.caption(f"장전 수집: {backfill.get('status','미실행')} · 완료 종목×거래일 "
               f"{backfill.get('complete_symbol_days',0)} / {backfill.get('required_symbol_days',0)}")
    st.caption('마지막 계산값은 해당 기준 시각의 참고 기록입니다. 오래되면 현재 계산값·매매용 검증값은 비워 두며, 전략 허용으로 사용하지 않습니다.')
    _table(watchlist_rvol_rows(view),
           '장전 history-backfill 후 run으로 당일 분봉을 수집하세요.')
