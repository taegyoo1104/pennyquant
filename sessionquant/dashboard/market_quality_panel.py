"""현재 확보율과 부족 입력을 보여 주며 과거 조회 성공을 실시간으로 표시하지 않는다."""
from ..market.reporting import current_report


def render_quality(state):
    import streamlit as st
    from .session_app import _table, timestamp
    report = current_report(state)
    st.subheader('시세·거래 적격성·RVOL 데이터 품질')
    st.caption('최근 평가 시각: '+timestamp(report.get('captured_at'))+' · 대상: 현재 집중 후보와 위험 관리 종목. SPY·QQQ는 별도 참조입니다.')
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
        rows.append({'종목':item['symbol'],'분봉 상태':bars['status'],'제공처':', '.join(bars.get('source',[])),
            '평가 시각':timestamp(item.get('observed_at')),'호가 시각':timestamp(item.get('quote_at')),
            '누락 분':bars.get('missing_minutes'),'누락률':bars.get('missing_rate'),
            '정정 충돌 분':bars.get('conflicting_minutes'),'명시적 거래량 0분':bars.get('explicit_zero_volume_minutes'),
            '분봉 지연(초)':bars.get('age_seconds'),'수신 지연(초)':bars.get('latest_receipt_lag_seconds'),
            'RVOL(검증됨)':volume.get('value'),'RVOL(계산값·비교 근거 미확인 가능)':volume.get('calculated_value'),
            '완전한 과거 거래일':volume.get('complete_sessions'),
            '필요 거래일':volume.get('required_sessions',20),'RVOL 부족 입력':', '.join(volume.get('reasons',[])),
            '계좌 검증 범위':(item.get('eligibility') or {}).get('account_scope','UNKNOWN'),
            '제한 사유':', '.join(item.get('blockers',[]))})
    _table(rows)
    with st.expander('우선 조회·요청 지연·과거 봉 보충 근거'):
        source = report.get('source_state') or {}
        st.caption('위험 호가 전달: '+timestamp(source.get('priority_delivered_at'))+' · 후보보다 먼저 엔진에 전달합니다. 네트워크 오류·거래소 지연까지 성공을 보장하지 않습니다.')
        _table(source.get('read_measurements',[]),'수집 주기 계측 자료가 없습니다.')
        _table([{'종목':s,**row} for s,row in source.get('history_progress',{}).items()],'과거 봉 보충 이력이 없습니다.')
        _table([{'종목':s,**row} for s,row in report.get('market_reference_quality',{}).items()])
    st.caption('토스 ACTIVE·보유·매수 가능 현금·빈 유의사항은 소수점 주문 자격의 증거가 아닙니다. 누락 봉은 보간하지 않으며 분할 전후 거래량 비교가 미확인이면 RVOL은 UNKNOWN입니다.')


def render_watchlist_rvol(view, backfill):
    import streamlit as st
    from .session_app import _table, timestamp
    from ..market.history import current_view
    st.subheader('감시목록 과거 분봉·RVOL')
    st.caption(f"장전 수집: {backfill.get('status','미실행')} · 완료 종목×거래일 "
               f"{backfill.get('complete_symbol_days',0)} / {backfill.get('required_symbol_days',0)}")
    st.caption('감시 종목은 호출 예산 안에서 순환합니다. 계산값은 비교 근거 검증과 별개이며, 오래된 값은 숨깁니다.')
    rows = current_view(view).get('items',[])
    _table([{'종목':r['symbol'],'RVOL 계산값':r.get('calculated_value'),'매매용 검증값':r.get('value'),
             '현재성':'최신' if r.get('fresh') else '지연·미확인',
             '계산 기준 시각':timestamp(r.get('cutoff_at')),'수신·평가 시각':timestamp(r.get('observed_at')),
             '과거 확보 거래일':r.get('complete_sessions'),'현재 누적 거래량':r.get('current_cumulative_volume'),
             '과거 동시간 평균':r.get('historical_mean_cumulative_volume'),
             '확인 필요':', '.join(r.get('reasons',[]))} for r in rows],
           '장전 history-backfill 후 run으로 당일 분봉을 수집하세요.')
